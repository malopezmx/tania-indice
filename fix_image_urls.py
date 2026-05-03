#!/usr/bin/env python3
"""
fix_image_urls.py
Queries the WordPress.com media library, matches uploaded images to the
references in imported posts, and updates the post content with real URLs.

Usage:
    python fix_image_urls.py

Configuration: edit the CONFIG section below before running.
"""

import json
import re
import sys
import time
import unicodedata
import urllib.request
import urllib.parse
import urllib.error


def wp_sanitize_filename(filename):
    """
    Mimic WordPress filename sanitization:
    - Lowercase
    - Accents stripped
    - Spaces and special chars → hyphens
    - Multiple hyphens collapsed
    - Extension preserved
    """
    if "." in filename:
        name, ext = filename.rsplit(".", 1)
        ext = "." + ext.lower()
    else:
        name, ext = filename, ""

    # Strip accents
    name = unicodedata.normalize("NFD", name.lower())
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    # Replace spaces and special chars with hyphens
    name = re.sub(r"[^a-z0-9_]+", "-", name)
    name = name.strip("-")
    return name + ext

# ── CONFIG — edit these before running ────────────────────────────────────────

CLIENT_ID      = "137919"
CLIENT_SECRET  = "m4zuAY4MqD7XpBRSdwIFj4mhnQ553Yv8ztV2rGls9JiLPC5W4A0TWaDSPUgtrBBA"          # paste your Client Secret here
REDIRECT_URI  = "https://elblogdeivanytania.com"   # must match your app settings

# Your WordPress.com site — use the .wordpress.com subdomain
SITE          = "blogtaniaquintero-lvghq.wordpress.com"

# ─────────────────────────────────────────────────────────────────────────────

AUTH_URL    = "https://public-api.wordpress.com/oauth2"
API_URL     = "https://public-api.wordpress.com/rest/v1.1"
TOKEN       = "adPVF&4nzbVk7Pcv56sUKW)ZXEz$44U7$Nxjr^eUKQlSpbRV#OZyGeD3UfYF!e6Y"


def api_get(endpoint, token, params=None):
    url = f"{API_URL}/sites/{SITE}{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def api_post(endpoint, token, data):
    url = f"{API_URL}/sites/{SITE}{endpoint}"
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST"
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def get_access_token():
    """
    WordPress.com OAuth2 — opens the authorization URL in the browser,
    then asks you to paste the code back here.
    """
    params = urllib.parse.urlencode({
        "client_id":     CLIENT_ID,
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
        "scope":         "global",
    })
    auth_url = f"{AUTH_URL}/authorize?{params}"

    print("\n══════════════════════════════════════════════════════")
    print("Step 1: Open this URL in your browser and authorize:")
    print(f"\n  {auth_url}\n")
    print("Step 2: After authorizing, WordPress.com will redirect")
    print(f"  to {REDIRECT_URI}?code=XXXXXXXX")
    print("  Copy the 'code' value from the URL.")
    print("══════════════════════════════════════════════════════\n")

    code = input("Paste the authorization code here: ").strip()

    # Exchange code for token
    data = urllib.parse.urlencode({
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri":  REDIRECT_URI,
        "code":          code,
        "grant_type":    "authorization_code",
    }).encode()

    req = urllib.request.Request(
        f"{AUTH_URL}/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST"
    )
    with urllib.request.urlopen(req) as r:
        result = json.loads(r.read())

    token = result.get("access_token")
    if not token:
        print(f"Error getting token: {result}")
        sys.exit(1)

    print(f"\n✓ Access token obtained.")
    print(f"  (Save this for future use: {token})\n")
    return token


def get_all_media(token):
    """Fetch all media items from the WordPress.com media library."""
    print("Fetching media library...")
    media_map    = {}   # filename → full URL
    media_id_map = {}   # filename → WordPress media ID
    offset = 0
    page_size = 100

    while True:
        result = api_get("/media", token, {
            "number": page_size,
            "offset": offset,
            "fields": "ID,URL,title,file",
        })
        items = result.get("media", [])
        if not items:
            break

        for item in items:
            url  = item.get("URL", "")
            # Extract just the filename from the URL
            fname = url.split("/")[-1]
            media_id   = item.get("ID", 0)
            media_map[fname]    = url
            media_id_map[fname] = media_id
            title = item.get("title", "")
            if title and title not in media_map:
                media_map[title]    = url
                media_id_map[title] = media_id

        print(f"  Fetched {offset + len(items)} media items so far...")
        if len(items) < page_size:
            break
        offset += page_size
        time.sleep(0.3)

    print(f"✓ Found {len(media_map)} media items.\n")
    return media_map, media_id_map


def get_all_posts(token):
    """Fetch all draft and published posts."""
    print("Fetching posts...")
    posts = []
    offset = 0
    page_size = 100

    for status in ["draft", "publish"]:
        offset = 0
        while True:
            result = api_get("/posts", token, {
                "status":  status,
                "number":  page_size,
                "offset":  offset,
                "fields":  "ID,title,content,featured_image",
            })
            batch = result.get("posts", [])
            if not batch:
                break
            posts.extend(batch)
            print(f"  {status}: {offset + len(batch)} posts fetched...")
            if len(batch) < page_size:
                break
            offset += page_size
            time.sleep(0.3)

    print(f"✓ Found {len(posts)} posts total.\n")
    return posts


