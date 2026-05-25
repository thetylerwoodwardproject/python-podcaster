#!/usr/bin/env python3
"""
generate.py -- regenerates feed.xml from the database.
Run this via cron to publish scheduled episodes automatically.

Example crontab entry (runs daily at 8am):
  0 8 * * * cd /opt/podcast-host && python3 generate.py >> /var/log/podcast.log 2>&1
"""
import os
import sys
from datetime import datetime, timezone

# Make sure we can import from the same directory
sys.path.insert(0, os.path.dirname(__file__))

import feed
import store

if __name__ == '__main__':
    live, scheduled = feed.write_feed()
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    print(f'[{now}] feed.xml regenerated -- {live} live, {scheduled} scheduled')
