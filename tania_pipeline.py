#!/usr/bin/env python3
"""
tania_pipeline.py
Converts Tania/Ivan blog emails (.eml) to WordPress WXR import files,
using Claude API to handle all the intelligent content processing.

Usage:
    python tania_pipeline.py <input_folder> <output_folder> [options]

Options:
    --no-upload     Skip WordPress upload, generate review HTML only
    --draft         Create posts as drafts instead of publishing directly
    --test FILE     Process only the specified .eml file(s), semicolon-separated

Requirements:
    pip install anthropic

Environment:
    ANTHROPIC_API_KEY  must be set

Output:
    <output_folder>/blog_ivan.wxr        WordPress import for Blog Ivan
    <output_folder>/blog_tania.wxr       WordPress import for Blog Tania
    <output_folder>/review_report.html   Human review before publishing
    <output_folder>/images/              All extracted post images
"""

import email
import json
import mimetypes
import os
import re
import sys
import time
import hashlib
import unicodedata
import urllib.request
import urllib.parse
import urllib.error
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from datetime import datetime
from pathlib import Path

try:
    import anthropic
except ImportError:
    print("Error: anthropic package not installed.")
    print("Run: pip install anthropic")
    sys.exit(1)

# ── Configuration ─────────────────────────────────────────────────────────────
# Update these URLs once the WordPress blogs are set up

BLOG_URL  = "https://elblogdeivanytania.com"
BLOG_NAME = "El blog de Iván García y Tania Quintero"

# Mapping from subject line blog tag to full original blog name
# Used to generate "Publicado originalmente en..." footer line
BLOG_ORIGIN_NAMES = {
    "BLOG IVAN":           "El Blog de Iván García",
    "BLOG TANIA":          "El Blog de Tania Quintero",
    "BLOG DESDE LA HABANA": "Blog Desde La Habana",
    # Add more blogs here as Tania merges them:
    # "BLOG DE LAS AMERICAS": "Blog de las Américas",
}
BLOG_ORIGIN_DEFAULT = "El Blog de Tania Quintero"   # if no tag found in subject

CLAUDE_MODEL      = "claude-sonnet-4-6"
POSTS_PER_REPORT  = 100  # number of posts per review/WXR chunk file

# ── WordPress.com API config ──────────────────────────────────────────────────
# Set WP_UPLOAD = True to upload posts directly to WordPress (recommended)
# Set WP_UPLOAD = False to generate WXR files only (legacy mode)
WP_UPLOAD      = True
WP_SITE        = "blogtaniaquintero-lvghq.wordpress.com"
WP_CLIENT_ID   = "137919"    # from developer.wordpress.com/apps
WP_CLIENT_SECRET = "m4zuAY4MqD7XpBRSdwIFj4mhnQ553Yv8ztV2rGls9JiLPC5W4A0TWaDSPUgtrBBA"
WP_REDIRECT_URI  = "https://elblogdeivanytania.com"
WP_TOKEN_FILE    = "wp_token.txt"    # saved token — avoids re-authorizing each run
WP_API         = "https://public-api.wordpress.com/rest/v1.1"

MONTHS_ES = {
    "ENERO": 1, "FEBRERO": 2, "MARZO": 3, "ABRIL": 4,
    "MAYO": 5, "JUNIO": 6, "JULIO": 7, "AGOSTO": 8,
    "SEPTIEMBRE": 9, "OCTUBRE": 10, "NOVIEMBRE": 11, "DICIEMBRE": 12,
}

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are processing emails from a Cuban journalism blog (Blog Ivan / Blog Tania)
to convert them to clean WordPress post HTML. The emails were written in Spanish by Tania Quintero,
an 80-year-old Cuban journalist living in Switzerland, and her son Iván García. They are not
technical people so formatting is inconsistent — apply good judgment, not rigid rules.

You will receive a JSON object with:
  subject       - the email subject line
  sender        - sender email address
  sent_date     - YYYY-MM-DD (derived from email date + subject day/month)
  body_html     - full HTML body of the email
  body_text     - plain text body
  attachments   - list of image filenames (already extracted, in MIME order)

Return your response in TWO clearly separated sections using these exact delimiters:

###META###
{
  "title":          "clean post title (string)",
  "author":         "Author Name or null",
  "featured_type":  "image" | "video" | "none",
  "featured_image": "filename.jpg or null",
  "featured_video": "https://www.youtube.com/embed/VIDEO_ID or null",
  "flags":          ["list of warning strings for human review"]
}
###HTML###
complete processed HTML for the WordPress post body goes here — raw HTML, no JSON encoding
###END###

The META section must be valid JSON. The HTML section is raw HTML — do NOT JSON-encode it.
Do not add any text outside these delimiters.

════════════════════════════════════════════
TITLE EXTRACTION
════════════════════════════════════════════
The clean title is almost always the first line of the body text.
Verify it appears loosely in the subject line (case-insensitive, accent-insensitive, partial match OK).
If confirmed → use the body's first line as the title.

If the body's first line is NOT in the subject, fall back to extracting from the subject:
take text between the colon after the date and "BLOG IVAN" or "BLOG TANIA", then:
  - KEEP roman numerals in parentheses: (I), (II), (III), (IV), (V) etc.
  - Strip all other parenthetical content
  - Strip everything from "OJO:" onwards (editing notes to Marco)
  - Strip trailing instruction words before BLOG: link, links, foto, fotos, video,
    al final, en el texto, al inicio, and combinations thereof

════════════════════════════════════════════
AUTHOR DETECTION
════════════════════════════════════════════
A standalone block of ≤5 words that sits immediately before a recognized source/date line
is the author byline — render it bold regardless of whether it was bold in the original.

Recognized source/date patterns begin with: publication name + comma + date, or
"Texto y foto", "Texto y fotos", "Fotos tomadas de", "Las fotos fueron tomadas de", etc.

If uncertain whether a block is an author name, add a flag but still make your best guess.

════════════════════════════════════════════
HTML PROCESSING
════════════════════════════════════════════

GENERAL CLEANUP:
  - Preserve <i> and <em> tags exactly — they mark verbatim quotes or editorial emphasis
  - Remove <b> and <strong> tags but keep their text content (exceptions: footer elements below)
  - Strip excessive inline CSS, font tags, color attributes, direction attributes
  - Preserve paragraph structure with clean <p> tags
  - Convert <br><br> sequences to paragraph breaks where appropriate

LINK INVERSION (applies everywhere in the body, not just footers):
  Tania's format: "anchor text (url)" or in HTML: text (<a href="url">url</a>) or text (<a href="url">url</a>)
  Transform to: <a href="url">anchor text</a>
  
  Use reading comprehension to identify the anchor text — it is typically the named entity,
  proper noun, title, or meaningful noun phrase immediately before the parenthesis.
  Do NOT include connecting words (de, en, la, el, y) unless they are part of the proper name.
  Do NOT include the opening parenthesis in the anchor text.
  
  Examples:
    "Finca de los Monos (https://...)" → <a href="https://...">Finca de los Monos</a>
    "La muchacha de Santa Clara que componía boleros (https://...)" → <a href="https://...">La muchacha de Santa Clara que componía boleros</a>
    "publicado en Rockdelux (https://...)" → <a href="https://...">Rockdelux</a>

