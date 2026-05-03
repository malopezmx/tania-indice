#!/usr/bin/env python3
"""
000Converter.py  –  v3
Converts Tania/Ivan blog emails (.eml) to:
  • Individual HTML files    output/DATE_BLOG_NNNN/post.html  (backup/preview)
  • Chunked WXR XML files    output/wxr/wxr_NNN_of_MMM.xml    (WordPress import)

Usage:
  python 000Converter.py  [input_dir]  [output_dir]
  Defaults: input  = E:\\Tania
            output = E:\\TaniaOut

After conversion:
  WordPress Dashboard  Tools  Import  WordPress
  Upload each wxr_NNN_of_MMM.xml in order.
  When prompted, map authors  ivan / tania  to their WordPress accounts.

Config:
  SITE_URL        – update to your actual WordPress.com URL before running
  POSTS_PER_CHUNK – posts per WXR file (keep output files under ~40 MB)
"""

import base64, email, json, mimetypes, os, re, sys, unicodedata
import xml.sax.saxutils as saxutils
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from datetime import datetime

#  Blog config 
SITE_URL        = "https://elblogdeivanytania.com"
BLOG_TITLE      = "El blog de Iván García y Tania Quintero"
POSTS_PER_CHUNK = 50   # WXR chunk size; reduce if files exceed 40 MB

#  Author mapping: email blog tag  WordPress login 
AUTHOR_WP = {
    "IVAN":    ("ivan",  "Iván García"),
    "TANIA":   ("tania", "Tania Quintero"),
    "UNKNOWN": ("admin", "Admin"),
}

#  Classification lists (unchanged from v2) 
AUTHOR_NAMES = [
    "Iván García", "Tania Quintero", "Charlie Bravo", "Yaremis Flores",
    "Zoé Valdés", "Raúl Rivero", "Juan Juan", "Luis Cino", "Yuri Valle",
    "Manuel Suárez", "Laritza Diversent", "Martí Noticias", "Texto y foto:",
]
SOURCE_BOLD = [
    "Cubafreepress","Cubanet","ABC","BBC","Cubaencuentro","Diario de Cuba",
    "El Mundo","El País","Infobae","El Nuevo Herald","Por ",
]
CAPTION_BOLD_ITALIC = ["Foto:","Fotos:","Cuadro:","Video","Nota","Tomado de"]
PUBLISHED_ITALIC    = ["(Publicado","Publicado"]

MONTHS_ES   = {"ENERO":1,"FEBRERO":2,"MARZO":3,"ABRIL":4,"MAYO":5,"JUNIO":6,
               "JULIO":7,"AGOSTO":8,"SEPTIEMBRE":9,"OCTUBRE":10,"NOVIEMBRE":11,"DICIEMBRE":12}
MONTHS_ES_N = {1:"enero",2:"febrero",3:"marzo",4:"abril",5:"mayo",6:"junio",
               7:"julio",8:"agosto",9:"septiembre",10:"octubre",11:"noviembre",12:"diciembre"}
DAYS_ES_N   = {0:"lunes",1:"martes",2:"miércoles",3:"jueves",4:"viernes",5:"sábado",6:"domingo"}

#  Date helpers 
def fmt_date_es(dt):
    if not dt: return "–"
    return f"{DAYS_ES_N[dt.weekday()]}, {dt.day} de {MONTHS_ES_N[dt.month]} de {dt.year}"

