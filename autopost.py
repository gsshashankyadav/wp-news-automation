#!/usr/bin/env python3
"""
autopost.py - free, automatic news posting for a WordPress site.

Every run it:
  1. reads the RSS feeds listed in feeds.json (each feed -> one WordPress category)
  2. finds items it has never processed before (remembered in seen.json)
  3. asks a free AI API (Google Gemini by default, or Groq) to write an ORIGINAL
     article from the headline + summary
  4. posts the article to WordPress through the REST API (as a draft by default)
  5. remembers the item so it is never posted twice

Usage:
  python autopost.py                    normal run (needs the secrets below)
  python autopost.py --dry-run          only show what WOULD be posted (no AI, no WordPress, no secrets)
  python autopost.py --test-connection  check the WordPress login and the AI key

Secrets (environment variables only, never written in the code):
  WP_URL, WP_USER, WP_APP_PASSWORD, AI_API_KEY
  optional: AI_BASE_URL, AI_MODEL  (empty values mean "use the default")
"""
from __future__ import annotations

import argparse
import calendar
import html
import json
import logging
import mimetypes
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import feedparser
import requests

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "feeds.json"
DEFAULT_SEEN = BASE_DIR / "seen.json"

DEFAULT_AI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_AI_MODEL = "gemini-2.5-flash"

# Some web hosts block the default "python-requests" User-Agent, so look like a browser.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

MAX_SEEN = 5000                      # how many processed ids seen.json keeps
HTTP_TIMEOUT = 30                    # seconds for feeds / WordPress / images
AI_TIMEOUT = 120                     # seconds for one AI answer
MAX_IMAGE_BYTES = 10 * 1024 * 1024   # skip featured images bigger than 10 MB
MAX_SUMMARY_CHARS = 4000             # how much of the RSS summary is sent to the AI

DEFAULT_SETTINGS = {
    "post_status": "draft",
    "language": "English",
    "article_words": 400,
    "max_posts_per_run": 6,
    "max_posts_per_feed": 2,
    "items_to_check_per_feed": 10,
    "delay_between_posts_sec": 15,
    "skip_backlog_on_first_run": True,
    "author_id": None,
    "featured_image": True,
}
INT_SETTINGS = (
    "article_words", "max_posts_per_run", "max_posts_per_feed",
    "items_to_check_per_feed", "delay_between_posts_sec",
)

log = logging.getLogger("autopost")


# --------------------------------------------------------------------------- errors

class ConfigError(Exception):
    """Something is wrong in feeds.json / seen.json / the secrets."""


class AIError(Exception):
    """The AI call failed or returned something we cannot use."""


class RateLimitError(AIError):
    """The AI provider answered HTTP 429: stop for now and try again next run."""


class AIFormatError(AIError):
    """The AI answered, but not with the JSON article we asked for."""


class WordPressError(Exception):
    """A WordPress REST API call failed."""

    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body if isinstance(body, dict) else {}


# --------------------------------------------------------------------------- helpers