YOUTUBE VIDEO EMBEDS:
  Detect when any of these patterns appear:
    1. "insertar video" (case-insensitive) in the same block as a YouTube URL
    2. Any text ending in a colon immediately before a YouTube URL
    3. A YouTube video title followed by "(youtube.com)" then a YouTube URL
  
  Strip the entire instruction/label and replace with:
  <div class="video-embed"><iframe width="100%" height="400"
    src="https://www.youtube.com/embed/VIDEO_ID" frameborder="0"
    allowfullscreen></iframe></div>
  
  Extract VIDEO_ID from: watch?v=VIDEO_ID, youtu.be/VIDEO_ID, or embed/VIDEO_ID
  
  A YouTube embed appearing BEFORE any body text → featured_type: "video",
  featured_video: the embed URL.

PHOTO INSERTION:
  Match photo markers to the attachments list (which is in MIME order):
  
  Pattern 1 — "insertar foto N" (case-insensitive, N is a digit):
    → Replace with the Nth attachment (1-based index)
  
  Pattern 2 — "insertar foto [description]" with no number, single attachment:
    → Replace with the only attachment, no caption
  
  Pattern 3 — "insertar las fotos por el mismo orden" or similar global instruction
    covering all photos at once:
    → Replace instruction with all attachments in MIME order, one after another
  
  Pattern 4 — "Foto N description" (bold/prominent standalone line, N is a digit):
    → Replace with Nth attachment, use description text as caption
  
  Always render placed images as:
    <figure class="post-image">
      <img src="images/FILENAME" alt="CAPTION">
      <figcaption>CAPTION</figcaption>      ← omit if no caption
    </figure>

  FEATURED IMAGE RULE — simple and absolute:

  STEP 1 — ORDER OF IMAGES:
  For CID inline images (embedded directly in HTML body), use the order they
  appear visually top-to-bottom in the HTML body — NOT MIME part order.
  For regular file attachments, use MIME attachment order.
  The attachments list you receive is already in MIME order.
  When CID images are present, re-order them by their visual position in the HTML.

  STEP 2 — FEATURED IMAGE (only applies when there is NO top video):
  The FIRST image is the featured image. It NEVER appears in the body.
  Set featured_type: "image", featured_image: first image filename.
  All remaining images (2nd, 3rd...) go in the body at their markers.
  Marker "insertar foto 1" refers to attachment 1 = featured → SKIP it silently.
  "insertar foto 2" → place attachment 2. "insertar foto 3" → attachment 3, etc.

  Concretely (NO top video):
  - 1 image → featured only, body has no images
  - 2 images → image 1 featured, image 2 in body
  - 3 images → image 1 featured, images 2+3 in body
  - 10 images (gallery) → image 1 featured, images 2-10 in body in order

  STEP 3 — IMAGES WHEN THERE IS A TOP VIDEO:
  When featured_type="video", the video takes the featured role.
  ALL images go in the body at their marker positions — no image is reserved
  as featured-only. The first image is ALSO set as featured_image for the
  WordPress thumbnail, but it still appears in the body at its marker.
  Do NOT skip "insertar foto 1" — place it in the body normally.

  Concretely (WITH top video):
  - 1 image + video → video featured, image goes in body at its marker,
    featured_image = that image for thumbnail
  - 2 images + video → video featured, both images go in body at their markers,
    featured_image = first image for thumbnail
  - No images + video → video featured, featured_image = null

  STEP 4 — FEATURED VIDEO OVERRIDES FEATURED IMAGE:
  A "top video" is ANY YouTube URL that appears before the main body text,
  regardless of format:
    - "insertar video de X en Y:" followed by a YouTube URL
    - A label ending in colon followed by a YouTube URL on the next line
    - "Insertar video: Title (youtube.com)" followed by a YouTube URL
    - A YouTube URL appearing as the very first content element
    - A bold YouTube label (*Label:* followed by URL) near the top

  CRITICAL: The post TITLE (first line, same as subject) does NOT count as
  body text. A video is "at the top" if it appears before any substantive
  paragraph of body content, even if the title comes before it in the HTML.
  In practice: if the video instruction appears within the first 2-3 block
  elements after the title, treat it as a top video.
  
  When a top video is detected:
  → featured_type MUST be "video" — this overrides everything else
  → featured_video MUST be the YouTube embed URL (https://www.youtube.com/embed/VIDEO_ID)
  → Do NOT place the video in the body content
  → Do NOT place any iframe in the body content for this video
  → featured_image = first image filename if one exists (for thumbnail), else null
  → First image still does NOT appear in body (same rule as always)
  → Only images 2, 3, etc. go in body

  IMPORTANT: Even when there is an attached image, if there is a top video,
  featured_type is "video" NOT "image". The image becomes featured_image only.

  If no top video: featured_type="image" if images exist, "none" if not.
  If no images at all: featured_image: null.


SEPARATORS:
  Lines consisting only of asterisks/spaces like "* * * * *" → <hr class="post-separator">

════════════════════════════════════════════
OPENING EDITORIAL NOTE
════════════════════════════════════════════
If the post begins with an italic paragraph BEFORE the main body text — Tania explaining
context, introducing the article, or writing "A modo de introducción" — treat it as an
opening note with the same style as closing Nota.- paragraphs:
  <p class="post-nota"><strong>Nota.-</strong> <small>italic note text</small></p>

════════════════════════════════════════════
ORIGINAL BLOG ATTRIBUTION
════════════════════════════════════════════
The input JSON contains a "blog_origin" field with the name of the original blog
where this post was published. Add this as the very last line of the post content,
after all footer elements (after Leer también, Escuchar, etc.):

  <p class="post-origin"><em>Publicado originalmente en [blog_origin].</em></p>

════════════════════════════════════════════
FOOTER STRUCTURE
════════════════════════════════════════════
Process everything after the main article body in this order:

1. AUTHOR NAME (bold):
   <p class="post-author"><strong>Author Name</strong></p>

2. SOURCE/DATE LINE (small bold):
   <p class="post-source"><strong>Cubanet, 24 de febrero de 2024.</strong></p>

3. MEDIA ATTRIBUTION — lines starting with Foto:, Fotos:, Video:, Texto y foto:,
   Texto y fotos:, or prose variants ("Las fotos fueron tomadas de...", "Foto tomada de..."):
   <p class="post-caption"><strong><em>Foto: attribution text</em></strong></p>

4. NOTA (closing note) — any prose paragraph after the author byline that is:
   - NOT a link block (Leer también, Ver también, etc.)
   - NOT a source/date line
   - NOT a media attribution
   Always prepend "Nota.-" in bold even if Tania forgot to write it:
   <p class="post-nota"><strong>Nota.-</strong> <small>note text, may contain links</small></p>

5. FOOTER LINK BLOCKS — lines starting with Leer también:, Ver también:, Escuchar, Video (...):
   Apply the same link inversion logic as body links.
   Style:
   <p class="post-links"><strong><em>Leer también:</em></strong>
     <a href="url1">anchor text 1</a> y <a href="url2">anchor text 2</a>.</p>

════════════════════════════════════════════
ALERTAS — añade al array flags cuando:
════════════════════════════════════════════
  Escribe TODAS las alertas en español. Son para Tania, una periodista cubana de 80 años.
  Usa un tono claro y amable, evita tecnicismos.

  - El título no pudo confirmarse con certeza → "El título fue tomado del cuerpo del texto porque no coincidía claramente con el asunto del correo. Por favor verifica que sea correcto."
  - El autor no pudo detectarse con certeza → "No se pudo identificar el autor con certeza. Por favor indica quién escribió este post."
  - Una nota o pie de foto menciona una imagen pero no se encontró el archivo → "La nota menciona una foto ('DESCRIPCION') pero no se encontró ninguna imagen adjunta. Es posible que se haya perdido."
  - Un marcador 'insertar foto N' existe pero no hay adjunto N → "El texto pide insertar la foto número N pero no hay suficientes imágenes adjuntas."
  - Un patrón 'insertar ...' no reconocido → "Se encontró una instrucción no reconocida en el texto: 'INSTRUCCION'. Por favor revisa si falta algo."
  - El asunto contenía 'OJO:' → "El asunto del correo contenía esta nota editorial: 'TEXTO DEL OJO'. Por favor verifica que se haya aplicado correctamente."
  - Hay marcadores de foto pero cero adjuntos → "El post hace referencia a fotos pero no se encontró ninguna imagen. Es posible que las imágenes se hayan perdido."
  - NO generar alerta cuando: una imagen sin marcador se usa como imagen destacada (comportamiento normal esperado)
  - NO generar alerta cuando: una sola imagen se coloca en el único punto de inserción del texto (comportamiento normal esperado)
  - NO generar alerta cuando: la Nota o pie de foto menciona la imagen y ésta se usa como imagen destacada — es el comportamiento correcto y esperado
  - NO generar alerta cuando: una imagen CID (insertada directamente en el cuerpo del correo) se coloca según su posición en el texto original
  - Cualquier otra situación genuinamente ambigua que requiera una decisión humana
"""

# ── Helpers ───────────────────────────────────────────────────────────────────

def decode_str(s):
    """Decode an encoded email header string."""
    if not s:
        return ""
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return s

def normalize(s):
    """Lowercase, strip accents, collapse whitespace — for loose string matching."""
    s = unicodedata.normalize('NFD', s.lower())
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    s = re.sub(r'[^\w\s]', ' ', s)
    return ' '.join(s.split())

def slugify(title):
    """Create a URL-safe slug from a title."""
    s = normalize(title)
    s = re.sub(r'\s+', '-', s)
    return s.strip('-')[:80]

def xml_cdata_safe(s):
    """Make a string safe for use inside XML CDATA sections."""
    return s.replace(']]>', ']]]]><![CDATA[>')

def escape_xml(s):
    """Escape XML special characters for use outside CDATA."""
    return (s.replace('&', '&amp;')
             .replace('<', '&lt;')
             .replace('>', '&gt;')
             .replace('"', '&quot;'))

# ── EML parsing ───────────────────────────────────────────────────────────────

def parse_subject_new_format(subject):
    """
    Parse new format: YYYY-MM-DD TITLE (BLOG TAG)
    For future posts only. Returns dict or None.
    """
    m = re.match(
        r'(\d{4})-(\d{2})-(\d{2})\s+(.+?)\s+(BLOG\s+[A-Z\s]+?)\s*$',
        subject.strip(), re.IGNORECASE
    )
    if not m:
        return None
    matched_tag = m.group(5).upper().strip()
    blog_key = next((k for k in BLOG_ORIGIN_NAMES if k in matched_tag), None)
    try:
        dt = datetime.strptime(m.group(1) + '-' + m.group(2) + '-' + m.group(3), '%Y-%m-%d')
        return {
            "post_num":   1,
            "day":        dt.day,
            "month":      dt.month,
            "month_str":  "",
            "raw_title":  m.group(4).strip(),
            "blog_origin": BLOG_ORIGIN_NAMES.get(blog_key, BLOG_ORIGIN_DEFAULT),
            "year_override": dt.year,
        }
    except ValueError:
        return None


def parse_subject(subject):
    """
    Parse: N) [WEEKDAY] DD MONTH[.][:]  Title ... [BLOG IVAN|TANIA] [OJO: ...]
    All optional elements handled gracefully.
    Returns dict or None.
    """
    WORD = r'[A-Za-z\u00c0-\u024f]+'

    # Try with BLOG tag (to strip it from raw_title cleanly)
    # Match any known BLOG tag
    blog_pattern = '|'.join(re.escape(k) for k in BLOG_ORIGIN_NAMES)
    m = re.match(
        rf'(\d+)\)\s+(?:{WORD}\s+)?(\d+)\s+({WORD})[\s.:]*(.+?)\s+({blog_pattern})',
        subject, re.IGNORECASE
    )
    if m:
        month_str = m.group(3).upper()
        if month_str in MONTHS_ES:
            # Normalise the matched blog tag to a canonical key
            matched_tag = m.group(5).upper().strip()
            blog_key = next((k for k in BLOG_ORIGIN_NAMES if k in matched_tag), None)
            return {
                "post_num":   int(m.group(1)),
                "day":        int(m.group(2)),
                "month":      MONTHS_ES[month_str],
                "month_str":  month_str,
                "raw_title":  m.group(4).strip(),
                "blog_origin": BLOG_ORIGIN_NAMES.get(blog_key, BLOG_ORIGIN_DEFAULT),
            }

    # Fallback: no BLOG tag — grab everything after month as raw title
    m2 = re.match(
        rf'(\d+)\)\s+(?:{WORD}\s+)?(\d+)\s+({WORD})[\s.:]*(.+)',
        subject, re.IGNORECASE
    )
    if m2:
        month_str = m2.group(3).upper()
        if month_str in MONTHS_ES:
            return {
                "post_num":   int(m2.group(1)),
                "day":        int(m2.group(2)),
                "month":      MONTHS_ES[month_str],
                "month_str":  month_str,
                "raw_title":  m2.group(4).strip(),
                "blog_origin": BLOG_ORIGIN_DEFAULT,  # no tag found — assume Tania
            }
    return None


def get_sender_email(msg):
    """Extract bare email address from From header."""
    from_h = msg.get('From', '')
    m = re.search(r'<(.+?)>', from_h)
    return m.group(1).lower() if m else from_h.strip().lower()

def get_sent_year(msg):
    """Extract year from Date header."""
    try:
        return parsedate_to_datetime(msg.get('Date', '')).year
    except Exception:
        return datetime.now().year

def get_html_body(msg):
    """Return the HTML body part, decoded."""
    for part in msg.walk():
        if part.get_content_type() == 'text/html':
            raw = part.get_payload(decode=True)
            return raw.decode('utf-8', errors='replace') if raw else ""
    return ""

def get_text_body(msg):
    """Return the plain text body part, decoded."""
    for part in msg.walk():
        if part.get_content_type() == 'text/plain':
            raw = part.get_payload(decode=True)
            return raw.decode('utf-8', errors='replace') if raw else ""
    return ""

def extract_attachments(msg, images_dir, prefix):
    """
    Extract all images from the email — both regular file attachments
    (Content-Disposition: attachment) and CID inline images embedded in
    the HTML body (Content-Disposition: inline or no disposition).
    Saves to images_dir with a prefix. Returns list of saved filenames.
    """
    attachments = []
    seen_cids = set()

    for part in msg.walk():
        ct = part.get_content_type()
        if not ct.startswith('image/'):
            continue

        disp = part.get('Content-Disposition', '')
        cid  = part.get('Content-ID', '').strip('<>').strip()

        # Skip duplicates (same CID seen twice)
        if cid and cid in seen_cids:
            continue
        if cid:
            seen_cids.add(cid)

        # Accept explicit attachments, inline images, and anything with a CID
        is_attachment = 'attachment' in disp
        is_inline     = 'inline' in disp or bool(cid)
        if not (is_attachment or is_inline):
            continue

        raw_name = part.get_filename() or part.get_param('name') or ''
        filename = decode_str(raw_name) if '=?' in raw_name else raw_name
        if not filename:
            if cid:
                filename = re.sub(r'[^\w.]', '_', cid)
            else:
                ext = ct.split('/')[-1] or 'jpg'
                filename = f"image_{len(attachments)+1}.{ext}"

        filename  = re.sub(r'[<>:"/\\|?*\r\n]', '_', filename).strip()

        # If a file with this name already exists (same base filename, different CID),
        # embed the CID into the filename to make it unique
        save_name = f"{prefix}_{filename}"
        if (images_dir / save_name).exists() and cid:
            stem, ext = (filename.rsplit('.', 1) + ['jpg'])[:2] if '.' in filename else (filename, 'jpg')
            save_name = f"{prefix}_{stem}_{re.sub(r'[^\w]', '', cid)}.{ext}"
        data = part.get_payload(decode=True)
        if data:
            (images_dir / save_name).write_bytes(data)
            attachments.append(save_name)

    return attachments

def reorder_attachments_by_visual(attachments, html_body, msg):
    """
    For emails where images are CID-embedded, the MIME attachment order
    may not match the visual order in the HTML body. This function reorders
    the attachments list to match the visual top-to-bottom order of CID
    references in the HTML, so Claude receives them in the correct order.
    Non-CID (regular file) attachments are left in MIME order at the end.
    """
    import re as _re

    # Build a map from CID → saved filename
    cid_to_filename = {}
    for part in msg.walk():
        cid = part.get('Content-ID', '').strip('<>').strip()
        if not cid:
            continue
        raw_name = part.get_filename() or part.get_param('name') or ''
        filename = decode_str(raw_name) if '=?' in raw_name else raw_name
        if not filename:
            ct = part.get_content_type()
            filename = f"image.{ct.split('/')[-1] or 'jpg'}"
        filename = _re.sub(r'[<>:"/\\|?*\r\n]', '_', filename).strip()
        cid_to_filename[cid] = filename

    if not cid_to_filename:
        return attachments  # no CID images, nothing to reorder

    # Extract CID references from HTML in visual (top-to-bottom) order
    cid_refs = _re.findall(r'src=["\']cid:([^"\']+)["\']', html_body)
    if not cid_refs:
        return attachments

    # Build reordered list: CID images in visual order first, then the rest
    cid_ordered = []
    seen = set()
    for cid in cid_refs:
        cid = cid.strip()
        # Find the saved filename that corresponds to this CID
        base = cid_to_filename.get(cid, '')
        # Match against the prefixed filenames in attachments
        # Match by CID first (most reliable), then fall back to base filename
        cid_clean = re.sub(r'[^\w]', '', cid)
        match = next((a for a in attachments if cid_clean in a and a not in seen), None)
        if not match:
            match = next((a for a in attachments if base and base in a and a not in seen), None)
        if match:
            cid_ordered.append(match)
            seen.add(match)

    # Append any remaining attachments not matched by CID (regular attachments)
    remaining = [a for a in attachments if a not in seen]
    return cid_ordered + remaining


def process_with_claude(client, subject_raw, sender, pub_date,
                        html_body, text_body, attachments, blog_origin):
    """Send email content to Claude and return processed JSON result.
    Retries once with a shorter body if the response is not valid JSON.
    """
    def _call(html_limit, text_limit):
        payload = {
            "subject":     subject_raw,
            "sender":      sender,
            "sent_date":   pub_date,
            "body_html":   html_body[:html_limit],
            "body_text":   text_body[:text_limit],
            "attachments": attachments,
            "blog_origin": blog_origin,
        }
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
        )
        raw = response.content[0].text.strip()

        # Extract META (JSON) and HTML sections using delimiters
        meta_match = re.search(r'###META###\s*(.+?)\s*###HTML###', raw, re.DOTALL)
        html_match = re.search(r'###HTML###\s*(.+?)\s*###END###', raw, re.DOTALL)

        if not meta_match or not html_match:
            # Fallback: try plain JSON (old format, in case model ignores instructions)
            cleaned = re.sub(r'^```(?:json)?\s*', '', raw)
            cleaned = re.sub(r'\s*```$', '', cleaned.strip())
            result = json.loads(cleaned)
            result.setdefault('content_html', '')
            return result

        result = json.loads(meta_match.group(1).strip())
        result['content_html'] = html_match.group(1).strip()
        return result

    try:
        return _call(60000, 25000)
    except (json.JSONDecodeError, ValueError):
        print(f"         ↻ JSON error — retrying with shorter body...")
        time.sleep(3)
        try:
            return _call(30000, 12000)
        except (json.JSONDecodeError, ValueError) as e:
            raise json.JSONDecodeError(f"Retry also failed: {e}", "", 0)

# ── WordPress.com API helpers ─────────────────────────────────────────────────

def wp_sanitize_filename(filename):
    """Mimic WordPress filename sanitization for matching uploaded files."""
    if "." in filename:
        name, ext = filename.rsplit(".", 1)
        ext = "." + ext.lower()
    else:
        name, ext = filename, ""
    name = unicodedata.normalize("NFD", name.lower())
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    name = re.sub(r"[^a-z0-9_]+", "-", name)
    name = name.strip("-")
    return name + ext


def wp_get_token(client_id, client_secret, redirect_uri, token_file):
    """Get WordPress.com OAuth token, using saved token if available."""
    # Try saved token first
    if os.path.exists(token_file):
        with open(token_file) as f:
            token = f.read().strip()
        if token:
            print(f"✓ Using saved WordPress token from {token_file}")
            return token

    # OAuth flow
    params = urllib.parse.urlencode({
        "client_id":     client_id,
        "redirect_uri":  redirect_uri,
        "response_type": "code",
        "scope":         "global",
    })
    auth_url = f"https://public-api.wordpress.com/oauth2/authorize?{params}"

    print("\n══════════════════════════════════════════════════════")
    print("WordPress.com Authorization needed.")
    print("Open this URL in your browser:")
    print(f"\n  {auth_url}\n")
    print(f"After authorizing, copy ONLY the 'code' value from the")
    print(f"redirect URL: {redirect_uri}?code=XXXXXXXX&state=...")
    print("══════════════════════════════════════════════════════\n")

    code = input("Paste the authorization code here: ").strip()
    # Strip &state=... if user accidentally included it
    code = code.split("&")[0]

    data = urllib.parse.urlencode({
        "client_id":     client_id,
        "client_secret": client_secret,
        "redirect_uri":  redirect_uri,
        "code":          code,
        "grant_type":    "authorization_code",
    }).encode()

    req = urllib.request.Request(
        "https://public-api.wordpress.com/oauth2/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST"
    )
    with urllib.request.urlopen(req) as r:
        result = json.loads(r.read())

    token = result.get("access_token")
    if not token:
        raise RuntimeError(f"Could not get token: {result}")

    # Save for future runs
    with open(token_file, "w") as f:
        f.write(token)
    print(f"✓ Token saved to {token_file} for future runs.\n")
    return token


def wp_api_get(site, token, endpoint, params=None):
    url = f"https://public-api.wordpress.com/rest/v1.1/sites/{site}{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def wp_api_post(site, token, endpoint, data=None, files=None):
    """POST to WordPress.com API. Use files= for multipart upload."""
    url = f"https://public-api.wordpress.com/rest/v1.1/sites/{site}{endpoint}"
    if files:
        # Multipart form upload for media
        boundary = "----TaniaBlogBoundary"
        body_parts = []
        for name, (fname, fdata, ftype) in files.items():
            body_parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; "
                f"name=\"{name}\"; filename=\"{fname}\"\r\n"
                f"Content-Type: {ftype}\r\n\r\n".encode() + fdata + b"\r\n"
            )
        body = b"".join(body_parts) + f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            url, data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST"
        )
    else:
        body = json.dumps(data or {}).encode()
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


def wp_upload_images(site, token, images_dir, post_attachments):
    """
    Upload all images for a set of posts to the WordPress media library.
    Returns a dict: local_filename → {url, id}
    """
    print("  Uploading images to WordPress...")
    media_map = {}   # local filename → {url, id}

    # Collect all unique image filenames needed
    all_files = set()
    for attachments in post_attachments:
        for fname in attachments:
            all_files.add(fname)

    for fname in sorted(all_files):
        fpath = images_dir / fname
        if not fpath.exists():
            print(f"    ⚠ Image not found: {fname}")
            continue
        if fname in media_map:
            continue   # already uploaded

        mime = mimetypes.guess_type(fname)[0] or "image/jpeg"
        try:
            fdata = fpath.read_bytes()
            result = wp_api_post(site, token, "/media/new", files={
                "media[]": (fname, fdata, mime)
            })
            media = result.get("media", [{}])[0]
            media_map[fname] = {
                "url": media.get("URL", ""),
                "id":  media.get("ID", 0),
            }
            print(f"    ✓ {fname}")
            time.sleep(0.3)
        except Exception as e:
            print(f"    ✗ {fname}: {e}")

    return media_map


def wp_fix_image_urls(html, attachments, media_map):
    """Replace local image/filename refs in HTML with WordPress URLs."""
    def replace_src(m):
        prefix = m.group(1)
        path   = m.group(2)
        suffix = m.group(3)
        # Skip YouTube and external URLs
        if "youtube" in path or path.startswith("http") or "version=3" in path:
            return m.group(0)
        fname = path.split("/")[-1]
        info = media_map.get(fname)
        if not info:
            # Try sanitized match
            san = wp_sanitize_filename(fname)
            info = next((v for k, v in media_map.items()
                         if wp_sanitize_filename(k) == san), None)
        if info:
            return f"{prefix}{info['url']}{suffix}"
        return m.group(0)

    return re.sub(r'(src=["\'])(?:images/)?([^"\']+)(["\'])', replace_src, html)


def convert_iframes_to_embeds(html):
    """
    WordPress.com API rejects <iframe> tags.
    Convert YouTube iframes to plain URLs which WordPress auto-embeds.
    """
    def replace_iframe(m):
        src = re.search(r'src=["\']([^"\']+)["\']', m.group(0))
        if not src:
            return ''
        url = src.group(1)
        # Convert embed URL to watch URL
        url = re.sub(r'https://www\.youtube\.com/embed/([\w-]+).*',
                     r'https://www.youtube.com/watch?v=\1', url)
        # WordPress auto-embeds bare YouTube URLs on their own line
        return f'\n\n{url}\n\n'
    # Replace entire video-embed divs containing iframes
    html = re.sub(r'<div[^>]*class="video-embed"[^>]*>.*?</div>',
                  replace_iframe, html, flags=re.DOTALL)
    # Also catch any remaining bare iframes
    html = re.sub(r'<iframe[^>]*src=["\']([^"\']*youtube[^"\']*)["\'][^>]*>.*?</iframe>',
                  lambda m: f'\n\nhttps://www.youtube.com/watch?v={re.search(r"embed/([\w-]+)", m.group(1)).group(1) if re.search(r"embed/([\w-]+)", m.group(1)) else ""}\n\n',
                  html, flags=re.DOTALL)
    return html


def wp_create_post(site, token, post, media_map, as_draft=False):
    """Create a WordPress post with correct image URLs, featured image and date."""
    html = wp_fix_image_urls(
        post.get("content_html", ""),
        post.get("attachments", []),
        media_map
    )
    # Convert iframes to plain URLs (WordPress.com API blocks iframes)
    html = convert_iframes_to_embeds(html)

    # When featured type is video, prepend the YouTube URL and strip it from body
    f_type = post.get("featured_type", "none")
    f_vid  = post.get("featured_video", "")
    if f_type == "video" and f_vid:
        # Convert embed URL to watch URL
        watch_url = re.sub(r'https://www\.youtube\.com/embed/([\w-]+).*',
                           r'https://www.youtube.com/watch?v=\1', f_vid)
        # Remove any occurrence of this URL from the body to avoid duplication
        html = html.replace(watch_url, '').replace(f_vid, '')
        html = re.sub(r'\n{3,}', '\n\n', html).strip()
        # Extract video ID for Gutenberg embed block (required for WP.com auto-embed)
        vid_match = re.search(r'watch\?v=([\w-]+)', watch_url)
        if vid_match:
            vid_id = vid_match.group(1)
            embed_block = (
                f'<!-- wp:embed {{"url":"{watch_url}","type":"video","providerNameSlug":"youtube"}} -->\n'
                f'<figure class="wp-block-embed is-type-video is-provider-youtube">'
                f'<div class="wp-block-embed__wrapper">\n{watch_url}\n</div></figure>\n'
                f'<!-- /wp:embed -->\n\n'
            )
            html = embed_block + html

    # Map author to a valid WordPress user login name
    raw_author = (post.get("author") or "").lower().strip()
    if "iv" in raw_author and "garc" in raw_author:
        wp_author = "ivangquintero"
    else:
        wp_author = "taniaquintero"   # all other authors → tania (she curates the blog)

    pub_date = post.get("date", "2000-01-01") + "T12:00:00"
    data = {
        "title":    post.get("title", "(sin título)"),
        "content":  html,
        "status":   "draft" if as_draft else "publish",
        "date":     pub_date,
        "date_gmt": pub_date,
        "author":   wp_author,
    }

    # Set featured image — use attached image or YouTube thumbnail as fallback
    f_img = post.get("featured_image")
    f_vid = post.get("featured_video", "")
    featured_id = 0

    if f_img:
        info = media_map.get(f_img)
        if not info:
            san = wp_sanitize_filename(f_img)
            info = next((v for k, v in media_map.items()
                         if wp_sanitize_filename(k) == san), None)
        if info and info.get("id"):
            featured_id = info["id"]

    if not featured_id and f_vid:
        # No image — use YouTube thumbnail as featured image
        vid_match = re.search(r'embed/([\w-]+)', f_vid)
        if vid_match:
            vid_id    = vid_match.group(1)
            thumb_url = f'https://img.youtube.com/vi/{vid_id}/hqdefault.jpg'
            try:
                thumb_data = urllib.request.urlopen(thumb_url).read()
                thumb_name = f'yt_thumb_{vid_id}.jpg'
                result = wp_api_post(site, token, "/media/new", files={
                    "media[]": (thumb_name, thumb_data, "image/jpeg")
                })
                media = result.get("media", [{}])[0]
                featured_id = media.get("ID", 0)
            except Exception as e:
                print(f"    ⚠ Could not fetch YouTube thumbnail: {e}")

    if featured_id:
        data["featured_image"] = featured_id

    return wp_api_post(site, token, "/posts/new", data)


# ── WXR builder ───────────────────────────────────────────────────────────────

def format_wxr_date(date_str):
    return f"{date_str} 00:00:00"

def format_rss_date(date_str):
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%a, %d %b %Y 00:00:00 +0000")
    except Exception:
        return date_str

def build_wxr(posts, blog_name, blog_url):
    """Assemble a WordPress WXR XML string from a list of processed posts."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0"',
        '    xmlns:excerpt="http://wordpress.org/export/1.2/excerpt/"',
        '    xmlns:content="http://purl.org/rss/1.0/modules/content/"',
        '    xmlns:wfw="http://wellformedweb.org/CommentAPI/"',
        '    xmlns:dc="http://purl.org/dc/elements/1.1/"',
        '    xmlns:wp="http://wordpress.org/export/1.2/">',
        '  <channel>',
        f'    <title>{escape_xml(blog_name)}</title>',
        f'    <link>{blog_url}</link>',
        '    <description></description>',
        f'    <pubDate>{datetime.now().strftime("%a, %d %b %Y %H:%M:%S +0000")}</pubDate>',
        '    <language>es</language>',
        '    <wp:wxr_version>1.2</wp:wxr_version>',
        f'    <wp:base_site_url>{blog_url}</wp:base_site_url>',
        f'    <wp:base_blog_url>{blog_url}</wp:base_blog_url>',
        '    <wp:author>',
        '      <wp:author_id>1</wp:author_id>',
        '      <wp:author_login><![CDATA[tania]]></wp:author_login>',
        '      <wp:author_email><![CDATA[tania@elblogdeivanytania.com]]></wp:author_email>',
        '      <wp:author_display_name><![CDATA[Tania Quintero]]></wp:author_display_name>',
        '      <wp:author_first_name><![CDATA[Tania]]></wp:author_first_name>',
        '      <wp:author_last_name><![CDATA[Quintero]]></wp:author_last_name>',
        '    </wp:author>',
        '    <wp:author>',
        '      <wp:author_id>2</wp:author_id>',
        '      <wp:author_login><![CDATA[ivan]]></wp:author_login>',
        '      <wp:author_email><![CDATA[ivan@elblogdeivanytania.com]]></wp:author_email>',
        '      <wp:author_display_name><![CDATA[Iván García]]></wp:author_display_name>',
        '      <wp:author_first_name><![CDATA[Iván]]></wp:author_first_name>',
        '      <wp:author_last_name><![CDATA[García]]></wp:author_last_name>',
        '    </wp:author>',
    ]

    for i, post in enumerate(posts, 1):
        author   = escape_xml(post.get('author') or 'tania')
        title    = escape_xml(post.get('title', '(sin título)'))
        slug     = slugify(post.get('title', f'post-{i}'))
        content  = xml_cdata_safe(post.get('content_html', ''))
        pub_date = post.get('date', '2000-01-01')

        lines += [
            '    <item>',
            f'      <title>{title}</title>',
            f'      <link>{blog_url}/{slug}/</link>',
            f'      <pubDate>{format_rss_date(pub_date)}</pubDate>',
            f'      <dc:creator>{author}</dc:creator>',
            f'      <content:encoded><![CDATA[{content}]]></content:encoded>',
            f'      <wp:post_id>{i}</wp:post_id>',
            f'      <wp:post_date>{format_wxr_date(pub_date)}</wp:post_date>',
            f'      <wp:post_date_gmt>{format_wxr_date(pub_date)}</wp:post_date_gmt>',
            '      <wp:comment_status>open</wp:comment_status>',
            '      <wp:ping_status>open</wp:ping_status>',
            f'      <wp:post_name>{escape_xml(slug)}</wp:post_name>',
            '      <wp:status>draft</wp:status>',   # stays draft until you review & publish
            '      <wp:post_type>post</wp:post_type>',
            '      <wp:is_sticky>0</wp:is_sticky>',
        ]

        # Add featured image as postmeta if present
        # WordPress.com may not honour _thumbnail_id on import but
        # we include it for self-hosted compatibility
        f_img = post.get('featured_image')
        if f_img:
            lines += [
                '      <wp:postmeta>',
                '        <wp:meta_key>_featured_image_filename</wp:meta_key>',
                f'        <wp:meta_value><![CDATA[{f_img}]]></wp:meta_value>',
                '      </wp:postmeta>',
            ]
        lines += ['    </item>']

    lines += ['  </channel>', '</rss>']
    return '\n'.join(lines)

