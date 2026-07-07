# Termicast

Self-hosted Podcast 2.0 RSS manager. No server process needed -- generates a
static feed.xml that nginx serves directly.

## How it works

- `cli.py` -- run this to manage episodes. Writes feed.xml to disk after every change.
- `generate.py` -- tiny script cron runs daily to publish scheduled episodes automatically.
- nginx serves feed.xml and the media folder as static files. Nothing is ever running.

## Requirements

- Python 3.8+
- nginx
- certbot
- `pip3 install requests` (only needed for the RSS import feature)

## Setup

### 1. Get the files onto your server

Clone from GitHub:

```bash
git clone https://github.com/youruser/termicast /opt/termicast
```

Or copy manually:

```bash
scp -r termicast/ user@yourserver:/opt/termicast
```

### 2. Install dependencies

```bash
pip3 install requests
apt install nginx certbot python3-certbot-nginx
```

### 3. Create the media directory

```bash
mkdir -p /opt/termicast/media
```

### 4. First run

The first time you run the script it walks you through a setup wizard asking
for your domain, show title, description, and author name.

```bash
cd /opt/termicast
python3 cli.py
```

### 5. Symlink feed.xml and media into /var/www/html

nginx serves files from `/var/www/html` by default. Symlinking keeps your
files in one place while letting nginx find them without any path gymnastics.

```bash
ln -s /opt/termicast/feed.xml /var/www/html/feed.xml
ln -s /opt/termicast/media /var/www/html/media
```

### 6. Configure nginx

```bash
cp /opt/termicast/nginx.conf.example /etc/nginx/sites-available/audio.example.com
```

Edit the file and replace `audio.example.com` with your actual domain. Then enable it:

```bash
ln -s /etc/nginx/sites-available/audio.example.com /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
```

> **Important:** Remove the default nginx site (`default`) or it will intercept
> requests before your config and you'll get 404s on the ACME challenge.

Test that it's working before running certbot:

```bash
curl http://audio.example.com/feed.xml
```

### 7. Get your SSL certificate

```bash
certbot --nginx -d audio.example.com
```

Certbot will automatically update your nginx config with HTTPS settings and
schedule automatic certificate renewal. Don't run this until the curl test
in the previous step returns your feed.

### 8. Set up the cron job

This is what publishes scheduled episodes automatically. Without it, scheduled
episodes only go live the next time you manually run `cli.py`.

```bash
crontab -e
```

Add this line (runs daily at 8am -- adjust to match when your episodes go live):

```
0 8 * * * cd /opt/termicast && python3 generate.py >> /var/log/termicast.log 2>&1
```

Your feed is now live at `https://audio.example.com/feed.xml`.

---

## Usage

```bash
cd /opt/termicast
python3 cli.py
```

feed.xml is regenerated automatically after every action in the CLI.

### Adding an episode

1. Drop your MP3 (and optional transcript) into `./media/`
2. Run `python3 cli.py` and select "Add episode"
3. Fill in the prompts
4. Set a future pub date to schedule, or a past/current date to publish immediately

### Scheduling an episode

1. Add the episode with a future pub date (e.g. `2026-06-02 08:00`)
2. The episode is saved to the database but filtered out of feed.xml
3. At 8am on June 2nd, cron runs `generate.py` and the episode appears in the feed

### Importing from an existing feed

Select "Import from RSS feed" and paste your current feed URL. The script will:

- Import all show metadata
- Download all audio files into `./media/`
- Download episode artwork
- Download transcripts if available in the feed

Existing files are skipped on re-import so it is safe to run more than once.

### Manually regenerating the feed

```bash
python3 generate.py
```

---

## Chapters

Chapters follow the [Podcast Index JSON format](https://github.com/Podcast-Index-org/podcast-namespace/blob/master/chapters/jsonChapters.md).
Each chapter supports a start time, title, optional link URL, and optional image URL.
Chapter JSON files are written to `./media/` automatically when the feed regenerates.

## Mirroring an external feed

"Mirror external feed" in the CLI keeps a verbatim, self-contained copy of
another host's feed on this server -- a failover if anything happens to your
primary host.

Unlike "Import from RSS feed" (a one-way *migration* that flattens the feed
into this tool's data model), the mirror never re-generates the XML. The
source feed is kept byte-for-byte, so every tag survives exactly as
published: `podcast:guid`, `podcast:value` splits, `podcast:podroll`,
`podcast:funding`, `psc:chapters`, multiple categories, and any future
Podcasting 2.0 tags. The only changes are:

- Asset URLs (enclosures, transcripts, chapters JSON and its images,
  artwork, chapter images, the XSL stylesheet) are rewritten to local copies
  downloaded into `./mirror/media/`.
- The `atom:link rel="self"` is pointed at the mirror's own URL.

Failed downloads keep their original source URL (a working remote link beats
a broken local one) and are retried on the next sync. Syncs are incremental:
already-downloaded assets are skipped.

Set it up in the CLI, then serve and schedule it:

```bash
ln -s /opt/termicast/mirror /var/www/html/mirror
crontab -e   # add:
0 9 * * * cd /opt/termicast && python3 mirror.py >> /var/log/termicast-mirror.log 2>&1
```

The mirror is then live at `https://audio.example.com/mirror/feed.xml`,
with all its media under `https://audio.example.com/mirror/media/`.

## Episode links ("From This Episode")

Each episode can carry a webpage link, emitted as the item's `<link>` element.
Apple Podcasts reads links from your episode data (the `<link>` element and any
`<a href>` in the show notes), fetches OpenGraph metadata from the destination,
and surfaces them as "From This Episode" cards. It is not a `podcast:` namespace
tag -- it is the standard RSS `<link>` plus whatever links live in your notes.

- **Add episode** prompts for an optional "Episode webpage link".
- **Edit episode -> Basic info** shows the current link; press Enter to keep it,
  type a new URL to replace it, or type `-` to clear it.

Note: importing from another feed carries over that feed's `<link>` (and the
links baked into its show notes). If you migrated from a host like rss.com and
still see its page under "From This Episode," clear or replace the episode link
here and strip any leftover links from the show notes.

## Transcripts

Place SRT, VTT, or TXT files in `./media/` and enter the filename when adding
or editing an episode. The correct MIME type is set automatically based on the
file extension.

## OP3 Tracking

Enclosure URLs are automatically prefixed with `https://op3.dev/e/` when you
enable it during the setup wizard. Stats appear at
`https://op3.dev/show/{your-podcast-guid}` once downloads start coming in.

You can disable OP3 by clearing the `op3Prefix` field in "Edit show settings".

---

## File structure

```
termicast/
  cli.py              # Management CLI -- run this to manage episodes
  generate.py         # Cron script -- regenerates feed.xml
  feed.py             # RSS XML generator
  mirror.py           # Cron script -- verbatim mirror of an external feed
  store.py            # JSON data layer
  mirror/             # Mirrored feed.xml + downloaded assets (auto-created)
  podcast.json        # Episode database (auto-created on first run)
  feed.xml            # Generated RSS feed (symlinked into /var/www/html)
  media/              # Audio files, transcripts, chapter JSON
  nginx.conf.example  # Reference nginx config
  README.md
```