def env(name, default=None):
    """Read an environment variable. Empty or blank values count as "not set"."""
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def clean_text(value):
    """Turn an HTML snippet into plain text (no tags, no entities, single spaces)."""
    if not value:
        return ""
    text = str(value)
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def short(text, limit=300):
    text = clean_text(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def body_snippet(resp, limit=300):
    try:
        return short(resp.text, limit)
    except Exception:
        return ""


# --------------------------------------------------------------------------- config + memory

def load_config(path):
    """Read feeds.json and return (settings, feeds). Bad feed entries are skipped with a warning."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{path.name} is not valid JSON (line {exc.lineno}, column {exc.colno}): {exc.msg}. "
            "Check for a missing comma or quote."
        ) from None

    settings = dict(DEFAULT_SETTINGS)
    for key, value in (data.get("settings") or {}).items():
        if not key.startswith("_"):
            settings[key] = value
    for key in INT_SETTINGS:
        try:
            settings[key] = max(0, int(settings[key]))
        except (TypeError, ValueError):
            log.warning("Setting '%s' must be a number; using the default %s.", key, DEFAULT_SETTINGS[key])
            settings[key] = DEFAULT_SETTINGS[key]
    if settings.get("author_id") in ("", 0, "0"):
        settings["author_id"] = None
    settings["post_status"] = str(settings.get("post_status") or "draft").strip()
    settings["language"] = str(settings.get("language") or "English").strip()

    feeds = []
    for number, feed in enumerate(data.get("feeds") or [], start=1):
        if not isinstance(feed, dict) or not str(feed.get("url") or "").strip():
            log.warning("Feed #%d in %s has no 'url' - skipping it.", number, path.name)
            continue
        url = str(feed["url"]).strip()
        try:
            category_id = int(feed.get("category_id"))
        except (TypeError, ValueError):
            log.warning("Feed #%d (%s) has no valid 'category_id' number - skipping it.", number, url)
            continue
        feeds.append({
            "url": url,
            "category_id": category_id,
            "category_name": str(feed.get("category_name") or f"category {category_id}"),
            "source_name": str(feed.get("source_name") or urlparse(url).netloc or url),
        })
    if not feeds:
        raise ConfigError(f"No usable feeds in {path.name}. Add at least one feed with a url and category_id.")
    return settings, feeds


class SeenStore:
    """The list of item ids already processed, stored in seen.json (oldest first)."""

    def __init__(self, path):
        self.path = Path(path)
        self.ids = self._load()
        self._set = set(self.ids)

    def _load(self):
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "[]")
        except (json.JSONDecodeError, OSError) as exc:
            # Refuse to continue: treating a broken file as empty could cause duplicate posts.
            raise ConfigError(f"{self.path.name} is damaged ({exc}). Fix it or reset it to [] .") from None
        if not isinstance(data, list):
            raise ConfigError(f"{self.path.name} must contain a JSON list, like [] .")
        return [str(x) for x in data]

    def __contains__(self, item_id):
        return item_id in self._set

    def __len__(self):
        return len(self.ids)

    def add(self, item_id):
        if item_id not in self._set:
            self.ids.append(item_id)
            self._set.add(item_id)

    def save(self):
        self.ids = self.ids[-MAX_SEEN:]
        self._set = set(self.ids)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.ids, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- RSS

def fetch_feed(url):
    """Download and parse one RSS/Atom feed. Returns the list of entries."""
    resp = requests.get(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
        },
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code >= 400:
        raise ValueError(f"the feed address answered HTTP {resp.status_code}")
    parsed = feedparser.parse(resp.content, response_headers={"content-location": resp.url or url})
    if not parsed.entries and parsed.get("bozo"):
        raise ValueError(f"this does not look like a valid RSS feed ({parsed.get('bozo_exception')})")
    return list(parsed.entries)


def entry_timestamp(entry):
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return calendar.timegm(t) if t else None


def newest_first(entries):
    """Sort by date when every entry has one; otherwise keep the feed's own order (usually newest first)."""
    stamps = [entry_timestamp(e) for e in entries]
    if entries and all(s is not None for s in stamps):
        return [e for _, e in sorted(zip(stamps, entries), key=lambda pair: pair[0], reverse=True)]
    return list(entries)


def item_id(entry):
    """Stable id for dedupe: the RSS id/guid, else the link, else the title."""
    for key in ("id", "guid", "link", "title"):
        value = entry.get(key)
        if value and str(value).strip():
            return str(value).strip()
    return None


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def find_image(entry):
    """Return the URL of the item's image (media:content, media:thumbnail or enclosure), or None."""
    best = None
    for media in entry.get("media_content") or []:
        url = media.get("url")
        medium = (media.get("medium") or "").lower()
        mtype = (media.get("type") or "").lower()
        if not url or (medium and medium != "image") or (mtype and not mtype.startswith("image/")):
            continue
        width = _int(media.get("width"))
        if best is None or width > best[0]:
            best = (width, url)
    if best:
        return best[1]

    for thumb in entry.get("media_thumbnail") or []:
        url = thumb.get("url")
        width = _int(thumb.get("width"))
        if url and (best is None or width > best[0]):
            best = (width, url)
    if best:
        return best[1]

    for enclosure in entry.get("enclosures") or []:
        url = enclosure.get("href") or enclosure.get("url")
        etype = (enclosure.get("type") or "").lower()
        if url and (etype.startswith("image/") or re.search(r"\.(jpe?g|png|gif|webp)(\?|$)", url, re.I)):
            return url
    return None


def make_item(entry, feed):
    summary = entry.get("summary") or entry.get("description") or ""
    if not clean_text(summary) and entry.get("content"):
        summary = entry["content"][0].get("value", "")
    return {
        "id": item_id(entry),
        "title": clean_text(entry.get("title")),
        "summary": clean_text(summary)[:MAX_SUMMARY_CHARS],
        "link": str(entry.get("link") or "").strip(),
        "image": find_image(entry),
        "feed": feed,
    }


# --------------------------------------------------------------------------- AI

def explain_ai_status(status):
    return {
        400: "bad request - often a wrong model name (check AI_MODEL)",
        401: "the AI API key is wrong (check AI_API_KEY, and that it belongs to the provider in AI_BASE_URL)",
        403: "the AI API key is not allowed to use this API/model (check AI_API_KEY and AI_BASE_URL)",
        404: "not found - check AI_BASE_URL and AI_MODEL",
    }.get(status, "the AI provider had a problem")


class AIClient:
    """Talks to any OpenAI-compatible /chat/completions endpoint with plain requests."""

    def __init__(self, api_key, base_url=None, model=None):
        self.api_key = api_key
        self.base_url = base_url or DEFAULT_AI_BASE_URL
        self.model = model or DEFAULT_AI_MODEL
        self.endpoint = self.base_url.rstrip("/") + "/chat/completions"

    def chat(self, messages, temperature=0.7, retries=2):
        payload = {"model": self.model, "messages": messages, "temperature": temperature}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        for attempt in range(retries + 1):
            try:
                resp = requests.post(self.endpoint, json=payload, headers=headers, timeout=AI_TIMEOUT)
            except requests.RequestException as exc:
                if attempt < retries:
                    log.warning("  AI connection problem (%s) - retrying...", exc)
                    time.sleep(5 * (attempt + 1))
                    continue
                raise AIError(f"could not reach the AI API at {self.endpoint}: {exc}") from None

            if resp.status_code == 429:
                raise RateLimitError(f"HTTP 429 rate limit / quota reached: {body_snippet(resp, 200)}")
            if resp.status_code >= 500 and attempt < retries:
                log.warning("  AI server busy (HTTP %s) - retrying...", resp.status_code)
                time.sleep(5 * (attempt + 1))
                continue
            if resp.status_code != 200:
                raise AIError(
                    f"HTTP {resp.status_code} = {explain_ai_status(resp.status_code)}. Details: {body_snippet(resp)}"
                )
            try:
                return resp.json()["choices"][0]["message"]["content"] or ""
            except (ValueError, KeyError, IndexError, TypeError):
                raise AIError(f"unexpected answer from the AI API: {body_snippet(resp)}") from None
        raise AIError("the AI API kept failing")  # pragma: no cover


def build_messages(item, settings):
    words = settings["article_words"]
    language = settings["language"]
    summary = item["summary"] or "(no summary was provided - only the headline is known)"
    prompt = f"""Write an original news article in {language}, based ONLY on the source headline and summary below.

RULES:
- Use ONLY the facts in the headline and summary. Never invent quotes, numbers, names, dates, places or events.
- Do not copy sentences from the source. Rewrite everything fully in your own words.
- Write a new, original headline. Do not reuse the original headline.
- Aim for about {words} words. If the facts are thin, write a short article of 150-250 words instead of padding it with filler, background guesses or speculation.
- Use a neutral, factual journalistic tone. No opinions, no clickbait.
- Format the content as simple HTML using only <p>, <h2>, <ul> and <li> tags. Do not use <h1>, <html>, <head> or <body>. Do not repeat the headline inside the content.
- Do not mention these instructions, the source website, or that you are an AI.
- Return ONLY a JSON object, with no text before or after it, in exactly this format:
{{"title": "new headline", "content": "<p>article HTML</p>", "excerpt": "one or two plain-text sentences summarising the article", "tags": ["tag one", "tag two", "tag three"]}}
- "tags" must be a list of 3 to 5 short topic tags.

SOURCE HEADLINE: {item["title"]}

SOURCE SUMMARY: {summary}
"""
    return [
        {"role": "system", "content": "You are a careful, professional news writer. You never invent facts and you always answer with valid JSON."},
        {"role": "user", "content": prompt},
    ]


def sanitize_content(content):
    """Keep the article HTML simple and safe."""
    content = content.strip()
    content = re.sub(r"(?is)<(script|style|iframe|head)\b.*?</\1>", "", content)
    content = re.sub(r"(?is)</?(html|body|!doctype)[^>]*>", "", content)
    content = re.sub(r"(?i)<h1(\s[^>]*)?>", "<h2>", content)
    content = re.sub(r"(?i)</h1>", "</h2>", content)
    if not re.search(r"<(p|h2|ul|li)\b", content, re.I):
        # The AI sent plain text: wrap each paragraph in <p>.
        parts = [p.strip() for p in re.split(r"\n\s*\n", content) if p.strip()]
        content = "\n".join(f"<p>{html.escape(p)}</p>" for p in parts)
    return content.strip()


def parse_ai_json(text):
    """Pull the JSON article out of the AI answer, even if it is wrapped in ```json fences or extra text."""
    if not text or not text.strip():
        raise AIFormatError("the AI returned an empty answer")
    cleaned = text.strip()
    cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise AIFormatError(f"the AI answer contains no JSON object: {short(text, 200)}")
    try:
        data = json.loads(cleaned[start:end + 1], strict=False)  # strict=False allows raw newlines in strings
    except json.JSONDecodeError as exc:
        raise AIFormatError(f"the AI answer is not valid JSON ({exc.msg})") from None
    if not isinstance(data, dict):
        raise AIFormatError("the AI answer is not a JSON object")

    title = clean_text(data.get("title"))
    content = sanitize_content(str(data.get("content") or ""))
    if not title or not clean_text(content):
        raise AIFormatError("the AI answer is missing the title or the content")
    excerpt = clean_text(data.get("excerpt")) or short(content, 200)

    raw_tags = data.get("tags") or []
    if isinstance(raw_tags, str):
        raw_tags = raw_tags.split(",")
    tags, lowered = [], set()
    for tag in raw_tags if isinstance(raw_tags, list) else []:
        tag = clean_text(tag).strip("#").strip()[:50]
        if tag and tag.lower() not in lowered:
            tags.append(tag)
            lowered.add(tag.lower())
    return {"title": title, "content": content, "excerpt": excerpt, "tags": tags[:5]}


def write_article(item, settings, ai):
    """Ask the AI for an article. A broken (non-JSON) answer is retried once."""
    messages = build_messages(item, settings)
    try:
        return parse_ai_json(ai.chat(messages))
    except AIFormatError as exc:
        log.warning("  The AI answer was not usable (%s) - asking once more...", exc)
        return parse_ai_json(ai.chat(messages))


# --------------------------------------------------------------------------- WordPress

def normalize_wp_url(url):
    url = url.strip()
    if not re.match(r"(?i)^https?://", url):
        url = "https://" + url
    url = re.sub(r"(?i)/(wp-json|wp-admin|wp-login\.php).*$", "", url.rstrip("/"))
    return url.rstrip("/")


def explain_wp_error(status, body, raw_text=""):
    wp_code = body.get("code", "") if isinstance(body, dict) else ""
    wp_msg = clean_text(body.get("message", "")) if isinstance(body, dict) else short(raw_text, 200)
    if status == 401 and wp_code == "rest_not_logged_in":
        hint = ("401 = WordPress did not receive any login at all. Your host is probably removing the "
                "'Authorization' header before it reaches WordPress (see README > Troubleshooting).")
    elif status == 401 and wp_code in ("invalid_username", "invalid_email"):
        hint = ("401 = wrong username or Application Password: WordPress does not know this username. "
                "Use the username you log in with.")
    elif status == 401 and wp_code == "incorrect_password":
        hint = ("401 = wrong username or Application Password: the username is right, the password is wrong. "
                "Use an Application Password (24 letters/numbers), NOT your normal login password, "
                "or create a new one.")
    elif status == 401:
        hint = ("401 = wrong username or Application Password. Use the Application Password "
                "(24 letters in groups of 4), NOT your normal login password. If both are right, your "
                "host may be removing the 'Authorization' header (see README > Troubleshooting).")
    elif status == 403:
        hint = ("403 = logged in but not allowed. The WordPress user needs the Administrator or Editor "
                "role, or a security plugin / firewall (Wordfence, Cloudflare, your host) is blocking the REST API.")
    elif status == 404 and not isinstance(body, dict):
        hint = "404 = the web server found nothing at this address (this answer did not come from WordPress)."
    elif status == 404:
        hint = ("404 = not found. Check WP_URL (like https://example.com, without /wp-admin) "
                "and that the REST API is not disabled by a plugin.")
    elif status == 400:
        hint = ("400 = WordPress rejected the data. Often a category_id or author_id in feeds.json "
                "that does not exist on your site.")
    elif status and status >= 500:
        hint = f"{status} = an error on your website/host. Try again later or check your hosting error log."
    else:
        hint = f"HTTP {status}"
    details = " ".join(x for x in (wp_code, wp_msg) if x)
    who = "WordPress" if isinstance(body, dict) else "the web server"
    return f"{hint}" + (f" ({who} said: {details})" if details else "")


def api_root_from_page(resp):
    """The REST API address a WordPress page announces (Link header or <link rel="https://api.w.org/">), or None."""
    root = resp.links.get("https://api.w.org/", {}).get("url")
    if not root:
        text = resp.text[:300000] if resp.text else ""
        for tag in re.findall(r"<link\b[^>]*>", text, re.I):
            match = re.search(r"""href=["']([^"']+)""", tag)
            if "api.w.org" in tag and match:
                root = html.unescape(match.group(1))
                break
    if root and not root.endswith("/"):
        root += "/"
    return root


class WordPress:
    """Minimal WordPress REST API client using an Application Password."""

    def __init__(self, url, user, app_password):
        self.base = normalize_wp_url(url)
        self.session = requests.Session()
        self.session.auth = (user, app_password)
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        # Where the REST API lives. me() changes it if the site uses another address
        # (e.g. https://example.com/?rest_route=/ on sites with "Plain" permalinks).
        self.api_root = self.base + "/wp-json/"
        self._tag_cache = {}

    def _url(self, path):
        return f"{self.api_root}wp/v2{path}"

    def request(self, method, path, **kwargs):
        kwargs.setdefault("timeout", HTTP_TIMEOUT)
        try:
            resp = self.session.request(method, self._url(path), **kwargs)
        except requests.RequestException as exc:
            raise WordPressError(f"could not connect to {self.base}: {exc}") from None
        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.status_code >= 400:
            message = explain_wp_error(resp.status_code, body, resp.text)
            if resp.history and urlparse(resp.url).netloc != urlparse(self.base).netloc:
                message += f" Note: {self.base} redirects to {resp.url} - put that address in WP_URL."
            raise WordPressError(message, resp.status_code, body)
        if body is None:
            raise WordPressError(
                f"WordPress answered with a web page instead of JSON ({body_snippet(resp, 150)}). "
                "Check WP_URL, and whether a security/caching plugin or your host blocks the REST API.",
                resp.status_code,
            )
        return body

    def me(self):
        """GET /users/me - proves the login works.

        If the usual /wp-json/ address does not answer like WordPress, ask the homepage where
        the REST API really is (subfolder installs, "Plain" permalinks, www redirects) and use that."""
        try:
            return self.request("GET", "/users/me", params={"context": "edit"})
        except WordPressError as exc:
            if exc.status is None or exc.body:
                raise  # no connection at all, or WordPress itself answered (e.g. 401): report as is
            first_error = exc

        original_root = self.api_root
        discovered, homepage_note = self.discover_api_root()
        roots = []
        for root in (discovered, self.base + "/?rest_route=/"):
            if root and root != original_root and root not in roots:
                roots.append(root)
        for root in roots:
            self.api_root = root
            try:
                user = self.request("GET", "/users/me", params={"context": "edit"})
                log.info("  Note: this site's REST API is at %s - using that address.", root)
                return user
            except WordPressError as exc:
                if exc.body:
                    raise  # WordPress answered at this address (e.g. 401), so this is the real problem
        self.api_root = original_root
        raise WordPressError(
            f"No WordPress REST API was found at {self.base}. {homepage_note}. What to check: "
            f"(1) WP_URL must be exactly the address of your site's homepage as your browser shows it "
            f"(https, with or without www). "
            f"(2) If WordPress is installed in a folder, include it, like https://example.com/blog . "
            f"(3) In WordPress go to Settings > Permalinks and click 'Save Changes' - this repairs the "
            f"/wp-json/ address on LiteSpeed/Apache hosts. "
            f"(4) Open {self.base}/wp-json/ in your browser: it should show text starting with {{\"name\":. "
            f"[First answer: {first_error}]",
            first_error.status,
        ) from None

    def discover_api_root(self):
        """Read the REST API address the homepage announces (Link header or <link rel="https://api.w.org/">).
        Returns (api_root or None, a sentence describing what the homepage showed)."""
        home = self.base + "/"
        try:
            resp = requests.get(home, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            return None, f"Opening the homepage {home} failed ({exc})"
        text = resp.text[:300000] if resp.text else ""
        root = api_root_from_page(resp)
        moved = urlparse(resp.url or home).netloc != urlparse(home).netloc
        if moved:
            note = f"The homepage redirects to {resp.url} - put that address in WP_URL"
        elif root:
            note = f"The homepage says its REST API is at {root}, but it did not answer there"
        elif "wp-content" in text:
            note = ("The homepage looks like WordPress but does not announce a REST API address "
                    "(a security plugin may be hiding or switching off the REST API)")
        else:
            note = f"The homepage answered HTTP {resp.status_code} and does not look like a WordPress site"
        return root, note

    def categories(self):
        return self.request("GET", "/categories", params={"per_page": 100, "orderby": "id", "order": "asc"})

    def tag_id(self, name):
        """Find a tag by name, or create it. Results are cached for this run."""
        key = name.lower()
        if key in self._tag_cache:
            return self._tag_cache[key]
        found = self.request("GET", "/tags", params={"search": name, "per_page": 100})
        for tag in found if isinstance(found, list) else []:
            if html.unescape(str(tag.get("name", ""))).lower() == key:
                self._tag_cache[key] = tag["id"]
                return tag["id"]
        try:
            created = self.request("POST", "/tags", json={"name": name})
            tag_id = created["id"]
        except WordPressError as exc:
            if exc.body.get("code") != "term_exists":
                raise
            tag_id = exc.body.get("data", {}).get("term_id")
        self._tag_cache[key] = tag_id
        return tag_id

    def tag_ids(self, names):
        ids = []
        for name in names:
            try:
                tag_id = self.tag_id(name)
                if tag_id:
                    ids.append(tag_id)
            except WordPressError as exc:
                log.warning("  Could not create tag '%s' (%s) - continuing without it.", name, exc)
        return ids

    def upload_image(self, image_url, alt_text=""):
        """Download an image and upload it to the media library. Returns the media id, or None on any problem."""
        try:
            resp = requests.get(image_url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT, stream=True)
            if resp.status_code >= 400:
                raise ValueError(f"image download answered HTTP {resp.status_code}")
            data = b""
            for chunk in resp.iter_content(64 * 1024):
                data += chunk
                if len(data) > MAX_IMAGE_BYTES:
                    raise ValueError("image is larger than 10 MB")
            if not data:
                raise ValueError("image is empty")

            path = unquote(urlparse(image_url).path)
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not ctype.startswith("image/"):
                ctype = mimetypes.guess_type(path)[0] or ""
            if not ctype.startswith("image/") or ctype == "image/svg+xml":
                raise ValueError(f"not a supported image type ({ctype or 'unknown'})")

            ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                   "image/webp": ".webp"}.get(ctype) or mimetypes.guess_extension(ctype) or ".jpg"
            stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(path).stem).strip("-")[:60] or "news-image"
            media = self.request(
                "POST", "/media", data=data, timeout=60,
                headers={"Content-Type": ctype, "Content-Disposition": f'attachment; filename="{stem}{ext}"'},
            )
            media_id = media.get("id")
            if media_id and alt_text:
                try:
                    self.request("POST", f"/media/{media_id}", json={"alt_text": alt_text})
                except WordPressError:
                    pass  # alt text is a nice-to-have
            return media_id
        except Exception as exc:
            log.warning("  Could not add the featured image (%s). Posting without an image.", exc)
            return None

    def create_post(self, payload):
        return self.request("POST", "/posts", json=payload)


