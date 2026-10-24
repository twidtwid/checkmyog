#!/usr/bin/env python3
"""Check My OG: fetch a page as a link-preview crawler would, check its Open
Graph tags and its og:video and og:image files, and serve a page that previews
the cards.

GET /                    the checker page (public/index.html)
GET /api/inspect?url=…   JSON (or Markdown with &format=md): tags, checks, verdicts
GET /api/stats           {"checks": N}, successful checks so far
GET /healthz             liveness for the host

Standard library only, plus the ffprobe binary (from ffmpeg) for video facts.

Fetching arbitrary URLs from a public page is an SSRF risk, so every hop
(including redirects) resolves the host, refuses private/loopback/link-local/
reserved addresses, and connects to the checked IP itself (no second DNS
lookup to rebind). http/https only, ports 80/443, size caps, a per-client
rate limit.

Environment: PORT (default 8080), POSTHOG_KEY and POSTHOG_HOST (optional
analytics; unset means none), PUBLIC_URL (the base URL written into the page,
llms.txt and openapi.json; default https://checkmyog.com), CLIENT_IP_HEADER
(the proxy header that carries the client IP; default Fly-Client-IP),
COUNTER_PATH (file that keeps the checks total across restarts; unset = memory).

SIGTERM shuts down cleanly (exit 0).
"""
import hashlib
import http.client
import io
import ipaddress
import json
import os
import re
import signal
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urljoin, urlsplit

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")
PORT = int(os.environ.get("PORT", "8080"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://checkmyog.com").rstrip("/")
# Only trust a client-IP header your proxy sets (Fly sets Fly-Client-IP); empty = socket address.
CLIENT_IP_HEADER = os.environ.get("CLIENT_IP_HEADER", "Fly-Client-IP")
POSTHOG_KEY = os.environ.get("POSTHOG_KEY", "")
POSTHOG_HOST = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com").rstrip("/")
# Where the "URLs checked" total is kept across restarts; empty = in memory only.
COUNTER_PATH = os.environ.get("COUNTER_PATH", "")
UA = {"x": "Twitterbot/1.0",
      "facebook": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"}
HTML_CAP = 2 * 1024 * 1024
VIDEO_CAP = 40 * 1024 * 1024
TIMEOUT = 12
ALLOWED_PORTS = (80, 443)
RATE = (20, 60)            # requests per window seconds, per client IP
_hits: dict[str, list[float]] = {}
_lock = threading.Lock()
_checks = 0


class Refused(Exception):
    pass


def _safe_ip(host: str, port: int) -> str:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise Refused(f"cannot resolve {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise Refused(f"{host} resolves to a non-public address")
    return infos[0][4][0]


def fetch(url: str, ua: str, method: str = "GET", rng: str | None = None,
          cap: int = HTML_CAP, hops: int = 5):
    """One request with redirects followed by hand. Returns
    (final_url, status, headers{lower: value}, body bytes, truncated)."""
    for _ in range(hops + 1):
        u = urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise Refused("only http(s) URLs")
        port = u.port or (443 if u.scheme == "https" else 80)
        if port not in ALLOWED_PORTS:
            raise Refused("only ports 80 and 443")
        ip = _safe_ip(u.hostname, port)
        sock = socket.create_connection((ip, port), timeout=TIMEOUT)
        if u.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
        conn = http.client.HTTPConnection(u.hostname, port, timeout=TIMEOUT)
        conn.sock = sock
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        headers = {"User-Agent": ua, "Accept": "*/*", "Accept-Encoding": "identity"}
        if rng:
            headers["Range"] = rng
        conn.request(method, path, headers=headers)
        r = conn.getresponse()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        if r.status in (301, 302, 303, 307, 308) and "location" in hdrs:
            conn.close()
            url = urljoin(url, hdrs["location"])
            continue
        body = b"" if method == "HEAD" else r.read(cap + 1)
        conn.close()
        return url, r.status, hdrs, body[:cap], len(body) > cap
    raise Refused("too many redirects")


class Meta(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags: list[tuple[str, str]] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            key = a.get("property") or a.get("name")
            if key and a.get("content") is not None:
                self.tags.append((key.strip().lower(), a["content"].strip()))
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def first(tags, *keys):
    for k in keys:
        for tk, v in tags:
            if tk == k and v:
                return v
    return None


def mp4_boxes(data: bytes) -> list[str]:
    """Top-level MP4 box names in file order (enough to see faststart)."""
    out, i = [], 0
    while i + 8 <= len(data) and len(out) < 20:
        size, name = struct.unpack(">I4s", data[i:i + 8])
        if size == 1 and i + 16 <= len(data):
            size = struct.unpack(">Q", data[i + 8:i + 16])[0]
        out.append(name.decode("latin-1"))
        if size < 8:
            break
        i += size
    return out


def image_size(data: bytes):
    """(format, width, height) from a PNG, JPEG, GIF or WebP header, else None."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return ("png",) + struct.unpack(">II", data[16:24])
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ("gif",) + struct.unpack("<HH", data[6:10])
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X":
            w = int.from_bytes(data[24:27], "little") + 1
            h = int.from_bytes(data[27:30], "little") + 1
            return ("webp", w, h)
        if chunk == b"VP8 ":
            w, h = struct.unpack("<HH", data[26:30])
            return ("webp", w & 0x3FFF, h & 0x3FFF)
        if chunk == b"VP8L":
            b = data[21:25]
            w = 1 + (((b[1] & 0x3F) << 8) | b[0])
            h = 1 + (((b[3] & 0xF) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
            return ("webp", w, h)
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            seg = struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return ("jpeg", w, h)
            i += 2 + seg
    return None


def probe(data: bytes) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
        f.write(data)
        f.flush()
        try:
            res = subprocess.run(
                ["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                 "-show_streams", f.name], capture_output=True, text=True, timeout=20)
            return json.loads(res.stdout or "{}")
        except Exception as e:      # noqa: BLE001 — report, never 500
            return {"error": str(e)}


# Label → (group, key). Groups order the report; keys let the page work out
# per-platform verdicts.
GROUPS = [
    ("Page answers 200", "page", "page_ok"), ("Facebook crawler", "page", "fb"),
    ("og:image present", "tags", "image"), ("twitter:card", "tags", "twcard"),
    ("og:video present", "tags", "video"), ("Video URL is absolute", "tags", "https"),
    ("og:video:type", "tags", "vtype"), ("og:video:width", "tags", "vdims"),
    ("Video answers 200", "video", "v200"), ("Served as video/mp4", "video", "vctype"),
    ("Range requests", "video", "range"), ("Faststart", "video", "faststart"),
    ("H.264", "video", "h264"), ("yuv420p", "video", "yuv"), ("Real size", "video", "vsize"),
    ("Duration", "video", "dur"), ("Discord will play", "platform", "discord"),
    ("iMessage size", "platform", "imsize"), ("Fallback", "poster", ""),
]


def check(ok, label, detail=""):
    group, key = next(((g, k) for pre, g, k in GROUPS if label.startswith(pre)), ("other", ""))
    return {"ok": bool(ok), "label": label, "detail": detail, "group": group, "key": key}


def inspect(url: str) -> dict:
    if not re.match(r"^https?://", url):
        url = "https://" + url
    final, status, hdrs, body, _ = fetch(url, UA["x"])
    p = Meta()
    p.feed(body.decode("utf-8", "replace"))
    tags = p.tags
    base = final
    absu = lambda v: urljoin(base, v) if v else None  # noqa: E731
    card = {
        "title": first(tags, "og:title", "twitter:title") or p.title.strip(),
        "description": first(tags, "og:description", "twitter:description", "description"),
        "image": absu(first(tags, "og:image", "og:image:secure_url", "twitter:image")),
        "video": absu(first(tags, "og:video:secure_url", "og:video", "og:video:url")),
        "video_type": first(tags, "og:video:type"),
        "video_w": first(tags, "og:video:width"),
        "video_h": first(tags, "og:video:height"),
        "twitter_card": first(tags, "twitter:card"),
        "site": first(tags, "og:site_name"),
        "og_type": first(tags, "og:type"),
        "domain": urlsplit(final).hostname,
    }
    checks = [check(status == 200, "Page answers 200 to Twitterbot", f"HTTP {status}, {final}")]
    _, fb_status, _, fb_body, _ = fetch(url, UA["facebook"])
    checks.append(check(fb_status == 200 and b"og:video" in fb_body,
                        "Facebook crawler sees og:video too", f"HTTP {fb_status}"))
    checks.append(check(bool(card["image"]), "og:image present (the poster)", card["image"] or "missing"))
    checks.append(check(card["twitter_card"] in ("summary_large_image", "player"), "twitter:card is summary_large_image",
                        card["twitter_card"] or "missing"))
    card["discord_plays"] = bool(card["video"]) and (
        (card["og_type"] or "").startswith("video") or card["twitter_card"] == "player")
    raw_video = first(tags, "og:video", "og:video:url", "og:video:secure_url")
    checks.append(check(bool(raw_video), "og:video present", card["video"] or "missing"))
    video = {}
    if card["video"]:
        checks.append(check(card["video"].startswith("https://"), "Video URL is absolute https", card["video"]))
        checks.append(check(card["video_type"] == "video/mp4", "og:video:type is video/mp4",
                            card["video_type"] or "missing"))
        checks.append(check(bool(card["video_w"] and card["video_h"]), "og:video:width/height given",
                            f"{card['video_w']}×{card['video_h']}"))
        _, vs, vh, vbody, trunc = fetch(card["video"], UA["x"], cap=VIDEO_CAP)
        ctype = vh.get("content-type", "")
        checks.append(check(vs == 200, "Video answers 200 to Twitterbot", f"HTTP {vs}"))
        checks.append(check(ctype.startswith("video/mp4"), "Served as video/mp4", ctype or "no content-type"))
        _, rs, rh, _, _ = fetch(card["video"], UA["x"], rng="bytes=0-1023", cap=2048)
        checks.append(check(rs == 206, "Range requests work (206)", f"HTTP {rs}, {rh.get('content-range', '')}"))
        size = len(vbody)
        checks.append(check(card["discord_plays"], "Discord will play it (og:type video.* or twitter:card player)",
                            f"og:type {card['og_type'] or 'missing'}, twitter:card {card['twitter_card'] or 'missing'}"))
        checks.append(check(not trunc and size <= 10 * 1024 * 1024, "iMessage size limit (≤ 10 MB)",
                            f"{size / 1024:,.0f} KB" + (" (over the 40 MB read cap)" if trunc else "")))
        boxes = mp4_boxes(vbody[:1024 * 1024] if not trunc else vbody)
        fast = "moov" in boxes and "mdat" in boxes and boxes.index("moov") < boxes.index("mdat")
        checks.append(check(fast, "Faststart (moov before mdat)", " → ".join(boxes[:6])))
        if not trunc:
            pr = probe(vbody)
            v = next((s for s in pr.get("streams", []) if s.get("codec_type") == "video"), {})
            a = next((s for s in pr.get("streams", []) if s.get("codec_type") == "audio"), None)
            dur = float(pr.get("format", {}).get("duration") or 0)
            video = {"codec": v.get("codec_name"), "profile": v.get("profile"), "pix_fmt": v.get("pix_fmt"),
                     "width": v.get("width"), "height": v.get("height"), "fps": v.get("r_frame_rate"),
                     "duration": round(dur, 2), "audio": bool(a), "bytes": size}
            checks.append(check(v.get("codec_name") == "h264", "H.264 video",
                                f"{v.get('codec_name')} {v.get('profile') or ''}".strip()))
            checks.append(check(v.get("pix_fmt") == "yuv420p", "yuv420p (plays everywhere)", v.get("pix_fmt") or "?"))
            match = str(v.get("width")) == str(card["video_w"]) and str(v.get("height")) == str(card["video_h"])
            checks.append(check(match, "Real size matches the tags",
                                f"file {v.get('width')}×{v.get('height')}, tags {card['video_w']}×{card['video_h']}"))
            checks.append(check(0 < dur <= 140, "Duration under 2:20", f"{dur:.1f} s"))
    poster = {}
    if card["image"]:
        _, ist, ih, ibody, _ = fetch(card["image"], UA["x"], cap=6 * 1024 * 1024)
        ctype = ih.get("content-type", "")
        checks.append(check(ist == 200 and ctype.startswith("image/"),
                            "Fallback: poster answers 200 as an image",
                            f"HTTP {ist}, {ctype}, {len(ibody) / 1024:,.0f} KB"))
        dims = image_size(ibody)
        if dims:
            fmt, iw, ih_ = dims
            poster = {"format": fmt, "width": iw, "height": ih_, "bytes": len(ibody)}
            checks.append(check(fmt in ("png", "jpeg", "gif", "webp"),
                                "Fallback: poster is PNG, JPEG, GIF or WebP", fmt))
            checks.append(check(iw >= 600 and abs(iw / ih_ - 1.91) < 0.05,
                                "Fallback: poster is a large card (≥ 600 px wide, 1.91:1)",
                                f"{iw}×{ih_} ({iw / ih_:.2f}:1)"))
            tw, th = first(tags, "og:image:width"), first(tags, "og:image:height")
            if tw and th:
                checks.append(check(str(iw) == tw and str(ih_) == th,
                                    "Fallback: poster size matches og:image:width/height",
                                    f"file {iw}×{ih_}, tags {tw}×{th}"))
            if video.get("width"):
                same = abs(iw / ih_ - video["width"] / video["height"]) < 0.02
                checks.append(check(same, "Fallback: poster and video have the same shape",
                                    f"poster {iw / ih_:.2f}:1, video {video['width'] / video['height']:.2f}:1"))
            checks.append(check(len(ibody) <= 5 * 1024 * 1024, "Fallback: poster ≤ 5 MB",
                                f"{len(ibody) / 1024:,.0f} KB"))
        else:
            checks.append(check(False, "Fallback: poster size readable", "unknown image format"))
        alt = first(tags, "og:image:alt", "twitter:image:alt")
        checks.append(check(bool(alt), "Fallback: poster has alt text", alt or "missing"))
    order = ["page", "tags", "video", "platform", "poster", "other"]
    checks.sort(key=lambda x: order.index(x["group"]))      # stable: keeps order within a group
    result = {"url": url, "final_url": final, "card": card, "checks": checks, "video": video,
              "poster": poster,
              "tags": [t for t in tags if t[0].startswith(("og:", "twitter:"))]}
    result.update(verdicts(result))
    return result


def load_checks() -> None:
    global _checks
    try:
        with open(COUNTER_PATH, encoding="utf-8") as f:
            _checks = int(f.read().strip() or 0)
    except (OSError, ValueError):
        _checks = 0


def count_check() -> int:
    """Add one successful check to the total and return it. The file is
    replaced atomically, so a crash mid-write never leaves it half written."""
    global _checks
    with _lock:
        _checks += 1
        if COUNTER_PATH:
            try:
                with open(COUNTER_PATH + ".tmp", "w", encoding="utf-8") as f:
                    f.write(str(_checks))
                os.replace(COUNTER_PATH + ".tmp", COUNTER_PATH)
            except OSError:
                pass
        return _checks


def track(event: str, client: str, ua: str, props: dict) -> None:
    """Send one server-side event to PostHog, in the background, never raising.
    Covers API callers that never run the page's JavaScript. The distinct_id is
    a daily hash of IP and user agent: counts visitors without storing either."""
    if not POSTHOG_KEY:
        return
    day = time.strftime("%Y-%m-%d", time.gmtime())
    who = hashlib.sha256(f"{day}|{client}|{ua}".encode()).hexdigest()[:24]
    body = json.dumps({"api_key": POSTHOG_KEY, "event": event, "distinct_id": who,
                       "properties": {**props, "$ip": client, "$raw_user_agent": ua,
                                      "$process_person_profile": False, "$lib": "checkmyog-server"}}).encode()

    def send():
        try:
            req = urllib.request.Request(f"{POSTHOG_HOST}/i/v0/e/", data=body,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).close()
        except Exception:      # noqa: BLE001 — analytics must never break a check
            pass
    threading.Thread(target=send, daemon=True).start()


def load_support() -> dict:
    try:
        with open(os.path.join(ROOT, "support.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"as_of": "?", "platforms": [], "untested": []}


def verdicts(r: dict) -> dict:
    """What each app shows for this URL. Same rules as the page: a platform
    that plays og:video plays it only if every video check passes and the
    platform's own rule (iMessage size, Discord og:type) holds; every other
    platform shows og:image (or a text-only card without one)."""
    c, ok = r["card"], {x["key"]: x["ok"] for x in r["checks"] if x["key"]}
    video_ok = bool(c["video"]) and all(
        x["ok"] for x in r["checks"] if x["group"] == "video" or x["key"] in ("video", "https", "vtype"))
    poster = "poster" if c["image"] else "none"
    rule = {"iMessage": ok.get("imsize"), "Discord": ok.get("discord")}
    sup = load_support()
    platforms = []
    for p in sup.get("platforms", []):
        name = p["platform"]
        if p["status"] == "plays" or name in rule:
            res = "plays" if video_ok and rule.get(name, True) else poster
        else:
            res = poster
        platforms.append({"platform": name, "result": res, "note": p.get("note", ""),
                          "source": p.get("source", "")})
    scored = [x for x in r["checks"] if x["group"] != "platform"]
    return {
        "summary": {
            "video_plays_in": [p["platform"] for p in platforms if p["result"] == "plays"],
            "everywhere_else": poster,
            "checks_passed": sum(1 for x in scored if x["ok"]),
            "checks_failed": [x["label"] for x in scored if not x["ok"]],
        },
        "platforms": platforms,
        "support_as_of": sup.get("as_of"),
        "untested_platforms": sup.get("untested", []),
    }


def to_markdown(r: dict) -> str:
    c, sm, v, p = r["card"], r["summary"], r.get("video") or {}, r.get("poster") or {}
    out = [f"# Check My OG: {r['final_url']}", ""]
    out.append(f"- Video plays in: {', '.join(sm['video_plays_in']) or 'none'}")
    out.append(f"- Everywhere else: {'og:image poster' if sm['everywhere_else'] == 'poster' else 'text-only card (no og:image)'}")
    out.append(f"- Checks: {sm['checks_passed']} passed, {len(sm['checks_failed'])} failed")
    out += ["", "## Files", ""]
    if c.get("video"):
        out.append(f"- og:video: {c['video']}")
        if v.get("codec"):
            out.append(f"  - {v['codec']} {v.get('profile') or ''}, {v['pix_fmt']}, {v['width']}x{v['height']}, "
                       f"{v['fps']} fps, {v['duration']} s, {'audio' if v['audio'] else 'no audio'}, {v['bytes']} bytes")
    else:
        out.append("- og:video: missing")
    out.append(f"- og:image: {c.get('image') or 'missing'}")
    if p.get("width"):
        out.append(f"  - {p['format']}, {p['width']}x{p['height']}, {p['bytes']} bytes")
    out += ["", "## Checks", "", "| Result | Group | Check | Value |", "|---|---|---|---|"]
    for x in r["checks"]:
        res = ("YES" if x["ok"] else "NO") if x["group"] == "platform" else ("PASS" if x["ok"] else "FAIL")
        cell = lambda t: str(t).replace("|", "\\|").replace("\n", " ")  # noqa: E731
        label = x["label"].removeprefix("Fallback: ")
        out.append(f"| {res} | {x['group']} | {cell(label[:1].upper() + label[1:])} | {cell(x['detail'])} |")
    out += ["", f"## Platforms (support as of {r.get('support_as_of')})", "", "| App | This URL | Note |", "|---|---|---|"]
    for pl in r["platforms"]:
        out.append(f"| {pl['platform']} | {pl['result']} | {pl['note']} |")
    if r.get("untested_platforms"):
        out += ["", f"No evidence either way: {', '.join(r['untested_platforms'])}."]
    out += ["", "## Tags", ""] + [f"- `{k}`: {val}" for k, val in r["tags"]]
    return "\n".join(out) + "\n"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    extensions_map = {**SimpleHTTPRequestHandler.extensions_map,
                      ".txt": "text/plain; charset=utf-8", ".md": "text/markdown; charset=utf-8",
                      ".json": "application/json", ".mp4": "video/mp4"}

    def send_head(self):
        """Static files, plus the single byte range SimpleHTTPRequestHandler
        ignores. The site's own og:video has to pass the Range check it runs."""
        path = self.translate_path(self.path)
        self._static = os.path.isfile(path)
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())
        if not (self._static and m and any(m.groups())):
            return super().send_head()
        size, (a, b) = os.path.getsize(path), m.groups()
        start, end = (int(a), min(int(b or size - 1), size - 1)) if a else (max(size - int(b), 0), size - 1)
        if start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        with open(path, "rb") as f:
            f.seek(start)
            body = f.read(end - start + 1)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        return io.BytesIO(body)

    # Files served with {{PUBLIC_URL}}, {{POSTHOG_KEY}} and {{POSTHOG_HOST}} filled in.
    TEMPLATES = {"/": ("index.html", "text/html; charset=utf-8"),
                 "/index.html": ("index.html", "text/html; charset=utf-8"),
                 "/llms.txt": ("llms.txt", "text/plain; charset=utf-8"),
                 "/openapi.json": ("openapi.json", "application/json")}

    def _template(self, name, ctype):
        with open(os.path.join(ROOT, name), encoding="utf-8") as f:
            text = f.read()
        for k, v in (("PUBLIC_URL", PUBLIC_URL), ("POSTHOG_KEY", POSTHOG_KEY), ("POSTHOG_HOST", POSTHOG_HOST)):
            text = text.replace("{{" + k + "}}", v)
        self._text(200, text, ctype)

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        if getattr(self, "_static", False):
            self.send_header("Accept-Ranges", "bytes")
        # Agents and other sites may call the API and read llms.txt from a browser.
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def _text(self, code, text, ctype="text/markdown; charset=utf-8"):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlsplit(self.path)
        if u.path == "/healthz":
            return self._text(200, "ok\n", "text/plain; charset=utf-8")
        if u.path in ("/api", "/api/"):
            return self._template("llms.txt", "text/markdown; charset=utf-8")
        if u.path in self.TEMPLATES:
            return self._template(*self.TEMPLATES[u.path])
        if u.path == "/api/stats":
            return self._json(200, {"checks": _checks})
        if u.path not in ("/api/inspect", "/api/check"):
            return super().do_GET()
        client = (CLIENT_IP_HEADER and self.headers.get(CLIENT_IP_HEADER)) or self.client_address[0]
        now = time.time()
        with _lock:
            hits = [t for t in _hits.get(client, []) if now - t < RATE[1]]
            if len(hits) >= RATE[0]:
                return self._json(429, {"error": "Too many checks; wait a minute."})
            _hits[client] = hits + [now]
            if len(_hits) > 10_000:      # forget idle clients so the dict can't grow forever
                for k in [k for k, v in _hits.items() if now - v[-1] >= RATE[1]]:
                    del _hits[k]
        qs = parse_qs(u.query)
        url = (qs.get("url") or [""])[0].strip()
        md = (qs.get("format") or [""])[0] in ("md", "markdown", "text") or \
            "text/markdown" in (self.headers.get("Accept") or "")
        if not url:
            return self._json(400, {"error": "Give a URL: /api/inspect?url=https://example.com/"})
        ua = self.headers.get("User-Agent") or ""
        props = {"url": url, "domain": urlsplit(url if "://" in url else "https://" + url).hostname,
                 "format": "md" if md else "json",
                 "via": "web" if self.headers.get("X-Checkmyog-Client") == "web" else "api"}
        t0 = time.monotonic()
        try:
            r = inspect(url)
            r["total_checks"] = count_check()
            self._text(200, to_markdown(r)) if md else self._json(200, r)
            sm = r["summary"]
            props.update(status=200, final_url=r["final_url"], video_plays_in=sm["video_plays_in"],
                         checks_passed=sm["checks_passed"], checks_failed=len(sm["checks_failed"]),
                         has_video=bool(r["card"]["video"]), has_image=bool(r["card"]["image"]))
        except Refused as e:
            self._json(400, {"error": str(e)})
            props.update(status=400, error=str(e))
        except Exception as e:      # noqa: BLE001
            self._json(502, {"error": f"Fetch failed: {type(e).__name__}: {e}"})
            props.update(status=502, error=type(e).__name__)
        props["duration_ms"] = round((time.monotonic() - t0) * 1000)
        track("check_served", client, ua, props)


if __name__ == "__main__":
    load_checks()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    print(f"Serving :{PORT} from {ROOT}", flush=True)
    server.serve_forever()
    server.server_close()
    print("Stopped cleanly", flush=True)
