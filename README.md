# My IPTV list

Personal M3U playlist + filtered XMLTV guide for TiviMate, rebuilt automatically by GitHub Actions every 6 hours and published with GitHub Pages.

- Playlist: `https://<user>.github.io/<repo>/playlist.m3u`
- Guide:    `https://<user>.github.io/<repo>/epg.xml.gz`

Edit `channels.csv` (on github.com or locally) and push — the site rebuilds in about a minute.
Guide sources live in `config.json`. Local commands: `python3 iptv.py import|check|build|serve` (see top of `iptv.py`).
