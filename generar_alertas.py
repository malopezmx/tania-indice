#!/usr/bin/env python3
"""
generar_alertas.py
Reads all revision*.html files in a folder and generates a matching
alertas*.html for each one, containing only the flagged posts.

Usage:
    python generar_alertas.py <folder>

Example:
    python generar_alertas.py E:\\Tania\\TestOutput

The script generates:
    alertas001.html          (from revision001.html)
    alertas_001_2012-...html (from revision_001_2012-...html)
    etc.
"""

import sys
import re
import urllib.parse
from pathlib import Path
from html.parser import HTMLParser
from datetime import datetime

BLOG_URL = "https://elblogdeivanytania.com"

ALERTAS_CSS = """
body{font-family:Arial,sans-serif;max-width:800px;margin:40px auto;padding:0 20px;color:#333;background:#f9f9f9}
h1{color:#8b0000;border-bottom:2px solid #8b0000;padding-bottom:8px}
.summary{background:#fff;border:1px solid #ddd;padding:12px 16px;border-radius:4px;margin-bottom:24px;font-size:.95em}
.post{border-left:4px solid #ffa000;padding:10px 15px;margin:12px 0;background:#fff8e1;border-radius:0 4px 4px 0}
.post h3{margin:0 0 4px;font-size:1em;font-weight:bold}
.post h3 a{color:#8b0000;text-decoration:none}
.post h3 a:hover{text-decoration:underline}
.post .fecha{font-size:.82em;color:#777;margin-bottom:6px}
.post ul{margin:6px 0 0;padding-left:18px;font-size:.88em;color:#444}
.post li{margin:3px 0}
.ninguna{color:green;font-style:italic;margin-top:20px}
"""


class RevisionParser(HTMLParser):
    """Parse revision HTML files to extract flagged posts."""

    def __init__(self):
        super().__init__()
        self.posts = []           # list of {title, date, wp_id, flags}
        self._current = None      # post being built
        self._in_h2 = False
        self._in_flag_li = False
        self._in_fecha_span = False
        self._in_flags_div = False  # True only when inside <div class="flags">
        self._depth_post = 0
        self._in_post = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        # Detect start of a .post div
        if tag == 'div' and 'post' in attrs.get('class', '').split():
            self._in_post = True
            self._depth_post = 1
            wp_id = attrs.get('data-wp-id', '')
            self._current = {'title': '', 'date': '', 'wp_id': wp_id, 'flags': []}
            return

        if not self._in_post:
            return

        if tag == 'div':
            self._depth_post += 1
            # Detect <div class="flags">
            if 'flags' in attrs.get('class', '').split():
                self._in_flags_div = True

        # Post title is in h2 inside post-header
        if tag == 'h2' and self._current is not None:
            classes = attrs.get('class', '').split()
            if 'warn' in classes or 'ok' in classes:
                self._in_h2 = True

        # Date is in first <span> inside .meta
        if tag == 'span' and self._current is not None:
            if not self._current['date']:
                self._in_fecha_span = True

        # Flag items are <li> ONLY inside .flags div
        if tag == 'li' and self._current is not None and self._in_flags_div:
            self._in_flag_li = True
            self._current['flags'].append('')

    def handle_endtag(self, tag):
        if not self._in_post:
            return

        if tag == 'div':
            if self._depth_post > 0:
                self._depth_post -= 1
            if self._depth_post == 0:
                # End of .post div
                if self._current and self._current.get('flags'):
                    self.posts.append(self._current)
                self._current = None
                self._in_post = False
                self._in_flags_div = False

        if tag == 'h2':
            self._in_h2 = False

        if tag == 'span':
            self._in_fecha_span = False

        if tag == 'li':
            self._in_flag_li = False

    def handle_data(self, data):
        if not self._in_post or self._current is None:
            return

        if self._in_h2 and not self._current['title']:
            self._current['title'] = data.strip()

        if self._in_fecha_span and not self._current['date']:
            # Span text looks like "📅 2023-12-04"
            cleaned = data.strip().replace('📅', '').strip()
            if re.match(r'\d{4}-\d{2}-\d{2}', cleaned):
                self._current['date'] = cleaned

        if self._in_flag_li and self._current['flags']:
            self._current['flags'][-1] += data


def parse_revision(html_path):
    """Parse a revision HTML file and return list of flagged posts."""
    content = html_path.read_text(encoding='utf-8', errors='replace')

    # Extract total post count from summary div
    total_match = re.search(r'<strong>(\d+)\s+posts?</strong>', content)
    total = int(total_match.group(1)) if total_match else 0

    parser = RevisionParser()
    parser.feed(content)
    return parser.posts, total


def escape_html(s):
    return (s.replace('&', '&amp;')
             .replace('<', '&lt;')
             .replace('>', '&gt;')
             .replace('"', '&quot;'))


def build_alertas(flagged_posts, total, source_name):
    """Build alertas HTML from a list of flagged posts."""
    parts = [f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="UTF-8">
<title>Alertas — {escape_html(source_name)}</title>
<style>{ALERTAS_CSS}</style>
</head>
<body>
<h1>⚠ Alertas para revisión</h1>
<div class="summary">
  <strong>{len(flagged_posts)} posts con alertas</strong>
  {f'de {total} procesados' if total else ''}
  &nbsp;|&nbsp; <strong>{escape_html(source_name)}</strong>
  &nbsp;|&nbsp; <small style="color:#888">Generado: {datetime.now().strftime('%Y-%m-%d %H:%M')}</small>
</div>
"""]

    if not flagged_posts:
        parts.append('<p class="ninguna">✓ No hay alertas en este archivo. ¡Todo correcto!</p>')
    else:
        for post in flagged_posts:
            title  = post.get('title', '(sin título)')
            date   = post.get('date', '')
            wp_id  = post.get('wp_id', '').strip()
            flags  = [f.strip() for f in post.get('flags', []) if f.strip()]

            if wp_id and wp_id != '?':
                link = f'{BLOG_URL}/?p={wp_id}'
            else:
                search = urllib.parse.quote(title[:60])
                link   = f'{BLOG_URL}/?s={search}'

            flag_items = '\n'.join(
                f'        <li>{escape_html(f)}</li>' for f in flags
            )
            parts.append(f"""<div class="post">
  <h3><a href="{link}" target="_blank">{escape_html(title)}</a></h3>
  <div class="fecha">{escape_html(date)}</div>
  <ul>
{flag_items}
  </ul>
</div>
""")

    parts.append('</body></html>')
    return ''.join(parts)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    folder = Path(sys.argv[1])
    if not folder.is_dir():
        print(f"Error: '{folder}' is not a valid folder.")
        sys.exit(1)

    revision_files = sorted(folder.glob('revision*.html'))
    if not revision_files:
        print(f"No revision*.html files found in {folder}")
        sys.exit(1)

    print(f"Found {len(revision_files)} revision file(s) in {folder}\n")

    for rev_path in revision_files:
        # Derive alertas filename: revision → alertas
        alertas_name = rev_path.name.replace('revision', 'alertas', 1)
        alertas_path = folder / alertas_name

        # Source label for the report header
        source_name = rev_path.stem.replace('revision', '').strip('_- ')
        source_name = source_name if source_name else rev_path.name

        print(f"Processing: {rev_path.name}")
        flagged_posts, total = parse_revision(rev_path)
        print(f"  → {len(flagged_posts)} flagged posts out of {total} total")

        html = build_alertas(flagged_posts, total, source_name)
        alertas_path.write_text(html, encoding='utf-8')
        print(f"  → Written: {alertas_path.name}\n")

    print("Done!")


if __name__ == '__main__':
    main()
