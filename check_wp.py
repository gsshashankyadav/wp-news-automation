#!/usr/bin/env python3
"""
check_wp.py - check, step by step, that this project can talk to your WordPress site.

Run it on your own computer (after "pip install -r requirements.txt"):

    python check_wp.py

It asks for your site address, username and Application Password (nothing is saved),
then checks:
  1. the homepage opens and looks like WordPress
  2. the REST API answers, and at which address
  3. the Application Password login works
  4. the user may create posts, upload images and create tags
  5. your categories and their ID numbers (for feeds.json)

It only READS from your site. It never creates, changes or deletes anything.

To skip the questions, set the environment variables WP_URL, WP_USER and WP_APP_PASSWORD
first (the same names as the GitHub secrets), or pass --url and --user.
"""
from __future__ import annotations

import argparse
import getpass
import html
import re
import sys
from urllib.parse import urlparse

import requests

import autopost
from autopost import USER_AGENT, WordPressError, api_root_from_page, env, normalize_wp_url, short

TIMEOUT = 30

# capability -> (what it is needed for, is it required?)
CAPABILITIES = [
    ("edit_posts", "create posts", True),
    ("publish_posts", 'publish posts (only needed with "post_status": "publish")', False),
    ("upload_files", "upload featured images", False),
    ("manage_categories", "create new tags", False),
]


class Report:
    """Prints one line per check and remembers whether anything failed."""

    def __init__(self):
        self.failed = False

    def step(self, number, total, text):
        print(f"\n[{number}/{total}] {text}")

    def ok(self, text):
        print(f"  OK    {text}")

    def info(self, text):
        print(f"  INFO  {text}")

    def no(self, text):
        print(f"  NO    {text}")

    def warn(self, text):
        print(f"  WARN  {text}")

    def fail(self, text):
        self.failed = True
        print(f"  FAIL  {text}")

    def tip(self, text):
        print(f"        -> {text}")


def check_password_format(password, r):
    compact = re.sub(r"\s+", "", password)
    if re.fullmatch(r"[A-Za-z0-9]{24}", compact):
        print("Password:     24 characters - looks like an Application Password")
        return
    print(f"Password:     {len(compact)} characters")
    r.warn("This does not look like an Application Password (those have 24 letters/numbers, "
           "shown as 6 groups of 4). Your normal login password will NOT work here.")
    r.tip("Create one: WordPress dashboard > Users > Profile > Application Passwords > Add New.")