# ── Review report ─────────────────────────────────────────────────────────────

REVIEW_CSS = """
body{font-family:Georgia,serif;max-width:960px;margin:40px auto;padding:0 20px;color:#333;background:#f9f9f9}
h1{border-bottom:3px solid #8b0000;padding-bottom:10px;color:#8b0000}
.summary{background:#fff;border:1px solid #ddd;padding:15px 20px;border-radius:4px;margin-bottom:30px}
.post{background:#fff;border:1px solid #ccc;margin:25px 0;border-radius:6px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.post-header{background:#f0f0f0;padding:12px 18px;border-bottom:1px solid #ccc}
.post-header h2{margin:0 0 4px;font-size:1.05em;color:#222}
.meta{font-size:.82em;color:#555;font-family:Arial,sans-serif}
.meta span{margin-right:12px}
.flags{background:#fff8e1;border-left:4px solid #ffa000;padding:10px 15px;margin:15px 18px 0}
.flags h4{margin:0 0 6px;color:#e65100;font-family:Arial,sans-serif;font-size:.9em}
.flags ul{margin:0;padding-left:18px;font-size:.9em;font-family:Arial,sans-serif}
.content{padding:18px;border-top:1px solid #eee}
.ok::before{content:'✓ ';color:green}
.warn::before{content:'⚠ ';color:#e65100}
.post-nota{font-size:.9em;color:#555;border-left:3px solid #aaa;padding-left:10px;margin:10px 0}
.post-source{font-size:.9em}
.post-caption{font-size:.9em;font-style:italic}
.post-links{font-size:.9em}
.post-author{font-weight:bold}
figure.post-image{margin:15px 0;text-align:center}
figure.post-image img{max-width:100%;border:1px solid #ddd}
figure.post-image figcaption{font-size:.85em;color:#666;margin-top:5px;font-style:italic}
.video-embed{margin:15px 0;text-align:center}
hr.post-separator{border:none;border-top:1px solid #ccc;margin:20px 0}
"""

