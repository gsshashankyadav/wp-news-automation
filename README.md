# WordPress News Auto-Poster (free, runs on GitHub Actions)

Every hour, GitHub runs a small Python script for you. It:

1. reads the RSS feeds you list in `feeds.json` (each feed goes into one WordPress category),
2. finds news items it has not handled before,
3. asks a **free AI** (Google Gemini, or Groq) to write an **original article in its own words**, using only the facts in the headline and summary,
4. sends the article to your WordPress site (as a **draft** by default), with a "Source:" credit link,
5. writes down what it handled in `seen.json`, so nothing is posted twice.

You don't need a server or a paid plan. GitHub runs it, the AI has a free tier, and WordPress has a built-in API.

---

## What you need

| Thing | Cost | Used for |
|---|---|---|
| A WordPress site with **HTTPS** (`https://...`) | you have it | where posts go |
| A WordPress user with the **Administrator** or **Editor** role | free | logging in through the API |
| A GitHub account | free | runs the script every hour |
| A Google Gemini API key (or a Groq key) | free tier | writes the articles |

---

## Step 1: Create a WordPress Application Password

An Application Password is a special password just for apps. It is **not** your normal login password, and you can cancel it at any time.

1. Log in to your WordPress dashboard (`https://yoursite.com/wp-admin`).
2. Go to **Users → Profile** (or **Users → All Users →** click your user).
3. Scroll down to **Application Passwords**.
4. In "New Application Password Name" type `News Auto-Poster` and click **Add New Application Password**.
5. WordPress shows a password like `abcd EFGH 1234 ijkl MNOP 5678`. **Copy it now.** You can't see it again later. Keep the spaces; they're fine.

> Can't see the "Application Passwords" section? Your site needs HTTPS, or a security plugin has switched the feature off. See *Troubleshooting* below.

## Step 2: Get a free AI key

**Option A: Google Gemini (default)**

1. Go to <https://aistudio.google.com/apikey> and sign in with a Google account.
2. Click **Create API key** and copy it.

That's all. The script uses Gemini automatically.

**Option B: Groq**