def check_homepage(base, r):
    """Step 1. Returns the REST API address the homepage announces, or None."""
    home = base + "/"
    try:
        resp = requests.get(home, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        r.fail(f"Could not open {home} ({exc.__class__.__name__}).")
        r.tip("Check the spelling of the address, and that the site opens in your browser.")
        return None

    server = resp.headers.get("Server") or "unknown"
    if resp.history and urlparse(resp.url).netloc != urlparse(home).netloc:
        final = f"{urlparse(resp.url).scheme}://{urlparse(resp.url).netloc}"
        r.warn(f"{home} redirects to {resp.url}")
        r.tip(f"Use {final} as WP_URL - logins can get lost on redirects.")
    if resp.status_code >= 400:
        r.warn(f"The homepage answered HTTP {resp.status_code} (web server: {server}).")
    else:
        r.ok(f"The homepage opens (HTTP {resp.status_code}, web server: {server}).")

    root = api_root_from_page(resp)
    if root:
        r.ok("It looks like a WordPress site.")
        r.info(f"It says its REST API is at {root}")
    elif "wp-content" in (resp.text or ""):
        r.ok("It looks like a WordPress site.")
        r.warn("But it does not announce a REST API address (a security plugin may hide or switch it off).")
    else:
        r.warn("It does not look like a WordPress site.")
        r.tip("Is this the right address? If WordPress is in a folder, include it, like https://example.com/blog")
    return root


def describe_failure(resp):
    try:
        data = resp.json()
    except ValueError:
        data = None
    if isinstance(data, dict) and data.get("code"):
        return f"HTTP {resp.status_code} from WordPress: {data.get('code')} - {short(data.get('message', ''), 150)}"
    server = resp.headers.get("Server") or "unknown"
    if resp.status_code == 200:
        return f"answered with a web page, not the REST API (web server: {server}): {short(resp.text, 100)}"
    return f"HTTP {resp.status_code} from the web server ({server}), not from WordPress: {short(resp.text, 100)}"


def check_rest_api(base, announced, r):
    """Step 2. Returns (working api root, the REST index data) or (None, None)."""
    candidates = []
    # Same order as the auto-poster: the usual address first, then what the homepage announced.
    for root in (base + "/wp-json/", announced, base + "/?rest_route=/"):
        if root and root not in candidates:
            candidates.append(root)

    for root in candidates:
        try:
            resp = requests.get(root, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=TIMEOUT)
        except requests.RequestException as exc:
            r.no(f"{root} - could not connect ({exc.__class__.__name__})")
            continue
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code == 200 and isinstance(data, dict) and "wp/v2" in (data.get("namespaces") or []):
            r.ok(f'{root} works - site name: "{html.unescape(str(data.get("name") or "?"))}"')
            return root, data
        r.no(f"{root} - {describe_failure(resp)}")

    r.fail("No working WordPress REST API address was found.")
    r.tip("In WordPress go to Settings > Permalinks and click 'Save Changes' (repairs /wp-json/ on LiteSpeed/Apache).")
    r.tip("Make sure no plugin switches off the REST API (e.g. 'Disable REST API', some security plugins).")
    r.tip("Check that the address is exactly your site's homepage, including any folder.")
    return None, None


def check_rest_details(base, root, data, r):
    """Extra notes about the REST API that was found. Returns the address to use as WP_URL."""
    if "rest_route=" in root:
        r.warn("/wp-json/ does not work, but ?rest_route= does. The auto-poster handles this by itself.")
        r.tip("To fix /wp-json/ anyway: Settings > Permalinks > 'Save Changes'.")

    recommended = base
    home = data.get("home") or data.get("url")
    if home and normalize_wp_url(home) != base:
        recommended = normalize_wp_url(home)
        r.warn(f"WordPress says its own address is {home}, but you entered {base}.")
        r.tip(f"Use {recommended} as WP_URL.")

    auth = data.get("authentication")
    if isinstance(auth, dict) and "application-passwords" in auth:
        r.ok("Application Passwords are switched on.")
    else:
        r.warn("The site does not announce Application Passwords - they may be switched off.")
        r.tip("They need HTTPS. Some security plugins have a setting that turns them off.")
    return recommended


def check_login(wp, r):
    """Step 3. Returns the /users/me data, or None."""
    try:
        me = wp.request("GET", "/users/me", params={"context": "edit"})
    except WordPressError as exc:
        r.fail(str(exc))
        if exc.body.get("code") == "rest_not_logged_in":
            if "rest_route=" in wp.api_root:
                # "Plain" permalinks: WordPress has not written its .htaccess rules, which include
                # the line that passes the login (Authorization header) on to WordPress.
                r.tip("First try: Settings > Permalinks > choose 'Post name' > Save Changes, then run this again.")
                r.tip("WordPress then writes .htaccess rules that pass the login through, and /wp-json/ starts working.")
                r.tip("Old links like ?p=123 keep working - WordPress redirects them to the new addresses.")
            r.tip("Fix from the WordPress dashboard (no cPanel needed):")
            r.tip("  1. Plugins > Add New > search 'Code Snippets' > Install > Activate.")
            r.tip("  2. Snippets > Add New, title 'Auto-poster login fix'.")
            r.tip("  3. Paste the code from wordpress-login-fix.php (in this project folder),")
            r.tip("     without the first line '<?php' and the comment block.")
            r.tip("  4. Choose 'Run snippet everywhere' > Save Changes and Activate. Then run this check again.")
            r.tip("If you have hosting file access instead, these 2 lines at the top of .htaccess also work:")
            r.tip("    RewriteEngine On")
            r.tip("    RewriteRule .* - [E=HTTP_AUTHORIZATION:%{HTTP:Authorization}]")
        return None
    roles = me.get("roles") or []
    r.ok(f"Logged in as '{me.get('name')}' (user id {me.get('id')}"
         + (f", role: {', '.join(roles)})" if roles else ")"))
    return me


def check_permissions(me, r):
    """Step 4."""
    caps = me.get("capabilities") or {}
    if not caps:
        r.warn("Could not read this user's permissions. Make sure the user is an Administrator or Editor.")
        return
    for cap, needed_for, required in CAPABILITIES:
        if caps.get(cap):
            r.ok(f"Can {needed_for}.")
        elif required:
            r.fail(f"Can NOT {needed_for}. Give this user the Editor or Administrator role.")
        else:
            r.warn(f"Can NOT {needed_for}. The Editor or Administrator role allows it.")


def list_categories(wp, r):
    """Step 5."""
    try:
        cats = wp.categories()
    except WordPressError as exc:
        r.warn(f"Could not list categories: {exc}")
        return
    print("        Use these numbers as category_id in feeds.json:")
    for cat in cats:
        print(f"          id {str(cat.get('id')):<6} {html.unescape(str(cat.get('name', '')))}")


def get_credentials(args):
    """Use --url/--user or environment variables, and ask for anything missing. Returns (url, user, password, asked)."""
    asked = False
    url = args.url or env("WP_URL")
    if not url:
        url, asked = input("Your site address (for example https://example.com): ").strip(), True
    user = args.user or env("WP_USER")
    if not user:
        user, asked = input("Your WordPress username: ").strip(), True
    password = env("WP_APP_PASSWORD")
    if not password:
        print("Your Application Password (paste it and press Enter - nothing shows while you type, that's normal):")
        password, asked = getpass.getpass("> ").strip(), True
    return url, user, password, asked


def run(url, user, password):
    """All checks. Returns 0 if everything works, 1 if not."""
    r = Report()
    base = normalize_wp_url(url)
    print("\nWordPress connection check (read-only - nothing on your site is changed)")
    print("=" * 72)
    print(f"Site address: {base}")
    print(f"Username:     {user}")
    check_password_format(password, r)
    total = 5

    r.step(1, total, "Opening the homepage...")
    announced = check_homepage(base, r)

    r.step(2, total, "Looking for the WordPress REST API (the address the auto-poster uses)...")
    root, data = check_rest_api(base, announced, r)
    if not root:
        return finish(r, base)
    recommended = check_rest_details(base, root, data, r)

    r.step(3, total, "Logging in with the Application Password...")
    wp = autopost.WordPress(base, user, password)
    wp.api_root = root
    me = check_login(wp, r)
    if me is None:
        return finish(r, recommended)

    r.step(4, total, "Checking what this user is allowed to do...")
    check_permissions(me, r)

    r.step(5, total, "Reading your categories...")
    list_categories(wp, r)
    return finish(r, recommended)


def finish(r, recommended):
    print("\n" + "=" * 72)
    if r.failed:
        print("RESULT: something is not right yet - read the FAIL lines (and the -> tips) above.")
        return 1
    print("RESULT: everything works!")
    print(f"Use these GitHub secrets:  WP_URL = {recommended}   WP_USER = the username above")
    print("                           WP_APP_PASSWORD = the Application Password you just used")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check that this project can log in to your WordPress site (read-only).")
    parser.add_argument("--url", help="your site address, e.g. https://example.com (or set WP_URL)")
    parser.add_argument("--user", help="your WordPress username (or set WP_USER)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    try:
        url, user, password, asked = get_credentials(args)
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 1
    if not (url and user and password):
        print("The site address, username and Application Password are all needed.")
        return 1

    code = run(url, user, password)
    if asked:
        try:
            input("\nPress Enter to close...")
        except (EOFError, KeyboardInterrupt):
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
