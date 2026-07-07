"""
mirror.py - verbatim mirror of an external podcast RSS feed

Strategy: the source feed's XML is kept byte-for-byte intact -- it is never
parsed and re-serialized, so every tag survives exactly as published
(podcast:guid, podcast:value, podcast:podroll, psc:chapters, tags that don't
exist yet...). The ONLY changes made are targeted string replacements:

  1. Asset URLs (enclosures, transcripts, chapters JSON, artwork, chapter
     images, the XSL stylesheet) are downloaded into ./mirror/media/ and the
     URLs in the XML are rewritten to point at this server.
  2. The atom:link rel="self" href is rewritten to the mirror feed's own URL.

Everything else -- <link> elements, funding URLs, GUIDs, WebSub hub, value
splits -- passes through untouched. The result is a drop-in failover copy:
if the primary host disappears, this feed still plays because every asset
it references is stored locally.

Run standalone (cron-friendly, like generate.py):

    python3 mirror.py

Configuration lives in podcast.json under the "mirror" key and is managed
from cli.py ("Mirror external feed").
"""
import hashlib
import json
import os
import re
import sys
from urllib.parse import urlparse

import requests

import store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MIRROR_DIR = os.path.join(BASE_DIR, 'mirror')
MIRROR_MEDIA_DIR = os.path.join(MIRROR_DIR, 'media')
MIRROR_FEED_FILE = os.path.join(MIRROR_DIR, 'feed.xml')

# rss.com (and some CDNs) return 403 to obvious bot user agents
USER_AGENT = ('Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 '
              'Firefox/128.0 podcast-host-mirror/1.0')

EXT_FROM_TYPE = {
    'audio/mpeg': '.mp3',
    'audio/mp3': '.mp3',
    'audio/x-m4a': '.m4a',
    'audio/mp4': '.m4a',
    'audio/ogg': '.ogg',
    'audio/opus': '.opus',
    'audio/wav': '.wav',
    'text/vtt': '.vtt',
    'application/x-subrip': '.srt',
    'application/srt': '.srt',
    'text/plain': '.txt',
    'text/html': '.html',
    'application/json': '.json',
    'application/json+chapters': '.json',
    'image/jpeg': '.jpg',
    'image/png': '.png',
    'image/gif': '.gif',
    'image/webp': '.webp',
    'text/xsl': '.xsl',
}

# Attribute-bearing tags whose URL attribute points at an asset we localize.
# tag name -> (url attribute, kind)
ASSET_TAGS = {
    'enclosure': ('url', 'audio'),
    'podcast:transcript': ('url', 'transcript'),
    'podcast:chapters': ('url', 'chapters'),
    'itunes:image': ('href', 'image'),
    'psc:chapter': ('image', 'image'),
}

TAG_RE = re.compile(
    r'<(enclosure|podcast:transcript|podcast:chapters|itunes:image|psc:chapter)\b[^>]*>')
ATTR_RE = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')
IMAGE_URL_RE = re.compile(r'<url>\s*([^<\s][^<]*?)\s*</url>')
STYLESHEET_RE = re.compile(r'<\?xml-stylesheet\b[^?]*?href="([^"]+)"')
SELF_LINK_RE = re.compile(r'<atom:link\b[^>]*rel="self"[^>]*/?>')
HREF_RE = re.compile(r'href="[^"]*"')


def xml_unescape(s):
    return (s.replace('&lt;', '<').replace('&gt;', '>')
             .replace('&quot;', '"').replace('&apos;', "'")
             .replace('&amp;', '&'))


def local_asset_name(url, mime=''):
    """Deterministic local filename for a remote asset URL.

    A short hash prefix keeps names unique across identical basenames, and
    determinism means re-runs skip files that were already downloaded.
    URLs with no file extension (e.g. rss.com transcript endpoints) get one
    from the declared MIME type.
    """
    path = urlparse(url).path
    base = os.path.basename(path) or 'asset'
    base = re.sub(r'[^A-Za-z0-9._-]', '_', base)
    if not os.path.splitext(base)[1]:
        base += EXT_FROM_TYPE.get((mime or '').split(';')[0].strip().lower(), '')
    return f'{hashlib.sha1(url.encode()).hexdigest()[:12]}-{base}'


def download_asset(url, dest_path, log):
    """Download url to dest_path unless it already exists. True on success."""
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        return True
    tmp_path = dest_path + '.part'
    try:
        res = requests.get(url, stream=True, timeout=60,
                           headers={'User-Agent': USER_AGENT})
        res.raise_for_status()
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(tmp_path, 'wb') as f:
            for chunk in res.iter_content(chunk_size=65536):
                f.write(chunk)
        os.replace(tmp_path, dest_path)
        log(f'    downloaded {os.path.basename(dest_path)}')
        return True
    except Exception as e:
        log(f'    FAILED {url}: {e}')
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False


