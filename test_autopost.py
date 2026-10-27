"""
Offline tests for autopost.py. No internet needed: every HTTP request (RSS, AI, WordPress,
images) is answered by a fake web server inside the test.

Run them with:   python -m unittest -v
"""
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

import autopost

FEED_URL = "https://news.example.com/rss.xml"
FEED2_URL = "https://other.example.org/feed"
WP = "https://mysite.example"
AI_URL = "https://ai.example/v1/chat/completions"

ENV = {
    "WP_URL": WP,
    "WP_USER": "editor",
    "WP_APP_PASSWORD": "abcd efgh ijkl mnop qrst uvwx",
    "AI_API_KEY": "test-key",
    "AI_BASE_URL": "https://ai.example/v1/",
    "AI_MODEL": "test-model",
}


def make_rss(items):
    parts = []
    for i, item in enumerate(items):
        extra = item.get("extra", "")
        parts.append(f"""
    <item>
      <title>{item['title']}</title>
      <link>{item.get('link', f'https://news.example.com/story-{i}')}</link>
      <guid isPermaLink="false">{item.get('guid', f'guid-{i}')}</guid>
      <description>{item.get('summary', 'Something happened in the city today.')}</description>
      <pubDate>{item.get('date', f'Mon, 26 Oct 2026 {10 - i:02d}:00:00 GMT')}</pubDate>
      {extra}
    </item>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
  <channel><title>Example News</title><link>https://news.example.com</link>
  {''.join(parts)}
  </channel>
</rss>""".encode()


def response(status=200, json_data=None, content=b"", headers=None, url=""):
    resp = requests.Response()
    resp.status_code = status
    if json_data is not None:
        content = json.dumps(json_data).encode()
        headers = {"Content-Type": "application/json", **(headers or {})}
    resp._content = content
    resp._content_consumed = True
    resp.headers.update(headers or {})
    resp.encoding = "utf-8"
    resp.url = url
    return resp


def ai_answer(title="A fresh headline", tags=("Politics", "City", "News")):
    article = {
        "title": title,
        "content": "<p>First paragraph.</p><h2>Details</h2><p>Second paragraph.</p>",
        "excerpt": "Short summary.",
        "tags": list(tags),
    }
    text = "```json\n" + json.dumps(article) + "\n```"
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


class FakeWeb:
    """Answers requests.Session.request calls (used by requests.get/post too)."""

    def __init__(self):
        self.calls = []
        self.feeds = {}
        self.posts = []
        self.tags = {"news": 7}          # an existing tag on the "site"
        self.media_uploads = 0
        self.ai_calls = 0
        self.ai_status = []              # e.g. [200, 429] -> 2nd AI call is rate limited
        self.ai_text = None
        self.wp_auth_ok = True
        self.image_ok = True
        self.post_status_code = 201
        # Where the fake site's REST API lives:
        #   "normal" /wp-json/, "plain" ?rest_route= (Plain permalinks), "blog" WordPress in /blog,
        #   "none" no WordPress at all (every address gives the web server's 404 page)
        self.site = "normal"

    def api_prefix(self):
        return {"normal": WP + "/wp-json/wp/v2", "plain": WP + "/?rest_route=/wp/v2",
                "blog": WP + "/blog/wp-json/wp/v2"}.get(self.site)

    def homepage(self):
        if self.site == "plain":
            return response(content=b"<html>wp-content</html>", url=WP + "/",
                            headers={"Link": f'<{WP}/?rest_route=/>; rel="https://api.w.org/"'})
        if self.site == "blog":
            html = f'<html><head><link rel="https://api.w.org/" href="{WP}/blog/wp-json/" /></head></html>'
            return response(content=html.encode(), url=WP + "/")
        return response(404, content=b"404 Not Found Proudly powered by LiteSpeed Web Server", url=WP + "/")

    def __call__(self, session, method, url, **kwargs):
        method = method.upper()
        self.calls.append((method, url, kwargs))
        params = kwargs.get("params") or {}

        if url in self.feeds:
            feed = self.feeds[url]
            return feed if isinstance(feed, requests.Response) else response(content=feed, url=url)

        if url == AI_URL:
            self.ai_calls += 1
            status = self.ai_status.pop(0) if self.ai_status else 200
            if status != 200:
                return response(status, {"error": {"message": "quota exceeded"}})
            if self.ai_text is not None:
                return response(200, {"choices": [{"message": {"content": self.ai_text}}]})
            return response(200, ai_answer(title=f"Original headline {self.ai_calls}"))

        if url.startswith("https://img.example/"):
            if not self.image_ok:
                return response(404, content=b"not found")
            return response(content=b"\xff\xd8\xff fake jpeg", headers={"Content-Type": "image/jpeg"})

        prefix = self.api_prefix()
        if prefix and url.startswith(prefix + "/"):
            if not self.wp_auth_ok:
                return response(401, {"code": "incorrect_password", "message": "The provided password is invalid."})
            path = url[len(prefix):]
            if method == "GET" and path == "/users/me":
                return response(200, {"id": 3, "name": "Editor", "roles": ["editor"]})
            if method == "GET" and path == "/categories":
                return response(200, [{"id": 1, "name": "Uncategorized"}, {"id": 5, "name": "World"}])
            if method == "GET" and path == "/tags":
                search = params.get("search", "").lower()
                return response(200, [{"id": i, "name": n.title()} for n, i in self.tags.items() if search in n])
            if method == "POST" and path == "/tags":
                name = kwargs["json"]["name"]
                new_id = 100 + len(self.tags)
                self.tags[name.lower()] = new_id
                return response(201, {"id": new_id, "name": name})
            if method == "POST" and path == "/media":
                self.media_uploads += 1
                return response(201, {"id": 55})
            if method == "POST" and path == "/media/55":
                return response(200, {"id": 55})
            if method == "POST" and path == "/posts":
                if self.post_status_code != 201:
                    return response(self.post_status_code, {"code": "rest_invalid_param", "message": "Invalid parameter(s): categories"})
                self.posts.append(kwargs["json"])
                post_id = 1000 + len(self.posts)
                return response(201, {"id": post_id, "status": kwargs["json"]["status"], "link": f"{WP}/?p={post_id}"})
        if url == WP + "/":
            return self.homepage()
        if url.startswith(WP + "/"):
            return response(404, content=b"404 Not Found The resource requested could not be found on this server! "
                                         b"Proudly powered by LiteSpeed Web Server")
        raise AssertionError(f"Unexpected request: {method} {url}")

    def count(self, method, pattern):
        return sum(1 for m, u, _ in self.calls if m == method and re.search(pattern, u))


class AutopostTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.config = self.dir / "feeds.json"
        self.seen_path = self.dir / "seen.json"
        self.seen_path.write_text("[]")
        self.write_config()
        self.web = FakeWeb()
        self.web.feeds[FEED_URL] = make_rss([
            {"title": "Story zero", "extra": '<media:thumbnail url="https://img.example/zero.jpg" width="240"/>'},
            {"title": "Story one", "extra": '<enclosure url="https://img.example/one.jpg" type="image/jpeg" length="1"/>'},
            {"title": "Story two"},
            {"title": "Story three"},
            {"title": "Story four"},
        ])
        patches = [
            # a plain function (not the object itself) so Python passes the session as `self`
            mock.patch.object(requests.Session, "request",
                              new=lambda session, method, url, **kw: self.web(session, method, url, **kw)),
            mock.patch.dict(os.environ, ENV),
            mock.patch.object(autopost.time, "sleep"),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        autopost.setup_logging()
        self.addCleanup(self.tmp.cleanup)

    def write_config(self, feeds=None, **settings):
        base = {"delay_between_posts_sec": 0, "max_posts_per_feed": 2, "max_posts_per_run": 6}
        base.update(settings)
        feeds = feeds or [{"url": FEED_URL, "category_id": 5, "category_name": "World", "source_name": "Example News"}]
        self.config.write_text(json.dumps({"settings": base, "feeds": feeds}))

    def run_autopost(self, dry_run=False):
        return autopost.run(self.config, self.seen_path, dry_run=dry_run)

    def seen(self):
        return json.loads(self.seen_path.read_text())

    def output(self):
        return autopost.sys.stdout.getvalue()


class TestHelpers(unittest.TestCase):
    def test_parse_ai_json_with_fences_and_extra_text(self):
        text = 'Sure! Here it is:\n```json\n{"title": "T", "content": "<p>Body\nline</p>", "excerpt": "E", "tags": ["a", "b", "A", "c"]}\n```'
        art = autopost.parse_ai_json(text)
        self.assertEqual(art["title"], "T")
        self.assertIn("<p>Body", art["content"])
        self.assertEqual(art["tags"], ["a", "b", "c"])  # duplicates removed (case-insensitive)

    def test_parse_ai_json_rejects_garbage(self):
        for bad in ("", "no json here", '{"title": "only a title"}', "[1, 2]"):
            with self.assertRaises(autopost.AIError):
                autopost.parse_ai_json(bad)

    def test_content_is_sanitized(self):
        art = autopost.parse_ai_json(json.dumps({
            "title": "T", "content": "<html><body><h1>Big</h1><script>alert(1)</script><p>ok</p></body></html>",
            "tags": "one, two, three",
        }))
        self.assertEqual(art["content"], "<h2>Big</h2><p>ok</p>")
        self.assertEqual(art["tags"], ["one", "two", "three"])
        self.assertEqual(art["excerpt"], "Big ok")  # made from the content when missing

    def test_plain_text_content_gets_paragraphs(self):
        art = autopost.parse_ai_json(json.dumps({"title": "T", "content": "Para one.\n\nPara two & more."}))
        self.assertEqual(art["content"], "<p>Para one.</p>\n<p>Para two &amp; more.</p>")

    def test_item_id_fallbacks(self):
        self.assertEqual(autopost.item_id({"id": "g1", "link": "L", "title": "T"}), "g1")
        self.assertEqual(autopost.item_id({"id": " ", "link": "L", "title": "T"}), "L")
        self.assertEqual(autopost.item_id({"title": "T"}), "T")
        self.assertIsNone(autopost.item_id({}))

    def test_find_image(self):
        self.assertEqual(autopost.find_image({"media_content": [
            {"url": "small.jpg", "width": "100", "medium": "image"},
            {"url": "video.mp4", "medium": "video"},
            {"url": "big.jpg", "width": "800"},
        ]}), "big.jpg")
        self.assertEqual(autopost.find_image({"media_thumbnail": [{"url": "thumb.jpg"}]}), "thumb.jpg")
        self.assertEqual(autopost.find_image({"enclosures": [{"href": "a.mp3", "type": "audio/mpeg"},
                                                             {"href": "pic.png", "type": "image/png"}]}), "pic.png")
        self.assertIsNone(autopost.find_image({"title": "no image"}))

    def test_env_treats_blank_as_unset(self):
        with mock.patch.dict(os.environ, {"AI_MODEL": "", "AI_BASE_URL": "   "}):
            self.assertIsNone(autopost.env("AI_MODEL"))
            client = autopost.AIClient("k", autopost.env("AI_BASE_URL"), autopost.env("AI_MODEL"))
        self.assertEqual(client.model, "gemini-2.5-flash")
        self.assertEqual(client.endpoint, "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")

    def test_groq_endpoint(self):
        client = autopost.AIClient("k", "https://api.groq.com/openai/v1", "llama-3.1-8b-instant")
        self.assertEqual(client.endpoint, "https://api.groq.com/openai/v1/chat/completions")

    def test_normalize_wp_url(self):
        self.assertEqual(autopost.normalize_wp_url("mysite.com/"), "https://mysite.com")
        self.assertEqual(autopost.normalize_wp_url("https://mysite.com/wp-admin/"), "https://mysite.com")
        self.assertEqual(autopost.normalize_wp_url("https://mysite.com/blog/wp-json/wp/v2"), "https://mysite.com/blog")

    def test_round_robin_is_fair(self):
        self.assertEqual(autopost.round_robin([[1, 2, 3], ["a"], ["x", "y"]]), [1, "a", "x", 2, "y", 3])
        self.assertEqual(autopost.round_robin([[1], [2]], offset=1), [2, 1])

    def test_seen_store_keeps_last_5000(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seen.json"
            store = autopost.SeenStore(path)
            for i in range(5100):
                store.add(f"id-{i}")
            store.save()
            data = json.loads(path.read_text())
            self.assertEqual(len(data), 5000)
            self.assertEqual(data[0], "id-100")
            self.assertEqual(data[-1], "id-5099")

    def test_damaged_seen_file_stops_instead_of_reposting(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seen.json"
            path.write_text("{oops")
            with self.assertRaises(autopost.ConfigError):
                autopost.SeenStore(path)


class TestRuns(AutopostTestCase):
    def test_first_run_posts_newest_and_skips_backlog(self):
        self.assertEqual(self.run_autopost(), 0)
        self.assertEqual(len(self.web.posts), 2)
        first = self.web.posts[0]
        self.assertEqual(first["title"], "Original headline 1")
        self.assertEqual(first["status"], "draft")
        self.assertEqual(first["categories"], [5])
        self.assertIn('Source: <a href="https://news.example.com/story-0" rel="nofollow', first["content"])
        self.assertIn(">Example News</a>", first["content"])
        self.assertEqual(first["featured_media"], 55)
        self.assertNotIn("author", first)
        # All 5 items are remembered: 2 posted + 3 old backlog items skipped.
        self.assertEqual(sorted(self.seen()), [f"guid-{i}" for i in range(5)])

    def test_tags_are_reused_created_once_and_cached(self):
        self.run_autopost()
        # "News" already existed (id 7); "Politics" and "City" created once even though used by 2 posts.
        self.assertEqual(self.web.count("POST", r"/tags$"), 2)
        self.assertEqual(self.web.count("GET", r"/tags$"), 3)
        self.assertIn(7, self.web.posts[0]["tags"])
        self.assertEqual(self.web.posts[0]["tags"], self.web.posts[1]["tags"])

    def test_second_run_posts_nothing_twice(self):
        self.run_autopost()
        self.run_autopost()
        self.assertEqual(len(self.web.posts), 2)
        self.assertEqual(self.web.ai_calls, 2)

    def test_new_items_are_posted_on_later_runs(self):
        self.run_autopost()
        self.web.feeds[FEED_URL] = make_rss([{"title": "Breaking", "guid": "guid-new",
                                              "date": "Mon, 26 Oct 2026 12:00:00 GMT"},
                                             {"title": "Story zero", "guid": "guid-0"}])
        self.run_autopost()
        self.assertEqual(len(self.web.posts), 3)
        self.assertIn("guid-new", self.seen())

    def test_rate_limit_stops_cleanly_and_saves_progress(self):
        self.web.ai_status = [200, 429]
        self.assertEqual(self.run_autopost(), 0)
        self.assertEqual(len(self.web.posts), 1)
        seen = self.seen()
        self.assertIn("guid-0", seen)
        self.assertNotIn("guid-1", seen)   # not posted -> will be tried again next run
        self.assertIn("rate limit", self.output())

    def test_bad_feed_does_not_stop_other_feeds(self):
        self.write_config(feeds=[
            {"url": FEED2_URL, "category_id": 9, "source_name": "Broken"},
            {"url": FEED_URL, "category_id": 5, "source_name": "Example News"},
        ])
        self.web.feeds[FEED2_URL] = response(500, content=b"server error")
        self.assertEqual(self.run_autopost(), 0)
        self.assertEqual(len(self.web.posts), 2)
        self.assertIn("Could not read this feed", self.output())

    def test_not_a_feed_is_reported(self):
        self.web.feeds[FEED_URL] = b"<html><body>this is a web page"
        self.assertEqual(self.run_autopost(), 1)
        self.assertIn("does not look like a valid RSS feed", self.output())

    def test_failed_image_upload_still_posts(self):
        self.web.image_ok = False
        self.run_autopost()
        self.assertEqual(len(self.web.posts), 2)
        self.assertNotIn("featured_media", self.web.posts[0])
        self.assertIn("Posting without an image", self.output())

    def test_featured_image_can_be_turned_off(self):
        self.write_config(featured_image=False, author_id=4, post_status="publish")
        self.run_autopost()
        self.assertEqual(self.web.media_uploads, 0)
        self.assertEqual(self.web.posts[0]["author"], 4)
        self.assertEqual(self.web.posts[0]["status"], "publish")

    def test_bad_ai_answer_is_retried_then_item_left_for_later(self):
        self.web.ai_text = "Sorry, I cannot help with that."
        self.assertEqual(self.run_autopost(), 1)   # nothing posted at all -> exit code 1
        self.assertEqual(self.web.ai_calls, 4)     # 2 items x (1 try + 1 retry)
        self.assertEqual(self.web.posts, [])
        self.assertNotIn("guid-0", self.seen())

    def test_wordpress_rejection_does_not_crash(self):
        self.web.post_status_code = 400
        self.assertEqual(self.run_autopost(), 1)
        self.assertIn("category_id", self.output())
        self.assertNotIn("guid-0", self.seen())

    def test_wrong_password_stops_before_using_ai(self):
        self.web.wp_auth_ok = False
        self.assertEqual(self.run_autopost(), 1)
        self.assertEqual(self.web.ai_calls, 0)
        self.assertIn("401 = wrong username or Application Password", self.output())

    def test_missing_secrets(self):
        with mock.patch.dict(os.environ, {"WP_APP_PASSWORD": ""}):
            self.assertEqual(self.run_autopost(), 1)
        self.assertIn("Missing secret(s): WP_APP_PASSWORD", self.output())

    def test_max_posts_per_run(self):
        self.write_config(max_posts_per_run=1)
        self.run_autopost()
        self.assertEqual(len(self.web.posts), 1)
        self.assertNotIn("guid-1", self.seen())   # chosen but not reached -> next run

    def test_dry_run_needs_no_secrets_and_changes_nothing(self):
        with mock.patch.dict(os.environ, {k: "" for k in ENV}):
            self.assertEqual(self.run_autopost(dry_run=True), 0)
        self.assertEqual(self.web.ai_calls, 0)
        self.assertEqual(self.web.count("GET", WP), 0)
        self.assertEqual(self.seen(), [])
        out = self.output()
        self.assertIn("2 post(s) would be written", out)
        self.assertIn("Story zero", out)
        self.assertIn("https://img.example/zero.jpg", out)


class TestFindingTheRestApi(AutopostTestCase):
    def test_plain_permalinks_site_uses_rest_route(self):
        self.web.site = "plain"
        self.assertEqual(self.run_autopost(), 0)
        self.assertEqual(len(self.web.posts), 2)
        self.assertEqual(self.web.count("POST", r"\?rest_route=/wp/v2/posts$"), 2)
        self.assertIn("REST API is at https://mysite.example/?rest_route=/", self.output())

    def test_wordpress_in_a_subfolder_is_found(self):
        self.web.site = "blog"
        self.assertEqual(self.run_autopost(), 0)
        self.assertEqual(self.web.count("POST", r"/blog/wp-json/wp/v2/posts$"), 2)

    def test_real_error_is_shown_after_finding_the_api(self):
        # /wp-json/ gives a server 404, but ?rest_route= reaches WordPress, which says "wrong password".
        self.web.site = "plain"
        self.web.wp_auth_ok = False
        self.assertEqual(self.run_autopost(), 1)
        out = self.output()
        self.assertIn("401 = wrong username or Application Password", out)
        self.assertNotIn("404", out)

    def test_no_wordpress_at_this_address(self):
        self.web.site = "none"
        self.assertEqual(self.run_autopost(), 1)
        self.assertEqual(self.web.ai_calls, 0)
        out = self.output()
        self.assertIn("No WordPress REST API was found at https://mysite.example", out)
        self.assertIn("does not look like a WordPress site", out)
        self.assertIn("Settings > Permalinks", out)
        self.assertIn("the web server said", out)


class TestConnection(AutopostTestCase):
    def test_success(self):
        self.web.ai_text = "OK"
        self.assertEqual(autopost.test_connection(), 0)
        out = self.output()
        self.assertIn("SUCCESS - logged in as 'Editor'", out)
        self.assertIn("id 5", out)          # category list for feeds.json
        self.assertIn("the AI answered: OK", out)

    def test_wrong_password(self):
        self.web.wp_auth_ok = False
        self.web.ai_text = "OK"
        self.assertEqual(autopost.test_connection(), 1)
        self.assertIn("401 = wrong username or Application Password", self.output())

    def test_bad_ai_key(self):
        self.web.ai_status = [401]
        self.assertEqual(autopost.test_connection(), 1)
        self.assertIn("AI API key is wrong", self.output())


if __name__ == "__main__":
    unittest.main()