# --------------------------------------------------------------------------- the run

def credit_line(item):
    name = html.escape(item["feed"]["source_name"])
    if not item["link"]:
        return f"\n<p>Source: {name}</p>"
    link = html.escape(item["link"], quote=True)
    return f'\n<p>Source: <a href="{link}" rel="nofollow noopener" target="_blank">{name}</a></p>'


def publish_item(item, settings, ai, wp):
    log.info("  Asking the AI to write the article...")
    article = write_article(item, settings, ai)
    log.info("  AI wrote: \"%s\" (%d words, tags: %s)", article["title"],
             len(clean_text(article["content"]).split()), ", ".join(article["tags"]) or "none")

    payload = {
        "title": article["title"],
        "content": article["content"] + credit_line(item),
        "excerpt": article["excerpt"],
        "status": settings["post_status"],
        "categories": [item["feed"]["category_id"]],
    }
    if settings.get("author_id"):
        payload["author"] = int(settings["author_id"])
    if article["tags"]:
        payload["tags"] = wp.tag_ids(article["tags"])
    if settings.get("featured_image") and item["image"]:
        log.info("  Uploading the featured image...")
        media_id = wp.upload_image(item["image"], alt_text=article["title"])
        if media_id:
            payload["featured_media"] = media_id

    log.info("  Sending the post to WordPress...")
    return wp.create_post(payload)


