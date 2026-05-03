#!/usr/bin/env python3
"""
fix_post_dates.py
Reads the revision.html to find post titles and dates,
then updates the WordPress posts via API to set the correct dates.

Usage:
    python fix_post_dates.py

Configure TOKEN, SITE below.
"""

import json, re, time, urllib.request, urllib.parse, os

TOKEN = open("wp_token.txt").read().strip() if os.path.exists("wp_token.txt") else input("Token: ").strip()
SITE  = "blogtaniaquintero-lvghq.wordpress.com"
API   = f"https://public-api.wordpress.com/rest/v1.1/sites/{SITE}"

def api_get(endpoint, params=None):
    url = API + endpoint
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

def api_post(endpoint, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        API + endpoint, data=body,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

# Read dates from revision.html
revision = "E:\\Tania\\TestOutput\\revision.html"
if not os.path.exists(revision):
    revision = input("Path to revision.html: ").strip()

with open(revision, encoding="utf-8") as f:
    html = f.read()

# Extract title + date pairs from review report
pairs = re.findall(
    r'<h2[^>]*>([^<]+)</h2>.*?<span>📅 (\d{4}-\d{2}-\d{2})</span>',
    html, re.DOTALL
)
print(f"Found {len(pairs)} posts in revision.html")

# Fetch all posts from WordPress
print("Fetching posts from WordPress...")
posts = []
for status in ["publish", "draft"]:
    offset = 0
    while True:
        result = api_get("/posts", {"status": status, "number": 100, "offset": offset,
                                    "fields": "ID,title,date"})
        batch = result.get("posts", [])
        if not batch: break
        posts.extend(batch)
        if len(batch) < 100: break
        offset += 100

print(f"Found {len(posts)} posts in WordPress")

# Match by title and update date
updated = 0
for title, date in pairs:
    title = title.strip()
    # Find matching WP post
    match = next((p for p in posts if p["title"].strip() == title), None)
    if not match:
        from html import unescape
        import re as _re
        def norm(s):
            s = unescape(s)  # decode &#8216; etc.
            s = s.lower()
            s = _re.sub(r"[''‘’´`]", "'", s)
            return s.strip()
        match = next((p for p in posts if norm(title[:30]) in norm(p["title"]) or norm(p["title"][:30]) in norm(title)), None)
    if not match:
        print(f"  ⚠ Not found: {title[:50]}")
        continue

    post_id   = match["ID"]
    post_date = f"{date}T12:00:00"
    try:
        api_post(f"/posts/{post_id}", {"date": post_date})
        print(f"  ✓ [{post_id}] {title[:50]} → {date}")
        updated += 1
        time.sleep(0.5)
    except Exception as e:
        print(f"  ✗ [{post_id}] {title[:50]}: {e}")

print(f"\nDone: {updated} posts updated")