def build_review_report(posts_chunk, chunk_num, total_chunks, total_all):
    """Generate one HTML review report file for a chunk of posts."""
    total    = len(posts_chunk)
    flagged  = sum(1 for p in posts_chunk if p.get('flags'))
    ok_count = total - flagged
    chunk_label = f"Parte {chunk_num} de {total_chunks}" if total_chunks > 1 else ""

    parts = [f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="UTF-8">
<title>Revisión del blog — {chunk_label}</title>
<style>{REVIEW_CSS}</style>
</head>
<body>
<h1>Revisión del blog de Iván y Tania</h1>
<div class="summary">
  {f'<strong>{chunk_label}</strong> &nbsp;|&nbsp; ' if chunk_label else ''}
  <strong>{total} posts</strong> &nbsp;|&nbsp;
  <span style="color:green">✓ {ok_count} sin alertas</span> &nbsp;|&nbsp;
  <span style="color:#e65100">⚠ {flagged} con alertas</span><br>
  <small style="color:#888">Generado: {datetime.now().strftime('%Y-%m-%d %H:%M')} &nbsp;|&nbsp; Total procesados: {total_all}</small>
</div>
"""]

    for i, post in enumerate(posts_chunk, 1):
        flags        = post.get('flags', [])
        status_class = 'warn' if flags else 'ok'
        title        = post.get('title', '(sin título)')
        author       = post.get('author') or '—'
        date         = post.get('date', '—')
        f_type       = post.get('featured_type', 'none')
        f_img        = post.get('featured_image') or ''
        f_vid        = post.get('featured_video') or ''
        source_file  = post.get('source_file', '')

        # Display: show video URL for video posts, image for image posts
        if f_type == 'video' and f_vid:
            featured_info = f'video: {f_vid}'
            if f_img:
                featured_info += f' | img: {f_img.split("_")[-1]}'
        elif f_img:
            featured_info = f'image: {f_img.split("_")[-1]}'
        else:
            featured_info = f_type

        parts.append(f"""
<div class="post">
  <div class="post-header">
    <h2 class="{status_class}">{escape_xml(title)}</h2>
    <div class="meta">
      <span>📅 {date}</span>
      <span>✍️ {escape_xml(author)}</span>
      <span>🖼 {featured_info}</span>
      <span style="color:#aaa">📄 {escape_xml(source_file)}</span>
    </div>
  </div>
""")

        if flags:
            flag_items = '\n'.join(f'<li>{escape_xml(f)}</li>' for f in flags)
            parts.append(f"""  <div class="flags">
    <h4>Alertas para revisión:</h4>
    <ul>{flag_items}</ul>
  </div>
""")

        # In the review report, replace YouTube iframes with clickable thumbnails
        # (iframes fail with error 153 when opened as local files)
        import re as _re
        review_html = post.get("content_html", "")
        def _yt_thumb(m):
            vid_id = _re.search(r'embed/([\w-]+)', m.group(0))
            if not vid_id:
                return m.group(0)
            vid = vid_id.group(1)
            url = f"https://www.youtube.com/watch?v={vid}"
            thumb = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"
            return (
                f'<div class="video-embed" style="text-align:center;margin:15px 0">'
                f'<a href="{url}" target="_blank">'
                f'<img src="{thumb}" style="max-width:100%;border:2px solid #ccc"'
                f' alt="Ver video en YouTube"><br>'
                f'<small style="color:#1a0dab">&#9654; Ver en YouTube: {url}</small>'
                f'</a></div>'
            )
        # If video is the featured element, remove it from body to avoid duplication
        if f_type == 'video':
            review_html = _re.sub(r'<div[^>]*class="video-embed"[^>]*>.*?</div>',
                                  '', review_html, flags=_re.DOTALL)
        else:
            review_html = _re.sub(r'<div class="video-embed">.*?</div>', _yt_thumb, review_html, flags=_re.DOTALL)

        # Render featured video or image at top of content area
        featured_block = ''
        if f_type == 'video' and f_vid:
            import re as _re2
            vid_id = _re2.search(r'embed/([\w-]+)', f_vid)
            if vid_id:
                vid   = vid_id.group(1)
                yt_url = f'https://www.youtube.com/watch?v={vid}'
                thumb  = f'https://img.youtube.com/vi/{vid}/hqdefault.jpg'
                featured_block = (
                    f'<div style="text-align:center;margin:0 0 18px 0;'
                    f'padding:8px;background:#f5f5f5;border:1px solid #ddd">'
                    f'<p style="margin:0 0 6px;font-size:.8em;color:#888;'
                    f'font-family:Arial,sans-serif">&#128249; Video destacado</p>'
                    f'<a href="{yt_url}" target="_blank">'
                    f'<img src="{thumb}" style="max-width:100%;border:2px solid #ccc" alt="Ver en YouTube"><br>'
                    f'<small style="color:#1a0dab">&#9654; Ver en YouTube</small>'
                    f'</a></div>'
                )
        elif f_img:
            img_src = f'images/{f_img}'
            if img_src not in review_html:
                featured_block = (
                    f'<div style="text-align:center;margin:0 0 18px 0;'
                    f'padding:8px;background:#f5f5f5;border:1px solid #ddd">'
                    f'<p style="margin:0 0 6px;font-size:.8em;color:#888;'
                    f'font-family:Arial,sans-serif">&#128204; Imagen destacada</p>'
                    f'<img src="{img_src}" style="max-width:100%" alt="">'
                    f'</div>'
                )

        parts.append(f'  <div class="content">\n{featured_block}{review_html}\n  </div>\n</div>\n')

    parts.append('</body></html>')
    return ''.join(parts)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    input_dir  = Path(sys.argv[1])
    output_dir = Path(sys.argv[2])
    output_dir.mkdir(parents=True, exist_ok=True)
    images_dir = output_dir / "images"
    images_dir.mkdir(exist_ok=True)

    # Parse options
    args = sys.argv[3:]
    no_upload  = "--no-upload" in args
    as_draft   = "--draft" in args
    test_files = None
    for arg in args:
        if arg.startswith("--test="):
            test_files = [f.strip() for f in arg[7:].split(";")]
        elif arg.startswith("--test"):
            idx = args.index(arg)
            if idx + 1 < len(args):
                test_files = [f.strip() for f in args[idx+1].split(";")]

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY environment variable not set.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    if test_files:
        # Test mode: only process specified files
        eml_files = []
        for fname in test_files:
            p = Path(fname) if Path(fname).is_absolute() else input_dir / fname
            matches = list(input_dir.glob(f"*{fname}*")) if not p.exists() else [p]
            eml_files.extend(matches)
        eml_files = sorted(set(eml_files))
        print(f"TEST MODE: processing {len(eml_files)} file(s)\n")
    else:
        eml_files = sorted(input_dir.glob("*.eml"))

    if not eml_files:
        print(f"No .eml files found in {input_dir}")
        sys.exit(1)

    if no_upload:
        print("--no-upload: WordPress upload will be skipped\n")

    print(f"Found {len(eml_files)} .eml files — starting pipeline\n")

    all_posts = []
    errors    = []

    for idx, eml_path in enumerate(eml_files, 1):
        print(f"[{idx}/{len(eml_files)}] {eml_path.name}")

        try:
            with open(eml_path, 'rb') as f:
                msg = email.message_from_binary_file(f)

            subject_raw = decode_str(msg.get('Subject', ''))
            sender      = get_sender_email(msg)
            sent_year   = get_sent_year(msg)
            parsed      = parse_subject_new_format(subject_raw) or parse_subject(subject_raw)

            if not parsed:
                print(f"         ⚠ Could not parse subject — skipping")
                errors.append(eml_path.name)
                continue



            year     = parsed.get('year_override', sent_year)
            pub_date = f"{year}-{parsed['month']:02d}-{parsed['day']:02d}"
            # Include a short hash of the source filename so two posts
            # on the same date (formerly one per blog) never share a prefix
            _src_hash = hashlib.md5(eml_path.name.encode()).hexdigest()[:6]
            prefix   = f"{pub_date}_{parsed['post_num']:02d}_{_src_hash}"

            attachments = extract_attachments(msg, images_dir, prefix)
            html_body   = get_html_body(msg)
            text_body   = get_text_body(msg)
            # Reorder CID images to match visual order in HTML body
            attachments = reorder_attachments_by_visual(attachments, html_body, msg)

            print(f"         → {pub_date} | {len(attachments)} attachments")
            print(f"         → Calling Claude API...")

            blog_origin = parsed.get('blog_origin', BLOG_ORIGIN_DEFAULT)
            result = process_with_claude(
                client, subject_raw, sender, pub_date,
                html_body, text_body, attachments, blog_origin
            )

            result['date']        = pub_date
            result['post_num']    = parsed['post_num']
            result['source_file'] = eml_path.name
            result['attachments'] = attachments

            flags = result.get('flags', [])
            if flags:
                print(f"         ⚠ {len(flags)} flag(s):")
                for f in flags:
                    print(f"           · {f}")
            else:
                print(f"         ✓ {result.get('title', '?')}")

            all_posts.append(result)

            time.sleep(0.5)     # be polite to the API

        except json.JSONDecodeError as e:
            print(f"         ✗ Claude returned invalid JSON: {e}")
            errors.append(eml_path.name)
        except Exception as e:
            print(f"         ✗ Error: {e}")
            errors.append(eml_path.name)

    # Sort by date
    all_posts.sort(key=lambda p: p['date'])

    print(f"\n{'='*60}")
    print(f"Procesados: {len(all_posts)} posts")
    if errors:
        print(f"  Con errores: {len(errors)}")

    if not all_posts:
        return

    # Always generate review reports
    chunks       = [all_posts[i:i+POSTS_PER_REPORT]
                    for i in range(0, len(all_posts), POSTS_PER_REPORT)]
    total_chunks = len(chunks)
    print()

    for chunk_num, chunk in enumerate(chunks, 1):
        d_from = chunk[0]['date']
        d_to   = chunk[-1]['date']
        review_fname = "revision.html" if total_chunks == 1 else                        f"revision_{chunk_num:03d}_{d_from}_al_{d_to}.html"
        rev_path = output_dir / review_fname
        rev_path.write_text(
            build_review_report(chunk, chunk_num, total_chunks, len(all_posts)),
            encoding='utf-8'
        )
        print(f"→ Revisión {chunk_num}/{total_chunks}: {rev_path}")

    # Upload to WordPress or generate WXR
    if WP_UPLOAD and WP_CLIENT_ID != "YOUR_CLIENT_ID" and not no_upload:
        print("\n── WordPress Upload ─────────────────────────────────────")
        try:
            wp_token = wp_get_token(
                WP_CLIENT_ID, WP_CLIENT_SECRET,
                WP_REDIRECT_URI, WP_TOKEN_FILE
            )

            # Collect all attachments across all posts
            post_attachments = [p.get("attachments", []) for p in all_posts]

            # Upload all images first
            media_map = wp_upload_images(
                WP_SITE, wp_token, images_dir, post_attachments
            )
            print(f"✓ {len(media_map)} images uploaded\n")

            # Create posts
            print("Creating posts in WordPress...")
            created = 0
            failed  = 0
            for i, post in enumerate(all_posts, 1):
                title = post.get('title', '?')
                try:
                    result = wp_create_post(WP_SITE, wp_token, post, media_map, as_draft)
                    post_id = result.get('ID', '?')
                    print(f"  ✓ [{i}/{len(all_posts)}] {title[:50]} (ID: {post_id})")
                    created += 1
                    time.sleep(2)
                except urllib.error.HTTPError as e:
                    err_body = e.read().decode(errors="replace")[:200]
                    print(f"  ✗ [{i}/{len(all_posts)}] {title[:50]}: HTTP {e.code} — {err_body}")
                    failed += 1
                except Exception as e:
                    print(f"  ✗ [{i}/{len(all_posts)}] {title[:50]}: {e}")
                    failed += 1

            print(f"\n✓ {created} posts created as drafts in WordPress")
            if failed:
                print(f"✗ {failed} posts failed — check errors above")

        except Exception as e:
            print(f"\n✗ WordPress upload failed: {e}")
            print("Falling back to WXR file generation...")
            WP_UPLOAD_FALLBACK = True
        else:
            WP_UPLOAD_FALLBACK = False
    else:
        WP_UPLOAD_FALLBACK = True
        if WP_UPLOAD and WP_CLIENT_ID == "YOUR_CLIENT_ID":
            print("\n⚠ WP_CLIENT_ID not configured — generating WXR files instead")

    # Generate WXR if not uploading directly
    if no_upload or not WP_UPLOAD or ('WP_UPLOAD_FALLBACK' in dir() and WP_UPLOAD_FALLBACK):
        for chunk_num, chunk in enumerate(chunks, 1):
            d_from = chunk[0]['date']
            d_to   = chunk[-1]['date']
            wxr_fname = "blog_ivan_tania.xml" if total_chunks == 1 else                         f"blog_ivan_tania_{chunk_num:03d}_{d_from}_al_{d_to}.xml"
            wxr_path = output_dir / wxr_fname
            wxr_path.write_text(
                build_wxr(chunk, BLOG_NAME, BLOG_URL), encoding='utf-8'
            )
            print(f"-> WXR {chunk_num}/{total_chunks}: {wxr_path}")

    print(f"\nImágenes guardadas en: {images_dir}")
    print("\nTodos los posts están como Borrador — revisa y publica cuando estés listo.")

if __name__ == "__main__":
    main()