def collect_candidates(feeds, settings, seen):
    """Read every feed. Returns (plan, feeds_ok) where plan is a list of
    (feed, items_to_post, items_to_skip) - nothing is changed yet."""
    plan, run_ids, feeds_ok = [], set(), 0
    for feed in feeds:
        log.info("Reading feed: %s -> category '%s' (id %s)", feed["source_name"], feed["category_name"], feed["category_id"])
        log.info("  %s", feed["url"])
        try:
            entries = fetch_feed(feed["url"])
        except Exception as exc:
            log.error("  Could not read this feed, skipping it: %s", exc)
            continue
        feeds_ok += 1

        items = []
        for entry in newest_first(entries)[: settings["items_to_check_per_feed"]]:
            try:
                item = make_item(entry, feed)
            except Exception as exc:
                log.warning("  Skipping one broken item in this feed: %s", exc)
                continue
            if not item["id"] or not item["title"]:
                log.warning("  Skipping an item with no id/title.")
                continue
            items.append(item)

        new_items = [it for it in items if it["id"] not in seen and it["id"] not in run_ids]
        # A feed none of whose items we have seen is new (or this is the very first run):
        # only take the newest few and mark the older backlog as seen.
        backlog = settings["skip_backlog_on_first_run"] and items and not any(it["id"] in seen for it in items)
        limit = settings["max_posts_per_feed"]
        to_post = new_items[:limit]
        to_skip = new_items[limit:] if backlog else []
        run_ids.update(it["id"] for it in new_items)

        msg = f"  Found {len(entries)} items, checked {len(items)}, new: {len(new_items)}, will try: {len(to_post)}"
        if to_skip:
            msg += f", older backlog marked as seen without posting: {len(to_skip)}"
        log.info(msg)
        plan.append((feed, to_post, to_skip))
    return plan, feeds_ok