#  Subject parser (unchanged from v2) 
def parse_subject(subject, email_date):
    r = {"blog":"UNKNOWN","title":subject,"post_date":None,"seq_num":0}
    up = subject.upper()
    if "BLOG IVAN" in up:    r["blog"] = "IVAN"
    elif "BLOG TANIA" in up: r["blog"] = "TANIA"
    m = re.search(r'\s*BLOG\s+(IVAN|TANIA).*$', subject, re.IGNORECASE)
    if m: subject = subject[:m.start()]
    subject = re.sub(r'\s*\([^)]*\)?\s*$', '', subject).strip()
    seq = re.match(r'^(\d+)[)\.\s]+', subject)
    if seq:
        r["seq_num"] = int(seq.group(1))
        subject = subject[seq.end():].strip()
    dm = re.match(r'^([A-ZÁÉÍÓÚÜÑ]+)\s+(\d{1,2})\s+([A-ZÁÉÍÓÚÜÑ]+)[\s:.]+',
                  subject, re.IGNORECASE)
    if dm:
        _, day_num, month_str = dm.groups()
        mon = MONTHS_ES.get(month_str.upper())
        if mon and email_date:
            try:    r["post_date"] = datetime(email_date.year, mon, int(day_num))
            except: r["post_date"] = email_date
        subject = subject[dm.end():].strip()
    else:
        # Fallback: DD MONTH without weekday (e.g. "1 SEPTIEMBRE  Title")
        dm2 = re.match(r'^(\d{1,2})\s+([A-ZÁÉÍÓÚÜÑ]+)[\s:.]+',
                       subject, re.IGNORECASE)
        if dm2:
            day_num, month_str = dm2.groups()
            mon = MONTHS_ES.get(month_str.upper())
            if mon and email_date:
                try:    r["post_date"] = datetime(email_date.year, mon, int(day_num))
                except: r["post_date"] = email_date
            subject = subject[dm2.end():].strip()
    r["title"] = subject.strip()
    return r

#  Text preprocessing (unchanged from v2) 
def preprocess(text):
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    lines = text.split('\n')
    merged = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if (i+1 < len(lines) and
                re.match(r'^\s*<https?://\S+>\s*$', lines[i+1]) and line.strip()):
            merged.append(line.rstrip() + ' ' + lines[i+1].strip())
            i += 2; continue
        merged.append(line); i += 1
    return '\n'.join(merged)

def linkify(text):
    def repl(m):
        anc = m.group(1).strip().rstrip('(').strip()
        return f'<a href="{m.group(2)}">{anc}</a>'
    text = re.sub(r'([^<\n]{3,}?)\s*<(https?://[^\s>]+)>', repl, text)
    text = re.sub(r'<(https?://[^\s>]+)>', r'<a href="\1">\1</a>', text)
    return text

def clean_stars(text):
    text = re.sub(r'\*\s+\*', ' ', text)
    text = re.sub(r'\*\*', '', text)
    text = re.sub(r'\*',   '', text)
    return text.strip()

def strip_bold(text):
    s = text.strip()
    if s.startswith('*') and s.endswith('*') and len(s) > 2:
        return True, clean_stars(s[1:-1])
    return False, clean_stars(s)

def classify(text):
    for n in AUTHOR_NAMES:
        if text.startswith(n): return "author"
    for s in SOURCE_BOLD:
        if text.startswith(s): return "source"
    for c in CAPTION_BOLD_ITALIC:
        if text.startswith(c): return "caption"
    for p in PUBLISHED_ITALIC:
        if text.startswith(p): return "published"
    if text.startswith("P:"):         return "question"
    if text.startswith("Mañana:"):      return "manana"
    if text.startswith("Leer también:"): return "leer"
    if text.startswith("Insertar video:"): return "youtube"
    if re.match(r'https?://(www\.)?(youtube\.com|youtu\.be)/', text): return "youtube"
    return "normal"

STYLES = {
    "author":    'style="font-weight:bold;"',
    "source":    'style="font-weight:bold;font-size:85%;"',
    "caption":   'style="font-weight:bold;font-style:italic;font-size:85%;"',
    "published": 'style="font-style:italic;font-size:85%;"',
}

def youtube_id(url):
    """Extract YouTube video ID from a URL."""
    m = re.search(r'(?:v=|youtu\.be/|embed/)([A-Za-z0-9_-]{11})', url)
    return m.group(1) if m else None

def make_youtube_block(url):
    """Return a WordPress embed block for a YouTube URL."""
    vid   = youtube_id(url)
    clean = f"https://www.youtube.com/watch?v={vid}" if vid else url
    return (f'<!-- wp:embed {{"url":"{clean}","type":"video",'
            f'"providerNameSlug":"youtube","responsive":true}} -->\n'
            f'<figure class="wp-block-embed is-type-video is-provider-youtube">'
            f'<div class="wp-block-embed__wrapper">\n{clean}\n</div></figure>\n'
            f'<!-- /wp:embed -->')

def build_placeholder_imap(imap_rel):
    """Return imap with placeholder comments – keeps WXR XML small."""
    return {k: f"<!-- IMG: {k} -->" for k in imap_rel}