def fix_post_content(content, media_map):
    """
    Replace local image references (images/filename or src="images/filename")
    with real WordPress media URLs.
    Returns (new_content, count_of_replacements).
    """
    replacements = 0
    # Build a sanitized lookup: wp_sanitize(original_name) → wp_url
    sanitized_map = {wp_sanitize_filename(k): v for k, v in media_map.items()}

    def replace_src(m):
        nonlocal replacements
        prefix = m.group(1)   # src=" or src='
        path   = m.group(2)   # images/filename or just filename
        suffix = m.group(3)   # closing quote

        # Skip YouTube and other external URLs
        if "youtube" in path or "youtu.be" in path or path.startswith("http"):
            return m.group(0)
        # Skip YouTube iframe parameters
        if "version=3" in path or "wmode=" in path:
            return m.group(0)

        # Extract just the filename
        fname = path.split("/")[-1]

        # Try exact match first
        wp_url = media_map.get(fname)
        if wp_url:
            replacements += 1
            return f'{prefix}{wp_url}{suffix}'

        # Try sanitized match (WordPress lowercases and hyphenates filenames)
        sanitized = wp_sanitize_filename(fname)
        wp_url = sanitized_map.get(sanitized)
        if wp_url:
            replacements += 1
            return f'{prefix}{wp_url}{suffix}'

        return m.group(0)   # no match, leave as-is

    new_content = re.sub(
        r'(src=["\'])(?:images/)?([^"\']+)(["\'])',
        replace_src,
        content
    )
    return new_content, replacements


def main():
    print("═" * 60)
    print("WordPress.com Image URL Fixer")
    print("═" * 60)

    if CLIENT_ID == "YOUR_CLIENT_ID":
        print("\nERROR: Please edit the CONFIG section at the top of this")
        print("script and set your CLIENT_ID and CLIENT_SECRET before running.")
        sys.exit(1)

    # Get access token
    token = TOKEN if TOKEN else get_access_token()

    # Fetch media library
    media_map, media_id_map = get_all_media(token)
    if not media_map:
        print("No media found in library. Have you uploaded the images?")
        sys.exit(1)

    # Fetch posts
    posts = get_all_posts(token)
    if not posts:
        print("No posts found.")
        sys.exit(1)

    # Debug: show what filenames posts reference vs what's in media library
    print("Checking image references in posts...")
    all_refs = set()
    for post in posts:
        refs = re.findall(r'src=["\'](?:images/)?([^"\']+)["\']', post.get("content", ""))
        for r in refs:
            fname = r.split("/")[-1]
            all_refs.add(fname)

    sanitized_map_debug = {wp_sanitize_filename(k): v for k, v in media_map.items()}
    print(f"\nImage filenames referenced in posts ({len(all_refs)}):")
    for ref in sorted(all_refs):
        # Skip YouTube refs in debug
        if "youtube" in ref or "version=3" in ref or "wmode=" in ref:
            continue
        wp_url = media_map.get(ref) or sanitized_map_debug.get(wp_sanitize_filename(ref))
        if wp_url:
            print(f"  ✓ {ref}")
        else:
            print(f"  ✗ {ref}")
            san = wp_sanitize_filename(ref)
            print(f"    → Sanitized: {san}")
            close = [k for k in media_map if san[:15] in k]
            if close:
                print(f"    → Found in library: {close[0]}")
    print()

    # Fix each post
    print("Updating posts...")
    updated = 0
    skipped = 0

    for post in posts:
        post_id = post["ID"]
        title   = post.get("title", "?")
        content = post.get("content", "")

        new_content, count = fix_post_content(content, media_map)

        if count == 0:
            skipped += 1
            continue

        # Update the post
        # Set featured image if post has one referenced
        update_data = {"content": new_content}

        # Look up the featured image in media library by ID
        # WordPress.com requires the media ID not filename
        # We find it by matching the filename in the media map
        fi = post.get("featured_image", {})
        if fi and isinstance(fi, dict):
            fi_url = fi.get("source_url", "")
            fi_id  = fi.get("ID", 0)
            if fi_id:
                update_data["featured_image"] = fi_id

        try:
            api_post(f"/posts/{post_id}", token, update_data)
            api_post(f"/posts/{post_id}", token, {"content": new_content})
            print(f"  ✓ [{post_id}] {title[:50]} — {count} image(s) updated")
            updated += 1
            time.sleep(0.5)
        except urllib.error.HTTPError as e:
            print(f"  ✗ [{post_id}] {title[:50]} — HTTP error {e.code}: {e.read()}")

    print(f"\n═══════════════════════════════")
    print(f"Done: {updated} posts updated, {skipped} posts had no local image refs.")


if __name__ == "__main__":
    main()