def round_robin(lists, offset=0):
    """Take one item from each feed in turn so every feed gets a fair share of the run."""
    if not lists:
        return []
    offset %= len(lists)
    lists = [list(x) for x in lists[offset:] + lists[:offset]]
    out = []
    while any(lists):
        for items in lists:
            if items:
                out.append(items.pop(0))
    return out


def missing_secrets(names=("WP_URL", "WP_USER", "WP_APP_PASSWORD", "AI_API_KEY")):
    return [name for name in names if not env(name)]


def make_clients():
    wp = WordPress(env("WP_URL"), env("WP_USER"), env("WP_APP_PASSWORD"))
    ai = AIClient(env("AI_API_KEY"), env("AI_BASE_URL"), env("AI_MODEL"))
    return wp, ai


def run(config_path=DEFAULT_CONFIG, seen_path=DEFAULT_SEEN, dry_run=False):
    """One complete run. Returns the process exit code (0 = fine)."""
    log.info("=== WordPress auto-poster: %s ===", "DRY RUN (nothing will be posted)" if dry_run else "normal run")
    try:
        settings, feeds = load_config(config_path)
        seen = SeenStore(seen_path)
    except ConfigError as exc:
        log.error("%s", exc)
        return 1
    log.info("Loaded %d feed(s). Posts will be created as '%s'. Remembered items: %d.",
             len(feeds), settings["post_status"], len(seen))
    if len(seen) == 0 and settings["skip_backlog_on_first_run"]:
        log.info("First run: only the newest %d item(s) per feed will be used; older ones are marked as seen.",
                 settings["max_posts_per_feed"])

    wp = ai = None
    if not dry_run:
        missing = missing_secrets()
        if missing:
            log.error("Missing secret(s): %s. Add them as GitHub repository secrets (see README).", ", ".join(missing))
            return 1
        wp, ai = make_clients()
        log.info("AI: model %s at %s", ai.model, ai.base_url)
        log.info("Checking the WordPress login at %s ...", wp.base)
        try:
            user = wp.me()
            log.info("  OK - logged in as '%s'.", user.get("name") or user.get("slug") or "?")
        except WordPressError as exc:
            log.error("  WordPress login failed: %s", exc)
            return 1

    plan, feeds_ok = collect_candidates(feeds, settings, seen)
    if feeds_ok == 0:
        log.error("None of the feeds could be read. Check the feed URLs in feeds.json.")
        return 1

    queue = round_robin([to_post for _, to_post, _ in plan], offset=int(time.time() // 3600))
    waiting = queue[settings["max_posts_per_run"]:]
    queue = queue[: settings["max_posts_per_run"]]
    skipped = [it for _, _, to_skip in plan for it in to_skip]

    if dry_run:
        log.info("")
        log.info("--- DRY RUN RESULT: %d post(s) would be written this run ---", len(queue))
        for number, item in enumerate(queue, start=1):
            log.info("%d. [%s -> %s (id %s)] %s", number, item["feed"]["source_name"],
                     item["feed"]["category_name"], item["feed"]["category_id"], item["title"])
            log.info("   link:    %s", item["link"] or "-")
            log.info("   image:   %s", item["image"] or "none")
            log.info("   summary: %s", short(item["summary"], 160) or "(empty)")
        if waiting:
            log.info("%d more new item(s) would wait for the next run (max_posts_per_run = %d).",
                     len(waiting), settings["max_posts_per_run"])
        if skipped:
            log.info("%d older backlog item(s) would be marked as seen without posting.", len(skipped))
        log.info("Dry run finished. seen.json was NOT changed, nothing was sent to the AI or WordPress.")
        return 0

    for item in skipped:
        seen.add(item["id"])
    if skipped:
        seen.save()

    posted = failed = 0
    rate_limited = False
    for number, item in enumerate(queue, start=1):
        if number > 1 and settings["delay_between_posts_sec"]:
            log.info("Waiting %d seconds before the next post...", settings["delay_between_posts_sec"])
            time.sleep(settings["delay_between_posts_sec"])
        log.info("")
        log.info("[%d/%d] %s (from %s)", number, len(queue), item["title"], item["feed"]["source_name"])
        try:
            post = publish_item(item, settings, ai, wp)
        except RateLimitError as exc:
            log.warning("The AI rate limit was reached (%s).", exc)
            log.warning("Stopping this run now. Progress is saved; the rest will be posted on a later run.")
            rate_limited = True
            break
        except (AIError, WordPressError) as exc:
            log.error("  Failed, will retry on a later run: %s", exc)
            failed += 1
            continue
        except Exception as exc:  # never let one item crash the whole run
            log.error("  Unexpected problem with this item, will retry on a later run: %r", exc)
            failed += 1
            continue
        seen.add(item["id"])
        seen.save()
        posted += 1
        log.info("  DONE - post #%s created as '%s': %s", post.get("id"), post.get("status"), post.get("link", ""))

    log.info("")
    log.info("=== Finished: %d posted, %d failed, %d waiting for a later run%s ===",
             posted, failed, len(waiting) + (len(queue) - posted - failed),
             " (stopped early: AI rate limit)" if rate_limited else "")
    return 1 if failed and not posted else 0


def test_connection():
    """--test-connection: check the WordPress login and make one tiny AI call."""
    log.info("=== Connection test ===")
    ok = True
    missing = missing_secrets()
    if missing:
        log.error("Missing environment variable(s): %s", ", ".join(missing))
        ok = False

    if env("WP_URL") and env("WP_USER") and env("WP_APP_PASSWORD"):
        wp = WordPress(env("WP_URL"), env("WP_USER"), env("WP_APP_PASSWORD"))
        log.info("WordPress: checking login at %s ...", wp.base)
        try:
            user = wp.me()
            roles = user.get("roles") or []
            log.info("  SUCCESS - logged in as '%s' (user id %s%s).", user.get("name"), user.get("id"),
                     f", role: {', '.join(roles)}" if roles else "")
            if roles and not set(roles) & {"administrator", "editor"}:
                log.warning("  This user is not an Administrator or Editor - creating tags or publishing may fail.")
            try:
                cats = wp.categories()
                log.info("  Your categories (use these numbers as category_id in feeds.json):")
                for cat in cats:
                    log.info("    id %-6s %s", cat.get("id"), html.unescape(str(cat.get("name", ""))))
            except WordPressError as exc:
                log.warning("  Could not list categories: %s", exc)
        except WordPressError as exc:
            log.error("  FAILED - %s", exc)
            ok = False

    if env("AI_API_KEY"):
        ai = AIClient(env("AI_API_KEY"), env("AI_BASE_URL"), env("AI_MODEL"))
        log.info("AI: testing model '%s' at %s ...", ai.model, ai.base_url)
        try:
            answer = ai.chat([{"role": "user", "content": "Reply with just the word OK."}], temperature=0, retries=0)
            log.info("  SUCCESS - the AI answered: %s", short(answer, 80) or "(empty)")
        except RateLimitError as exc:
            log.warning("  The key works, but the rate limit / free quota is used up right now: %s", exc)
        except AIError as exc:
            log.error("  FAILED - %s", exc)
            ok = False

    log.info("=== %s ===", "All good!" if ok else "Some checks FAILED - read the messages above")
    return 0 if ok else 1


class _Formatter(logging.Formatter):
    PREFIX = {logging.WARNING: "WARNING: ", logging.ERROR: "ERROR: "}

    def format(self, record):
        record.prefix = self.PREFIX.get(record.levelno, "")
        return super().format(record)


def setup_logging():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")  # never crash on odd characters in headlines
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_Formatter("%(asctime)s %(prefix)s%(message)s", datefmt="%H:%M:%S"))
    log.handlers[:] = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False


def main(argv=None):
    parser = argparse.ArgumentParser(description="Turn RSS news items into original WordPress posts using a free AI API.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="read the feeds and show what would be posted (no AI, no WordPress, no secrets needed)")
    mode.add_argument("--test-connection", action="store_true",
                      help="check the WordPress login and the AI key, then exit")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="path to feeds.json")
    parser.add_argument("--seen", default=str(DEFAULT_SEEN), help="path to seen.json")
    args = parser.parse_args(argv)

    setup_logging()
    if args.test_connection:
        return test_connection()
    return run(args.config, args.seen, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