def make_div(text, kind, bold=False):
    text = linkify(text)
    if kind == "question": return f'<p><b>{text}</b></p>'
    if kind == "youtube":
        m = re.search(r'https?://[^\s<>"]+', text)
        if m: return make_youtube_block(m.group(0))
        return f'<p>{linkify(text)}</p>'
    if kind == "manana":
        return f'<p><b>Mañana:</b> {text[len("Mañana:"):].strip()}</p>'
    if kind == "leer":
        return f'<p><b>Leer también:</b> {text[len("Leer también:"):].strip()}</p>'
    if kind == "author":
        return f'\n<p></p>\n<p {STYLES["author"]}>{text}</p>'
    st = STYLES.get(kind, '')
    if st:   return f'<p {st}>{text}</p>'
    return f'<p>{text}</p>'  # never bold for normal paragraphs (Gmail asterisk artifact)

def img_div(fname, imap):
    """Render an image tag or placeholder comment."""
    src = imap.get(fname.lower(), fname)
    if src.startswith('<!--'):
        return src
    return (f'<div align="center">'
            f'<img style="width:400px;max-width:100%;" src="{src}" alt="{fname}"/>'
            f'</div>')

def title_matches(text, title):
    """Check if a text block is essentially the same as the post title."""
    def norm(s):
        return re.sub(r'[^\w\s]', '', s).strip().lower()
    return norm(text) == norm(title)

def convert(plain, imap, skip_title=None, skip_first_image=False):
    """Convert plain-text email body to HTML blocks.
    skip_title: if set, suppress the first paragraph matching this title.
    skip_first_image: if True, skip the first image (it becomes featured image).
    """
    text   = preprocess(plain)
    blocks = re.split(r'\n{2,}', text)
    parts  = []
    title_skipped = skip_title is None   # if no title to skip, mark as already done
    images_seen   = 0

    for block in blocks:
        block = block.strip()
        if not block: continue
        # Pure image block
        m = re.match(r'^\*?\[image:\s*(.+?)\]\*?$', block, re.IGNORECASE)
        if m:
            if skip_first_image and images_seen == 0:
                images_seen += 1
                continue   # skip first image – it is the featured image
            images_seen += 1
            parts.append(img_div(m.group(1).strip(), imap))
            continue
        # Block may contain inline images
        segs = re.split(r'(\[image:[^\]]+\])', block)
        if len(segs) == 1:
            lines  = [l.strip() for l in block.split('\n') if l.strip()]
            joined = ' '.join(lines)
            bold, clean = strip_bold(joined)
            # Skip title if it matches
            if not title_skipped and title_matches(clean, skip_title):
                title_skipped = True
                continue
            parts.append(make_div(clean, classify(clean), bold))
        else:
            for seg in segs:
                seg = seg.strip()
                if not seg: continue
                m2 = re.match(r'\[image:\s*(.+?)\]', seg, re.IGNORECASE)
                if m2:
                    if skip_first_image and images_seen == 0:
                        images_seen += 1
                        continue
                    images_seen += 1
                    parts.append(img_div(m2.group(1).strip(), imap))
                else:
                    lines  = [l.strip() for l in seg.split('\n') if l.strip()]
                    joined = ' '.join(lines)
                    if not joined: continue
                    bold, clean = strip_bold(joined)
                    if not title_skipped and title_matches(clean, skip_title):
                        title_skipped = True
                        continue
                    parts.append(make_div(clean, classify(clean), bold))
    return '\n'.join(parts)

#  Image extraction & base64 helpers 
def extract_images(msg, image_dir):
    """Extract embedded images to disk; return filenamerelative-path map."""
    os.makedirs(image_dir, exist_ok=True)
    mapping = {}
    for part in msg.walk():
        if part.get_content_maintype() == 'image':
            fname = part.get_filename()
            if fname: fname = str(make_header(decode_header(fname)))
            if not fname:
                fname = part.get('Content-ID', '').strip('<>') + '.jpg'
            safe    = re.sub(r'[^\w\s.\-]', '_', fname).strip('_')
            payload = part.get_payload(decode=True)
            if payload:
                with open(os.path.join(image_dir, safe), 'wb') as f:
                    f.write(payload)
                rel = f"images/{safe}"
                mapping[fname.lower()] = rel
                mapping[safe.lower()]  = rel
    return mapping

