#!/usr/bin/env python3
"""
Personal IPTV list builder for TiviMate (Python 3.8+, no dependencies).

  python3 iptv.py import <playlist.m3u or URL> [--group-filter TEXT]
        Add channels from an existing M3U into channels.csv (so you can curate them).
  python3 iptv.py check
        Test every stream URL in channels.csv and report which are dead.
  python3 iptv.py build [--base-url URL]
        Write output/playlist.m3u and output/epg.xml.gz (EPG filtered to your channels).
  python3 iptv.py serve
        Build, then serve output/ on your LAN so TiviMate can load it.
"""
import argparse, csv, gzip, ssl, http.server, io, json, os, re, socket, socketserver, sys
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from xml.sax.saxutils import escape, quoteattr

HERE = os.path.dirname(os.path.abspath(__file__))
CHANNELS = os.path.join(HERE, "channels.csv")
CONFIG = os.path.join(HERE, "config.json")
OUT = os.path.join(HERE, "output")
FIELDS = ["name", "group", "logo", "epg_id", "url", "user_agent", "referrer"]
UA = "Mozilla/5.0 (Linux; Android 12) TiviMate/5"


def _ssl_context():
    # python.org builds on macOS ship without root certs; fall back to certifi or the system bundle.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    ctx = ssl.create_default_context()
    if os.path.exists("/etc/ssl/cert.pem"):
        ctx.load_verify_locations("/etc/ssl/cert.pem")
    return ctx


SSL = _ssl_context()


