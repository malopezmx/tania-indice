#!/usr/bin/env python3
"""
000Uploader.py  –  v1
Uploads images from converted post folders to WordPress.com and patches
each post's content, replacing <!-- IMG: filename --> placeholders with
real WordPress media URLs. Also sets the first image as featured image.

Usage:
  python 000Uploader.py <output_dir>
  Default: output_dir = E:\\TaniaOut

Config: fill in the four constants below before running.
"""

import os, sys, json, re, time
import requests

# ── Config – fill these in ──────────────────────────────────────────────────
WP_USERNAME    = "blogtaniaquintero@proton.me"
WP_PASSWORD    = "TaNiA1948!Romay#67"          # your WordPress.com login password
CLIENT_ID      = "137919"
CLIENT_SECRET  = "m4zuAY4MqD7XpBRSdwIFj4mhnQ553Yv8ztV2rGls9JiLPC5W4A0TWaDSPUgtrBBA"          # paste your Client Secret here
SITE           = "elblogdeivanytania.com"
# ────────────────────────────────────────────────────────────────────────────

API            = "https://public-api.wordpress.com/rest/v1.1"
OAUTH_URL      = "https://public-api.wordpress.com/oauth2/token"
DELAY          = 0.5         # seconds between API calls (be nice to the server)


def get_token():
    """Exchange credentials for an OAuth2 access token."""
    print("Authenticating with WordPress.com...")
    r = requests.post(OAUTH_URL, data={
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type":    "password",
        "username":      WP_USERNAME,
        "password":      WP_PASSWORD,
    })
    if r.status_code != 200:
        print(f"  AUTH FAILED: {r.status_code} {r.text[:200]}")
        sys.exit(1)
    token = r.json().get("access_token")
    print(f"  OK – token obtained.")
    return token


def upload_image(token, image_path):
    """Upload one image file to WordPress media library.
    Returns the URL of the uploaded image, or None on failure."""
    fname = os.path.basename(image_path)
    with open(image_path, "rb") as f:
        data = f.read()
    mime = "image/jpeg"
    ext  = fname.rsplit(".", 1)[-1].lower()
    if ext == "png":  mime = "image/png"
    elif ext == "gif": mime = "image/gif"
    elif ext == "webp": mime = "image/webp"

    r = requests.post(
        f"{API}/sites/{SITE}/media/new",
        headers={"Authorization": f"Bearer {token}"},
        files={"media[]": (fname, data, mime)},
    )
    if r.status_code in (200, 201):
        items = r.json().get("media", [])
        if items:
            url = items[0].get("URL") or items[0].get("url")
            media_id = items[0].get("ID") or items[0].get("id")
            return url, media_id
    print(f"    UPLOAD FAILED {fname}: {r.status_code} {r.text[:120]}")
    return None, None


def find_post_by_slug(token, slug):
    """Look up a post by its slug. Returns (post_id, content) or (None,None)."""
    r = requests.get(
        f"{API}/sites/{SITE}/posts/slug:{slug}",
        headers={"Authorization": f"Bearer {token}"},
    )
    if r.status_code == 200:
        data = r.json()
        return data.get("ID"), data.get("content", "")
    return None, None


def update_post(token, post_id, content, featured_image_id=None):
    """Update post content and optionally set featured image."""
    payload = {"content": content}
    if featured_image_id:
        payload["featured_image"] = str(featured_image_id)
    r = requests.post(
        f"{API}/sites/{SITE}/posts/{post_id}",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )
    return r.status_code in (200, 201)


def slugify(text):
    """Match the slugify function in the converter."""
    import unicodedata
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", text)[:80].strip("-")