def build_b64_imap(imap_rel, post_dir):
    """Return a copy of imap_rel with values replaced by base64 data URIs.
    Used so WXR files are self-contained (no external image references)."""
    b64 = {}
    for key, rel in imap_rel.items():
        abs_path = os.path.join(post_dir, rel)
        if os.path.exists(abs_path):
            try:
                mime, _ = mimetypes.guess_type(abs_path)
                if not mime: mime = 'image/jpeg'
                with open(abs_path, 'rb') as f:
                    data = base64.b64encode(f.read()).decode()
                b64[key] = f"data:{mime};base64,{data}"
            except Exception:
                b64[key] = rel   # fallback: keep relative path
        else:
            b64[key] = rel
    return b64

#  HTML output template (unchanged from v2) 
HTML_T = """\
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<style>
  body{{font-family:Georgia,serif;font-size:16px;line-height:1.7;
        max-width:760px;margin:40px auto;padding:0 20px;color:#222}}
  h1  {{font-size:1.5em;border-bottom:1px solid #ccc;padding-bottom:8px}}
  .meta{{color:#666;font-size:.85em;margin-bottom:24px}}
  div {{margin:4px 0}} a{{color:#0066cc}} img{{max-width:100%;height:auto}}
</style>
</head>
<body>
<h1>{title}</h1>
{body}
</body>
</html>
"""

#  WXR output helpers 
def wxr_header(pub_date):
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!-- WordPress eXtended RSS (WXR) 1.2  –  {BLOG_TITLE} -->
<rss version="2.0"
  xmlns:excerpt="http://wordpress.org/export/1.2/excerpt/"
  xmlns:content="http://purl.org/rss/1.0/modules/content/"
  xmlns:wfw="http://wellformedweb.org/CommentAPI/"
  xmlns:dc="http://purl.org/dc/elements/1.1/"
  xmlns:wp="http://wordpress.org/export/1.2/">
<channel>
  <title>{saxutils.escape(BLOG_TITLE)}</title>
  <link>{SITE_URL}</link>
  <description></description>
  <pubDate>{pub_date}</pubDate>
  <language>es</language>
  <wp:wxr_version>1.2</wp:wxr_version>
  <wp:base_site_url>{SITE_URL}</wp:base_site_url>
  <wp:base_blog_url>{SITE_URL}</wp:base_blog_url>
  <wp:author>
    <wp:author_id>1</wp:author_id>
    <wp:author_login><![CDATA[ivan]]></wp:author_login>
    <wp:author_email><![CDATA[ivan@blog]]></wp:author_email>
    <wp:author_display_name><![CDATA[Iván García]]></wp:author_display_name>
    <wp:author_first_name><![CDATA[Iván]]></wp:author_first_name>
    <wp:author_last_name><![CDATA[García]]></wp:author_last_name>
  </wp:author>
  <wp:author>
    <wp:author_id>2</wp:author_id>
    <wp:author_login><![CDATA[tania]]></wp:author_login>
    <wp:author_email><![CDATA[tania@blog]]></wp:author_email>
    <wp:author_display_name><![CDATA[Tania Quintero]]></wp:author_display_name>
    <wp:author_first_name><![CDATA[Tania]]></wp:author_first_name>
    <wp:author_last_name><![CDATA[Quintero]]></wp:author_last_name>
  </wp:author>
  <wp:category>
    <wp:term_id>1</wp:term_id>
    <wp:category_nicename><![CDATA[blog-ivan]]></wp:category_nicename>
    <wp:category_parent></wp:category_parent>
    <wp:cat_name><![CDATA[Blog Iván]]></wp:cat_name>
  </wp:category>
  <wp:category>
    <wp:term_id>2</wp:term_id>
    <wp:category_nicename><![CDATA[blog-tania]]></wp:category_nicename>
    <wp:category_parent></wp:category_parent>
    <wp:cat_name><![CDATA[Blog Tania]]></wp:cat_name>
  </wp:category>