# ---------- helpers ----------
def load_config():
    cfg = {"epg_sources": [], "port": 8000}
    if os.path.exists(CONFIG):
        with open(CONFIG, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def fetch(src, timeout=90):
    if re.match(r"https?://", src):
        req = urllib.request.Request(src, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout, context=SSL) as r:
            data = r.read()
    else:
        with open(os.path.expanduser(src), "rb") as f:
            data = f.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def norm(name):
    """Loose name key for auto-matching channels to EPG display-names."""
    name = name.lower()
    name = re.sub(r"\((.*?)\)|\[(.*?)\]", " ", name)          # drop (720p) [Geo-blocked] etc.
    name = re.sub(r"\b(hd|fhd|uhd|sd|4k|hevc|h265)\b", " ", name)
    return re.sub(r"[^a-z0-9]+", "", name)


def load_channels():
    if not os.path.exists(CHANNELS):
        sys.exit("channels.csv not found")
    with open(CHANNELS, newline="", encoding="utf-8") as f:
        rows = []
        for r in csv.DictReader(f):
            r = {k: (r.get(k) or "").strip() for k in FIELDS}
            if r["url"] and not r["name"].startswith("#"):
                rows.append(r)
    return rows


def save_channels(rows):
    with open(CHANNELS, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


# ---------- import ----------
def parse_m3u(text):
    out, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', line))
            title = line.rsplit(",", 1)[-1].strip() if "," in line else ""
            cur = {"name": title or attrs.get("tvg-name", ""), "group": attrs.get("group-title", ""),
                   "logo": attrs.get("tvg-logo", ""), "epg_id": attrs.get("tvg-id", ""),
                   "user_agent": attrs.get("http-user-agent", ""), "referrer": attrs.get("http-referrer", "")}
        elif line.startswith("#EXTVLCOPT") and cur is not None:
            k, _, v = line.split(":", 1)[1].partition("=")
            if k == "http-user-agent":
                cur["user_agent"] = v
            elif k in ("http-referrer", "http-referer"):
                cur["referrer"] = v
        elif line and not line.startswith("#") and cur is not None:
            cur["url"] = line
            out.append(cur)
            cur = None
    return out


def cmd_import(a):
    new = parse_m3u(fetch(a.source).decode("utf-8", "replace"))
    if a.group_filter:
        new = [c for c in new if a.group_filter.lower() in c["group"].lower()]
    if a.name_filter:
        new = [c for c in new if a.name_filter.lower() in c["name"].lower()]
    existing = load_channels() if os.path.exists(CHANNELS) else []
    seen = {c["url"] for c in existing}
    added = [c for c in new if c["url"] not in seen]
    save_channels(existing + added)
    print(f"Imported {len(added)} channels ({len(new) - len(added)} duplicates skipped). "
          f"channels.csv now has {len(existing) + len(added)}. Edit it to keep only what you want.")


# ---------- check ----------
def probe(c):
    headers = {"User-Agent": c["user_agent"] or UA}
    if c["referrer"]:
        headers["Referer"] = c["referrer"]
    try:
        req = urllib.request.Request(c["url"], headers=headers)
        with urllib.request.urlopen(req, timeout=10, context=SSL) as r:
            r.read(2048)
            return c, r.status < 400, str(r.status)
    except Exception as e:  # noqa: BLE001
        return c, False, type(e).__name__ + ": " + str(e)[:60]


def cmd_check(a):
    rows = load_channels()
    dead = []
    with ThreadPoolExecutor(16) as ex:
        for c, ok, info in ex.map(probe, rows):
            print(("  OK   " if ok else "  DEAD ") + c["name"] + ("" if ok else f"  ({info})"))
            if not ok:
                dead.append(c)
    print(f"\n{len(rows) - len(dead)}/{len(rows)} streams responded.")
    if a.prune and dead:
        dead_urls = {c["url"] for c in dead}
        save_channels([c for c in rows if c["url"] not in dead_urls])
        print(f"Removed {len(dead)} dead channels from channels.csv.")


# ---------- build ----------
def build_epg(rows, sources):
    """Stream each XMLTV source and keep only the channels/programmes we need.
    Each channel is matched by (1) exact epg_id, (2) channel name vs display-name,
    or (3) the EPG id appearing inside the stream URL (works for Pluto/Samsung/etc).
    Returns {row index: matched epg id}."""
    by_id, by_name = {}, {}
    for idx, c in enumerate(rows):
        if c["epg_id"]:
            by_id.setdefault(c["epg_id"], []).append(idx)
        by_name.setdefault(norm(c["name"]), []).append(idx)
    resolved = {}          # row idx -> epg id
    claimed = {}           # epg id -> source index (first source wins)
    n_prog = 0
    os.makedirs(OUT, exist_ok=True)
    with gzip.open(os.path.join(OUT, "epg.xml.gz"), "wt", encoding="utf-8") as out:
        out.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="iptv.py">\n')
        for i, src in enumerate(sources):
            try:
                print(f"  EPG source {i + 1}: {src}")
                data = fetch(src)
            except Exception as e:  # noqa: BLE001
                print(f"    ! skipped ({e})")
                continue
            for _, el in ET.iterparse(io.BytesIO(data), events=("end",)):
                if el.tag == "channel":
                    cid = el.get("id", "")
                    if cid and cid not in claimed:
                        hits = list(by_id.get(cid, []))
                        for d in el.findall("display-name"):
                            hits += by_name.get(norm(d.text or ""), [])
                        if len(cid) >= 12:
                            hits += [k for k, c in enumerate(rows) if k not in resolved and cid in c["url"]]
                        hits = [k for k in hits if k not in resolved]
                        if hits:
                            claimed[cid] = i
                            for k in hits:
                                resolved[k] = cid
                            out.write(ET.tostring(el, encoding="unicode"))
                    el.clear()
                elif el.tag == "programme":
                    if claimed.get(el.get("channel")) == i:
                        out.write(ET.tostring(el, encoding="unicode"))
                        n_prog += 1
                    el.clear()
            del data
        out.write("</tv>\n")
    missing = [c["name"] for k, c in enumerate(rows) if k not in resolved]
    print(f"  EPG: guide found for {len(resolved)}/{len(rows)} channels ({n_prog} programmes).")
    if missing:
        print(f"  No guide data for: " + ", ".join(missing[:15]) + (f" ... (+{len(missing) - 15})" if len(missing) > 15 else ""))
    return resolved


def cmd_build(a, cfg=None):
    cfg = cfg or load_config()
    rows = load_channels()
    base = (a.base_url or cfg.get("base_url") or f"http://{lan_ip()}:{cfg['port']}").rstrip("/")
    print(f"Building {len(rows)} channels...")
    resolved = build_epg(rows, cfg["epg_sources"]) if cfg["epg_sources"] else {}
    epg_url = f"{base}/epg.xml.gz"
    lines = [f'#EXTM3U url-tvg="{epg_url}" x-tvg-url="{epg_url}"']
    for k, c in enumerate(rows):
        tid = resolved.get(k, c["epg_id"])
        attr = lambda s: s.replace('"', "'")
        extinf = (f'#EXTINF:-1 tvg-id="{attr(tid)}" tvg-name="{attr(c["name"])}" '
                  f'tvg-logo="{attr(c["logo"])}" group-title="{attr(c["group"] or "Other")}",{c["name"]}')
        lines.append(extinf)
        if c["user_agent"]:
            lines.append(f"#EXTVLCOPT:http-user-agent={c['user_agent']}")
        if c["referrer"]:
            lines.append(f"#EXTVLCOPT:http-referrer={c['referrer']}")
        lines.append(c["url"])
    with open(os.path.join(OUT, "playlist.m3u"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nDone.\n  Playlist: {base}/playlist.m3u\n  EPG:      {epg_url}")


def cmd_serve(a):
    cfg = load_config()
    cmd_build(a, cfg)
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=OUT)
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", cfg["port"]), handler) as srv:
        print(f"\nServing on your network — enter the playlist URL above in TiviMate. Ctrl+C to stop.")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("import"); s.add_argument("source")
    s.add_argument("--group-filter"); s.add_argument("--name-filter"); s.set_defaults(fn=cmd_import)
    s = sub.add_parser("check"); s.add_argument("--prune", action="store_true", help="remove dead channels")
    s.set_defaults(fn=cmd_check)
    for name, fn in (("build", cmd_build), ("serve", cmd_serve)):
        s = sub.add_parser(name); s.add_argument("--base-url", help="public URL where output/ is hosted")
        s.set_defaults(fn=fn)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