def collect_assets(xml_text):
    """Find every asset URL in the feed XML.

    Returns a list of (raw_attr_value, unescaped_url, mime, kind) tuples.
    raw_attr_value is the string exactly as it appears in the XML (may
    contain &amp;) and is what gets replaced; unescaped_url is what gets
    downloaded.
    """
    assets = []
    seen = set()

    def add(raw, mime, kind):
        if not raw or raw in seen or not raw.startswith(('http://', 'https://')):
            return
        seen.add(raw)
        assets.append((raw, xml_unescape(raw), mime, kind))

    for m in TAG_RE.finditer(xml_text):
        tag = m.group(1)
        attrs = dict(ATTR_RE.findall(m.group(0)))
        url_attr, kind = ASSET_TAGS[tag]
        add(attrs.get(url_attr, ''), attrs.get('type', ''), kind)

    for m in IMAGE_URL_RE.finditer(xml_text):  # channel <image><url>
        add(m.group(1), 'image/png', 'image')

    for m in STYLESHEET_RE.finditer(xml_text):
        add(m.group(1), 'text/xsl', 'image')

    return assets


def localize_chapters_json(path, media_base_url, log):
    """Rewrite img URLs inside a downloaded chapters JSON file to local copies."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        log(f'    could not parse chapters JSON {os.path.basename(path)}: {e}')
        return
    changed = False
    for ch in data.get('chapters', []):
        img = ch.get('img')
        if not img or not img.startswith(('http://', 'https://')):
            continue
        if img.startswith(media_base_url):  # already localized on a prior sync
            continue
        name = local_asset_name(img, 'image/png')
        if download_asset(img, os.path.join(MIRROR_MEDIA_DIR, name), log):
            ch['img'] = f'{media_base_url}/{name}'
            changed = True
    if changed:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)


def sync_mirror(log=print):
    """Fetch the configured source feed, localize its assets, and write the
    mirror feed. Returns a stats dict."""
    data = store.load()
    cfg = data.get('mirror') or {}
    source_url = cfg.get('sourceUrl')
    if not source_url:
        raise RuntimeError('No mirror configured. Set one up in cli.py first.')

    base_url = data['show'].get('baseUrl', '').rstrip('/')
    if not base_url:
        raise RuntimeError('Show baseUrl is not set; run the setup wizard first.')
    feed_url = f'{base_url}/mirror/feed.xml'
    media_base_url = f'{base_url}/mirror/media'

    log(f'  Fetching {source_url}')
    res = requests.get(source_url, timeout=60, headers={'User-Agent': USER_AGENT})
    res.raise_for_status()
    res.encoding = res.encoding or 'utf-8'
    xml_text = res.text

    assets = collect_assets(xml_text)
    log(f'  Found {len(assets)} unique assets to localize')

    os.makedirs(MIRROR_MEDIA_DIR, exist_ok=True)
    ok = failed = 0
    for raw, url, mime, kind in assets:
        name = local_asset_name(url, mime)
        dest = os.path.join(MIRROR_MEDIA_DIR, name)
        if download_asset(url, dest, log):
            if kind == 'chapters':
                localize_chapters_json(dest, media_base_url, log)
            # Replace the URL exactly as written in the XML. On failure the
            # original remote URL is kept -- a working remote link beats a
            # broken local one.
            xml_text = xml_text.replace(f'"{raw}"', f'"{media_base_url}/{name}"')
            xml_text = xml_text.replace(f'>{raw}<', f'>{media_base_url}/{name}<')
            ok += 1
        else:
            failed += 1

    # Point the self link at the mirror itself so validators don't flag it
    m = SELF_LINK_RE.search(xml_text)
    if m:
        new_tag = HREF_RE.sub(f'href="{feed_url}"', m.group(0), count=1)
        xml_text = xml_text.replace(m.group(0), new_tag, 1)

    tmp = MIRROR_FEED_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(xml_text)
    os.replace(tmp, MIRROR_FEED_FILE)

    log(f'  Mirror written to {MIRROR_FEED_FILE}')
    log(f'  Assets: {ok} localized, {failed} failed (left pointing at source)')
    return {'assets_ok': ok, 'assets_failed': failed, 'feed_url': feed_url}


if __name__ == '__main__':
    try:
        sync_mirror()
    except Exception as e:
        print(f'mirror sync failed: {e}', file=sys.stderr)
        sys.exit(1)