"""

WXR_FOOTER = "</channel>\n</rss>\n"

def slugify(text):
    """Convert title to a URL-safe slug."""
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode()
    text = re.sub(r'[^\w\s-]', '', text).strip().lower()
    return re.sub(r'[-\s]+', '-', text)[:80].strip('-')

def make_wxr_item(post_id, info, body_html, sent_date):
    """Build one WXR <item> XML string for a single post."""
    author_login, _ = AUTHOR_WP.get(info["blog"], AUTHOR_WP["UNKNOWN"])
    cat_nicename    = "blog-ivan"  if info["blog"] == "IVAN"  else "blog-tania"
    cat_name        = "Blog Iván"  if info["blog"] == "IVAN"  else "Blog Tania"

    post_date = info["post_date"] or sent_date or datetime.now()
    rfc2822   = post_date.strftime("%a, %d %b %Y %H:%M:%S +0000")
    wp_date   = post_date.strftime("%Y-%m-%d %H:%M:%S")

    slug = slugify(info["title"]) or f"post-{post_id:04d}"

    # WXR content goes inside CDATA; escape any accidental ]]> in the body
    safe_body = body_html.replace("]]>", "]]]]><![CDATA[>")

    return (
        f'  <item>\n'
        f'    <title>{saxutils.escape(info["title"])}</title>\n'
        f'    <link>{SITE_URL}/?p={post_id}</link>\n'
        f'    <pubDate>{rfc2822}</pubDate>\n'
        f'    <dc:creator><![CDATA[{author_login}]]></dc:creator>\n'
        f'    <category domain="category" nicename="{cat_nicename}">'
              f'<![CDATA[{cat_name}]]></category>\n'
        f'    <guid isPermaLink="false">{SITE_URL}/?p={post_id}</guid>\n'
        f'    <description></description>\n'
        f'    <content:encoded><![CDATA[{safe_body}]]></content:encoded>\n'
        f'    <excerpt:encoded><![CDATA[]]></excerpt:encoded>\n'
        f'    <wp:post_id>{post_id}</wp:post_id>\n'
        f'    <wp:post_date><![CDATA[{wp_date}]]></wp:post_date>\n'
        f'    <wp:post_date_gmt><![CDATA[{wp_date}]]></wp:post_date_gmt>\n'
        f'    <wp:comment_status><![CDATA[open]]></wp:comment_status>\n'
        f'    <wp:ping_status><![CDATA[open]]></wp:ping_status>\n'
        f'    <wp:post_name><![CDATA[{slug}]]></wp:post_name>\n'
        f'    <wp:status><![CDATA[publish]]></wp:status>\n'
        f'    <wp:post_parent>0</wp:post_parent>\n'
        f'    <wp:menu_order>0</wp:menu_order>\n'
        f'    <wp:post_type><![CDATA[post]]></wp:post_type>\n'
        f'    <wp:post_password><![CDATA[]]></wp:post_password>\n'
        f'    <wp:is_sticky>0</wp:is_sticky>\n'
        f'  </item>\n'
    )

#  Per-email processor 
def process_eml(eml_path, output_dir, post_id):
    """
    Process one .eml file:
      1. Parse headers & subject
      2. Extract images to disk
      3. Write HTML post (relative image paths)
      4. Build & return WXR item XML (base64-embedded images)
    """
    with open(eml_path, 'rb') as f:
        msg = email.message_from_bytes(f.read())

    subject   = str(make_header(decode_header(msg['subject'] or '')))
    sent_date = None
    try:
        sent_date = parsedate_to_datetime(msg['date'])
    except Exception:
        pass

    info     = parse_subject(subject, sent_date)
    date_str = (info["post_date"] or sent_date or datetime.now()).strftime("%Y-%m-%d")
    base     = f"{date_str}_{info['blog']}_{info['seq_num']:04d}"
    post_dir = os.path.join(output_dir, base)
    os.makedirs(post_dir, exist_ok=True)

    # Extract images once; imap_rel holds  filename  "images/safe_name.jpg"
    imap_rel = extract_images(msg, os.path.join(post_dir, "images"))

    # Get plain-text body
    plain = ""
    for part in msg.walk():
        if part.get_content_type() == 'text/plain':
            plain = part.get_payload(decode=True).decode('utf-8', errors='replace')
            break

    #  HTML output (uses relative image paths – works when opened locally) 
    body_html = convert(plain, imap_rel)
    html_out  = HTML_T.format(
        title = info["title"],
        body      = body_html,
    )
    with open(os.path.join(post_dir, "post.html"), 'w', encoding='utf-8') as f:
        f.write(html_out)

    #  WXR item (images embedded as base64 data URIs – self-contained) 
    #  WXR item (images as placeholder comments – keeps XML small)
    imap_placeholder = build_placeholder_imap(imap_rel)
    body_wxr = convert(plain, imap_placeholder, skip_title=info['title'], skip_first_image=True)

    # Find first image for featured image (uploader reads meta.json)
    first_image = None
    img_dir_path = os.path.join(post_dir, "images")
    if os.path.exists(img_dir_path):
        files = sorted(os.listdir(img_dir_path))
        if files:
            first_image = files[0]
    import json as _json
    with open(os.path.join(post_dir, "meta.json"), 'w', encoding='utf-8') as mf:
        _json.dump({"title": info["title"], "first_image": first_image}, mf, ensure_ascii=False)
    item_xml = make_wxr_item(post_id, info, body_wxr, sent_date)

    nc = len(os.listdir(os.path.join(post_dir, "images"))) \
         if os.path.exists(os.path.join(post_dir, "images")) else 0

    return dict(
        base     = base,
        title    = info["title"],
        blog     = info["blog"],
        date     = date_str,
        images   = nc,
        item_xml = item_xml,
    )

#  Main 
def main():
    inp = sys.argv[1] if len(sys.argv) > 1 else r'E:\Tania'
    out = sys.argv[2] if len(sys.argv) > 2 else r'E:\TaniaOut'
    os.makedirs(out, exist_ok=True)

    emls = sorted(f for f in os.listdir(inp) if f.endswith('.eml'))
    if not emls:
        print("No .eml files found in", inp)
        return

    total        = len(emls)
    total_chunks = (total + POSTS_PER_CHUNK - 1) // POSTS_PER_CHUNK
    wxr_dir      = os.path.join(out, "wxr")
    os.makedirs(wxr_dir, exist_ok=True)
    now_rfc      = datetime.now().strftime("%a, %d %b %Y %H:%M:%S +0000")

    print(f"{'='*65}")
    print(f"  Input  : {inp}  ({total} emails)")
    print(f"  Output : {out}")
    print(f"  WXR    : {total_chunks} chunk files  ({POSTS_PER_CHUNK} posts each)")
    print(f"{'='*65}\n")

    chunk_num   = 0
    chunk_count = 0
    wxr_file    = None
    ok          = 0
    errors      = []

    for post_id, eml_name in enumerate(emls, 1):

        # Open a new WXR chunk file when needed
        if chunk_count == 0:
            chunk_num += 1
            if wxr_file is not None:
                wxr_file.write(WXR_FOOTER)
                wxr_file.close()
            fname    = os.path.join(wxr_dir, f"wxr_{chunk_num:03d}_of_{total_chunks:03d}.xml")
            wxr_file = open(fname, 'w', encoding='utf-8')
            wxr_file.write(wxr_header(now_rfc))

        try:
            r = process_eml(os.path.join(inp, eml_name), out, post_id)
            wxr_file.write(r["item_xml"])
            chunk_count += 1
            ok          += 1
            img_tag = f"({r['images']} img{'s' if r['images']!=1 else ''})"
            print(f"  [{r['blog']:5}] {r['date']}  \"{r['title'][:55]}\"  {img_tag}")
        except Exception as e:
            import traceback
            msg_short = str(e)[:80]
            print(f"  ERROR  {eml_name}: {msg_short}")
            errors.append((eml_name, str(e)))
            traceback.print_exc()

        if chunk_count >= POSTS_PER_CHUNK:
            chunk_count = 0   # will trigger new file on next post

    # Close the last open WXR file
    if wxr_file is not None and not wxr_file.closed:
        wxr_file.write(WXR_FOOTER)
        wxr_file.close()

    #  Summary 
    print(f"\n{'='*65}")
    print(f"  Done:  {ok}/{total} posts converted,  {len(errors)} errors")
    print(f"  HTML:  {out}")
    print(f"  WXR :  {wxr_dir}  ({total_chunks} files)")
    if errors:
        print(f"\n  Failed files:")
        for name, err in errors:
            print(f"    {name}: {err}")
    print(f"\n  Next step:")
    print(f"    WordPress  Tools  Import  WordPress (WXR importer)")
    print(f"    Upload  wxr_001_of_{total_chunks:03d}.xml  first, then the rest in order.")
    print(f"    When prompted, map  ivan / tania  to their WordPress.com accounts.")
    print(f"{'='*65}")

if __name__ == "__main__":
    main()
    