1. Go to <https://console.groq.com/keys>, sign up, and click **Create API Key**.
2. When you add secrets (Step 6), also add `AI_BASE_URL` = `https://api.groq.com/openai/v1` and `AI_MODEL` = `llama-3.1-8b-instant` (or another model listed on Groq's site).

> Free tiers have limits on requests per minute and per day, and providers change them from time to time. If you hit a limit, the script stops cleanly and carries on in the next hour. Nothing is lost.

## Step 3: Find your category IDs

Every feed in `feeds.json` needs the **number** (ID) of a WordPress category.

**The easy way:** in your dashboard go to **Posts → Categories**, hover over a category name (or click it), and look at the address bar. The number after `tag_ID=` is the ID. For example `...&tag_ID=5&...` means ID **5**.

**Another way:** open `https://yoursite.com/wp-json/wp/v2/categories?per_page=100` in your browser. Each `"id"` is shown next to its `"name"`.

**The easiest way, after Step 6:** run the workflow in **test-connection** mode. It prints the full list of your categories with their IDs.

## Step 4: Edit `feeds.json`

Open `feeds.json` in any text editor (Notepad works). It has two parts.

**`feeds`** is your list of news sources. Copy the `{ ... }` block once for each feed you want, and put a comma between the blocks:

```json
{
  "url": "https://feeds.bbci.co.uk/news/world/rss.xml",
  "category_id": 5,
  "category_name": "World",
  "source_name": "BBC News"
}
```

- `url` is the RSS feed address. Many sites link it as "RSS". You can also try adding `/feed` to a WordPress site's address.
- `category_id` is the number from Step 3. **Change the example `1`s to your real IDs.**
- `category_name` is only shown in the logs, to help you read them.
- `source_name` is shown in the credit line at the bottom of each post ("Source: BBC News").

**`settings`** controls how the script behaves:

| Setting | Default | Meaning |
|---|---|---|
| `post_status` | `"draft"` | `"draft"` = you review each post and click Publish yourself. `"publish"` = posts go live straight away. `"pending"` = waiting for review. |
| `language` | `"English"` | The language the AI writes in, e.g. `"Hindi"` or `"Spanish"`. |
| `article_words` | `400` | Roughly how long each article should be. When a story has few facts, the AI writes 150–250 words instead. |
| `max_posts_per_run` | `6` | The most posts created in one hourly run, across all feeds. |
| `max_posts_per_feed` | `2` | The most posts from one feed in one run. |
| `items_to_check_per_feed` | `10` | How many of the newest items in each feed are checked. |
| `delay_between_posts_sec` | `15` | Seconds to wait between posts, to stay inside the AI's free limits. |
| `skip_backlog_on_first_run` | `true` | On the very first run (or when you add a new feed), post only the newest few items and mark the older ones as done, so your site isn't flooded with old news. |
| `author_id` | `null` | `null` = posts belong to the user from Step 1. Put a user ID number to post as someone else. |
| `featured_image` | `true` | If the RSS item has a picture, upload it and use it as the featured image. If that fails, the post is still created, just without the image. |

> `feeds.json` must stay valid JSON. Use double quotes `"`, separate items with commas, and put **no** comma after the last item. If you get it wrong, the script tells you the line number. You can also check the file at <https://jsonlint.com>.

## Step 5: Put the project on GitHub

1. Go to <https://github.com/new>.
2. Repository name: for example `wp-news-auto`.
3. Pick **Public** or **Private**:
   - **Public**: GitHub Actions minutes are unlimited and free. Your secrets stay **hidden**. People can only see your feed list and `seen.json`.
   - **Private**: GitHub gives about 2,000 free minutes a month. An hourly run takes 1–3 minutes, so you could get close to that. If you go private, keep an eye on usage (**Settings → Billing**), or change the schedule to every 2 hours (see *Change how often it runs*).
4. **Don't** tick "Add a README" (this project already has one). Click **Create repository**.
5. Open a terminal **in this project folder** and run the commands below. Replace `YOUR-NAME` with your GitHub username:

```bash
git remote add origin https://github.com/YOUR-NAME/wp-news-auto.git
```

```bash
git push -u origin main
```

The first time, a window asks you to log in to GitHub. Do it, and the push finishes. Refresh the GitHub page and your files appear.

## Step 6: Add your secrets on GitHub

Secrets are encrypted. The script can read them, but nobody can see them, not even you after saving.

1. In your repository on GitHub, go to **Settings → Secrets and variables → Actions**.
2. Click **New repository secret** once for each secret below:

| Name | Value |
|---|---|
| `WP_URL` | Your site address, e.g. `https://yoursite.com` (no `/wp-admin` at the end) |
| `WP_USER` | Your WordPress **username** (the one you log in with) |
| `WP_APP_PASSWORD` | The Application Password from Step 1 |
| `AI_API_KEY` | Your Gemini (or Groq) key from Step 2 |
| `AI_BASE_URL` | *Optional.* Only for Groq: `https://api.groq.com/openai/v1` |
| `AI_MODEL` | *Optional.* Only to use a different model, e.g. `llama-3.1-8b-instant` for Groq |

Names must match exactly (capital letters, underscores). If you skip the optional ones, the script uses Gemini with `gemini-2.5-flash`.

## Step 7: Run it for the first time

1. On GitHub, click the **Actions** tab. If it asks, click **I understand my workflows, go ahead and enable them**.
2. Click **Auto-post news** on the left.
3. Click **Run workflow**, choose **test-connection**, and click the green **Run workflow** button.
4. Wait about a minute, click the run, then click **autopost → Run the auto-poster** to read the log. You should see:
   - `SUCCESS - logged in as '...'`
   - a list of your categories with their IDs (check that the IDs in `feeds.json` are right)
   - `SUCCESS - the AI answered: OK`
5. Run again with **dry-run**. It shows which news items *would* be posted, without posting anything.
6. Run again with **normal**. This is a real run. Then open **Posts → Drafts** in WordPress and read the new articles.

After that, it runs by itself every hour at minute 17 (UTC). You'll see a small commit "Update seen.json" after each run that handled something. That's the script saving its memory.

**Your routine:** check your drafts, fix anything you don't like, and click **Publish**. When you trust the quality, you can set `"post_status": "publish"`. Reviewing first is safer, because AI can still get things wrong.

---

## Changing things later

- **Add or remove feeds / change settings:** edit `feeds.json` on GitHub (open the file, click the pencil ✏️, then **Commit changes**). The next run uses it.
- **Change how often it runs:** edit `.github/workflows/autopost.yml`. The line `cron: "17 * * * *"` means "minute 17 of every hour". `"17 */2 * * *"` means every 2 hours, and `"17 6-22 * * *"` means hourly between 06:00 and 22:00 UTC.
- **Pause it:** **Actions → Auto-post news → ⋯ (top right) → Disable workflow**.
- **Start over / forget everything:** set `seen.json` back to `[]`. Careful: the next run then treats everything as new again (with `skip_backlog_on_first_run` it still posts only the newest few per feed).

## Running it on your own computer (optional)

You need Python 3.12 or newer. Open a terminal in this project folder and install the two libraries once:

```bash
pip install -r requirements.txt
```

### Check your WordPress connection

```bash
python check_wp.py
```

It asks for your site address, username and Application Password. Nothing you type is saved, and the password stays hidden while you type or paste it. Then it checks five things and marks each one `OK`, `WARN` or `FAIL`, with a `->` tip for anything that's wrong:

1. **Homepage:** does your site open, and does it look like WordPress?
2. **REST API:** does the address the auto-poster uses work? It tries `/wp-json/` and then `?rest_route=`.
3. **Login:** does the Application Password work? It also tells apart "wrong password" and "your host removes the login header".
4. **Permissions:** can this user create posts, publish, upload images and create tags?
5. **Categories:** your category IDs, ready to copy into `feeds.json`.

At the end it tells you the exact `WP_URL` to put in your GitHub secret. It only *reads* from your site. It never creates, changes or deletes anything.

### Try a dry run

```bash
python autopost.py --dry-run
```

`--dry-run` needs no secrets. For a real run or `--test-connection`, set the environment variables first. In PowerShell:

```powershell
$env:WP_URL="https://yoursite.com"; $env:WP_USER="you"; $env:WP_APP_PASSWORD="xxxx xxxx xxxx xxxx xxxx xxxx"; $env:AI_API_KEY="your-key"
python autopost.py --test-connection
```

To run the automatic tests (no internet needed):

```bash
python -m unittest -v
```

---

## Troubleshooting

**Start here:** run `python check_wp.py` on your computer (see above). It tests each part of the WordPress connection separately and tells you which one is broken.

| Message in the log | What it means / what to do |
|---|---|
| `401 = wrong username or Application Password` | Check `WP_USER` (username) and `WP_APP_PASSWORD` (the **Application** Password, not your normal one). |
| `401 = WordPress did not receive any login at all` (`rest_not_logged_in`) | Your web server (often LiteSpeed) drops the login before it reaches WordPress. See **Fixing "did not receive any login"** below. |
| `403 = logged in but not allowed` | Give the user the **Editor** or **Administrator** role. Or a security plugin (Wordfence, iThemes, etc.), Cloudflare, or your host is blocking the REST API or GitHub's servers. Allow `/wp-json/` in that tool. |
| `404 = not found` | Check `WP_URL`. Make sure no plugin disables the REST API. |
| `answered with a web page instead of JSON` | Your host or a plugin shows a "checking your browser" / captcha page to robots. This often happens on very cheap or free hosting. Allow the REST API in that tool, or use a different host. |
| `400 = WordPress rejected the data` | Usually a `category_id` or `author_id` in `feeds.json` that doesn't exist. Run **test-connection** to see your real IDs. |
| `No "Application Passwords" section` | The site must use HTTPS. Some security plugins have a switch to turn the feature back on. |
| `AI rate limit was reached` | Normal on the free tier. The run stops and continues next hour. If it happens a lot, lower `max_posts_per_run`, raise `delay_between_posts_sec`, try a lighter model such as `gemini-2.5-flash-lite` (secret `AI_MODEL`), or switch to Groq. |
| `the AI API key is wrong` | Copy the key again into the `AI_API_KEY` secret. If you use Groq, make sure `AI_BASE_URL` is also set. |
| `Could not read this feed` | The feed address is wrong or the site is down. Open the URL in your browser; it should show XML text. The other feeds still run. |
| The "Save seen.json" step fails with `403` / `permission denied` | **Settings → Actions → General → Workflow permissions** → choose **Read and write permissions** → Save. |
| The workflow stopped running by itself | GitHub pauses scheduled workflows in repositories with no activity for 60 days. Open **Actions** and enable it again. Note that scheduled runs can also start a few minutes late, or be skipped when GitHub is very busy. |

### Fixing "did not receive any login"

Some web servers, often LiteSpeed, remove the normal login header before WordPress can read it. The auto-poster also sends the login under a second name (`X-WP-Authorization`), and a small snippet passes it on to WordPress. WordPress still checks the Application Password exactly as usual. Your normal password doesn't work through it.

1. **If your permalinks are "Plain"**, first go to **Settings → Permalinks**, choose **Post name**, click **Save Changes**, and run `python check_wp.py` again. Sometimes that's enough. Old `?p=123` links still redirect to the new addresses.
2. **Still failing? Add the snippet.** This is done from the WordPress dashboard, so no cPanel is needed:
   1. **Plugins → Add New**, search for **Code Snippets**, then click **Install** and **Activate**.
   2. **Snippets → Add New**, title `Auto-poster login fix`.
   3. Open `wordpress-login-fix.php` from this project and paste the code **below** the comment block. Leave out the first line `<?php` and the comment.
   4. Choose **Run snippet everywhere**, then click **Save Changes and Activate**.
   5. Run `python check_wp.py` again.
3. **If you do have hosting file access**, you can instead add these two lines at the top of `.htaccess` in your site's main folder: `RewriteEngine On` and `RewriteRule .* - [E=HTTP_AUTHORIZATION:%{HTTP:Authorization}]`.

## Good to know

- **Copyright and rules.** The AI is told to use only the facts and write in its own words, and every post links to the source. Still, the source sites own their text and **photos**. Some sites don't allow their RSS content to be reused, and copying someone's photos onto your site can break copyright law. Check each source's terms. If you're unsure, set `"featured_image": false`. Search engines and ad networks (for example Google AdSense) also have rules about automatically generated content. Reviewing and improving the drafts yourself is the safest approach.
- **AI can make mistakes.** It's told never to invent facts, but read each draft before publishing, especially numbers, names and quotes.
- **What if an item fails?** It is *not* marked as done, so it's tried again on a later run while it's still among the newest items in its feed.
- **Feed pictures** are often small (for example, BBC thumbnails are 240 pixels wide). You can always replace the featured image in WordPress.

## Files in this project

| File | What it is |
|---|---|
| `autopost.py` | The main script |
| `feeds.json` | Your feeds and settings: **edit this** |
| `seen.json` | The script's memory of handled items (updated automatically, starts as `[]`) |
| `requirements.txt` | The two small Python libraries it needs (`feedparser`, `requests`) |
| `.github/workflows/autopost.yml` | Tells GitHub to run the script every hour |
| `check_wp.py` | Checks the WordPress connection step by step (run it on your computer) |
| `wordpress-login-fix.php` | Snippet for WordPress (via the Code Snippets plugin), only needed if your server drops the login |
| `test_autopost.py` | Offline tests (fake RSS, AI and WordPress) |