def process_folder(token, folder_path):
    """Process one post folder: upload images, patch post content.
    The first image is uploaded as featured image only (not in body).
    All other images replace their placeholders in the body.
    """
    import json as _json

    # Read meta.json for title and first_image
    meta_path = os.path.join(folder_path, "meta.json")
    html_path = os.path.join(folder_path, "post.html")

    if os.path.exists(meta_path):
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = _json.load(f)
        title       = meta.get("title", "")
        first_image = meta.get("first_image")  # filename of featured image
    elif os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()
        m = re.search(r"<title>(.+?)</title>", html)
        if not m:
            return False, "no title found"
        title       = m.group(1)
        first_image = None
    else:
        return False, "no meta.json or post.html"

    slug = slugify(title)

    # Find post on WordPress
    post_id, content = find_post_by_slug(token, slug)
    if not post_id:
        return False, f"post not found for slug: {slug}"

    images_dir  = os.path.join(folder_path, "images")
    url_map     = {}   # filename -> wordpress URL
    featured_id = None

    # Upload featured image first (not in body placeholders)
    if first_image and os.path.exists(os.path.join(images_dir, first_image)):
        url, media_id = upload_image(token, os.path.join(images_dir, first_image))
        time.sleep(DELAY)
        if media_id:
            featured_id = media_id
            print(f"    Featured: {first_image} → {(url or '')[:60]}...")

    # Find body image placeholders and upload them
    placeholders = re.findall(r"<!-- IMG: ([^>]+?) -->", content)
    for fname in placeholders:
        if fname in url_map:
            continue
        candidates = [fname, re.sub(r"[^\w\s.\-]", "_", fname).strip("_")]
        img_path = None
        for c in candidates:
            p = os.path.join(images_dir, c)
            if os.path.exists(p):
                img_path = p
                break
        if not img_path:
            print(f"    IMAGE NOT FOUND: {fname}")
            continue
        url, media_id = upload_image(token, img_path)
        time.sleep(DELAY)
        if url:
            url_map[fname] = url
            print(f"    Uploaded: {fname} → {url[:60]}...")

    # Patch body: replace placeholders with <img> tags
    def replace_placeholder(m):
        fn  = m.group(1)
        url = url_map.get(fn)
        if url:
            return (f'<div align="center">'
                    f'<img style="width:400px;max-width:100%;" src="{url}" alt="{fn}"/>'
                    f'</div>')
        return m.group(0)

    new_content = re.sub(r"<!-- IMG: ([^>]+?) -->", replace_placeholder, content)

    # Update post content and featured image
    ok = update_post(token, post_id, new_content, featured_id)
    n_body = len(url_map)
    n_feat = 1 if featured_id else 0
    return ok, f"featured={'yes' if featured_id else 'no'}, body images={n_body}/{len(placeholders)}"


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else r"E:\TaniaOut"

    if not WP_PASSWORD or not CLIENT_SECRET:
        print("ERROR: Fill in WP_PASSWORD and CLIENT_SECRET in the script before running.")
        sys.exit(1)

    # Get OAuth token
    token = get_token()

    # Find all post folders (named DATE_BLOG_NNNN)
    folders = sorted(
        f for f in os.listdir(out_dir)
        if os.path.isdir(os.path.join(out_dir, f))
        and re.match(r"\d{4}-\d{2}-\d{2}_", f)
    )

    if not folders:
        print(f"No post folders found in {out_dir}")
        sys.exit(1)

    print(f"\nFound {len(folders)} post folders in {out_dir}\n{'='*60}")
    ok_count  = 0
    err_count = 0

    for folder in folders:
        path = os.path.join(out_dir, folder)
        print(f"  {folder}")
        ok, msg = process_folder(token, path)
        if ok:
            ok_count += 1
            print(f"    OK – {msg}")
        else:
            err_count += 1
            print(f"    SKIP – {msg}")
        time.sleep(DELAY)

    print(f"\n{'='*60}")
    print(f"  Done: {ok_count} posts updated, {err_count} skipped/errors")
    print(f"  Check your blog to verify images and featured images look correct.")


if __name__ == "__main__":
    main()
