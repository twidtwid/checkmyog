"""Tests for server.py. Standard library only: python3 -m unittest discover -s tests"""
import base64
import json
import os
import struct
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server  # noqa: E402

PNG_1200x630 = b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", 1200, 630) + b"\x08\x02\x00\x00\x00" + b"\x00" * 64


def jpeg(w, h):
    sof = b"\xff\xc0" + struct.pack(">HBHH", 17, 8, h, w) + b"\x03" + b"\x00" * 9
    return b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9 + sof + b"\x00" * 16


class Parsing(unittest.TestCase):
    def test_png(self):
        self.assertEqual(server.image_size(PNG_1200x630), ("png", 1200, 630))

    def test_gif(self):
        self.assertEqual(server.image_size(b"GIF89a" + struct.pack("<HH", 640, 336) + b"\x00" * 8), ("gif", 640, 336))

    def test_jpeg(self):
        self.assertEqual(server.image_size(jpeg(1200, 630)), ("jpeg", 1200, 630))

    def test_webp_vp8x(self):
        data = b"RIFF\x00\x00\x00\x00WEBPVP8X" + b"\x00" * 8 + (1199).to_bytes(3, "little") + (629).to_bytes(3, "little")
        self.assertEqual(server.image_size(data), ("webp", 1200, 630))

    def test_unknown_image(self):
        self.assertIsNone(server.image_size(b"not an image"))

    def test_mp4_faststart_order(self):
        box = lambda name, n=0: struct.pack(">I4s", 8 + n, name) + b"\x00" * n  # noqa: E731
        self.assertEqual(server.mp4_boxes(box(b"ftyp", 8) + box(b"moov", 16) + box(b"mdat", 4)), ["ftyp", "moov", "mdat"])

    def test_meta_parser(self):
        p = server.Meta()
        p.feed('<title> Hi </title><meta property="og:title" content="T"><meta name="twitter:card" content="summary_large_image">'
               '<link rel="shortcut icon" href="/favicon.ico"><link rel="apple-touch-icon" href="/touch.png">')
        self.assertEqual(p.tags, [("og:title", "T"), ("twitter:card", "summary_large_image")])
        self.assertEqual(p.title.strip(), "Hi")
        self.assertEqual(p.icons, [("icon", "/favicon.ico"), ("apple-touch-icon", "/touch.png")])


class Refusals(unittest.TestCase):
    def test_private_addresses(self):
        for host in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "192.168.1.1", "localhost", "::1"):
            with self.subTest(host=host), self.assertRaises(server.Refused):
                server._safe_ip(host, 80)

    def test_scheme_and_port(self):
        with self.assertRaises(server.Refused):
            server.fetch("ftp://example.com/", "x")
        with self.assertRaises(server.Refused):
            server.fetch("http://example.com:8080/", "x")


class Verdicts(unittest.TestCase):
    def test_imessage_and_discord_play_a_good_mp4(self):
        """Both apps play og:video when every video check passes; one failed
        video check (here faststart) drops both. Discord has no rule of its own."""
        r = {"card": {"video": "https://example.com/v.mp4", "image": "https://example.com/p.png"},
             "checks": [server.check(True, "og:video present"), server.check(True, "Video URL is absolute https"),
                        server.check(True, "og:video:type is video/mp4"), server.check(True, "H.264 video"),
                        server.check(True, "iMessage size guideline (video + poster ≤ 10 MB)")]}
        self.assertEqual(server.verdicts(r)["summary"]["video_plays_in"], ["iMessage", "Discord"])
        r["checks"].append(server.check(False, "Faststart (moov before mdat)"))
        self.assertEqual(server.verdicts(r)["summary"]["video_plays_in"], [])
        r["checks"][-1] = server.check(True, "Faststart (moov before mdat)")
        r["checks"][-2] = server.check(False, "iMessage size guideline (video + poster ≤ 10 MB)")
        self.assertEqual(server.verdicts(r)["summary"]["video_plays_in"], ["Discord"])

    def test_a_video_over_50_mb_plays_in_neither(self):
        """Over Apple's 10 MB only Discord plays; over Discord's 50 MB neither does."""
        r = {"card": {"video": "https://example.com/v.mp4", "image": "https://example.com/p.png"},
             "checks": [server.check(True, "og:video present"), server.check(False, "iMessage size guideline (video + poster ≤ 10 MB)"),
                        server.check(True, "Discord size limit (video ≤ 50 MB)")]}
        self.assertEqual(server.verdicts(r)["summary"]["video_plays_in"], ["Discord"])
        r["checks"][-1] = server.check(False, "Discord size limit (video ≤ 50 MB)")
        self.assertEqual(server.verdicts(r)["summary"]["video_plays_in"], [])
        self.assertEqual(r["checks"][-1]["group"], "platform")    # a fact about the apps, not a failed check

    def test_refresh_guidance_comes_from_support(self):
        """Apps that document a refresh carry it with its source; the rest get null."""
        r = {"card": {"video": None, "image": "https://example.com/p.png"}, "checks": [],
             "final_url": "https://example.com/", "tags": []}
        r.update(server.verdicts(r))
        by = {p["platform"]: p for p in r["platforms"]}
        self.assertIn("?v=2", by["Discord"]["refresh"])
        self.assertTrue(by["Discord"]["refresh_source"].startswith("https://docs.discord.com/"))
        self.assertIn("Sharing Debugger", by["Facebook"]["refresh"])
        self.assertIsNone(by["Slack"]["refresh"])
        self.assertTrue(all(p["refresh_source"] for p in r["platforms"] if p["refresh"]))
        self.assertIn("?v=2", r["refresh_tip"])
        md = server.to_markdown(r)
        self.assertIn("## Stuck on an old preview?", md)
        self.assertIn("- Everyone else: no documented way to refresh", md)

    def test_a_note_counts_in_neither_direction(self):
        """A page without og:video previews fine everywhere, so that row is a
        note: ok null, info true, left out of both passed and failed."""
        r = {"card": {"video": None, "image": "https://example.com/p.png"},
             "checks": [server.check(True, "Page answers 200 to Twitterbot"), server.note("og:video (optional)", "none"),
                        server.check(False, "Fallback: poster has alt text")], "final_url": "https://example.com/", "tags": []}
        r.update(server.verdicts(r))
        self.assertEqual((r["summary"]["checks_passed"], r["summary"]["checks_failed"]), (1, ["Fallback: poster has alt text"]))
        self.assertEqual(r["summary"]["video_plays_in"], [])
        md = server.to_markdown(r)
        self.assertIn("- Checks: 1 passed, 1 failed, 1 note\n", md)
        self.assertIn("| NOTE | tags | og:video (optional) | none |", md)
        self.assertIn("| FAIL | poster | Poster has alt text |", md)


class Fixture(BaseHTTPRequestHandler):
    """A tiny site with a card: / has og tags, /og.png is a 1200x630 PNG."""
    def log_message(self, *a):
        pass

    def do_GET(self):
        port = self.server.server_address[1]
        if self.path == "/og.png":
            body, ctype = PNG_1200x630, "image/png"
        elif self.path == "/moved":
            self.send_response(301)
            self.send_header("Location", "/")
            self.end_headers()
            return
        else:
            body = (f'<html><head><title>Fixture</title><meta property="og:title" content="Fixture page">'
                    f'<meta property="og:image" content="http://127.0.0.1:{port}/og.png">'
                    f'<meta property="og:image:alt" content="A card"><meta name="twitter:card" content="summary_large_image">'
                    f'<link rel="icon" href="/favicon.ico"><link rel="apple-touch-icon" href="/touch.png">'
                    f'</head></html>').encode()
            ctype = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class EndToEnd(unittest.TestCase):
    """Runs the real checker and HTTP handler against a local fixture site.
    The private-address guard is relaxed for 127.0.0.1 only, inside these tests."""

    @classmethod
    def setUpClass(cls):
        cls.site = serve(Fixture)
        cls.port = cls.site.server_address[1]
        cls._orig = server._safe_ip, server.ALLOWED_PORTS, server.POSTHOG_KEY
        server._safe_ip = lambda host, port: "127.0.0.1" if host == "127.0.0.1" else cls._orig[0](host, port)
        server.ALLOWED_PORTS = (80, 443, cls.port)
        server.POSTHOG_KEY = ""
        cls.app = serve(server.Handler)
        cls.base = f"http://127.0.0.1:{cls.app.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        server._safe_ip, server.ALLOWED_PORTS, server.POSTHOG_KEY = cls._orig
        cls.site.shutdown()
        cls.app.shutdown()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as r:
                return r.status, r.headers.get("Content-Type"), r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read().decode()

    def test_long_domain_host_serves_its_own_page(self):
        import http.client
        host = next(iter(server.Handler.HOST_PAGES))
        c = http.client.HTTPConnection("127.0.0.1", self.app.server_address[1], timeout=10)
        c.request("GET", "/", headers={"Host": host})
        r = c.getresponse()
        body = r.read().decode()
        c.close()
        self.assertEqual(r.status, 200)
        self.assertNotIn("{{", body)
        self.assertIn(f'<meta property="og:image" content="https://{host}/long-og.png">', body)
        self.assertIn(f'<meta property="og:video" content="https://{host}/long-og.mp4">', body)
        self.assertIn(f'<link rel="canonical" href="https://{host}/">', body)
        self.assertIn("Check My OG", self.get("/")[2])  # the main host is still the checker
        for name, magic in (("/long-og.png", b"\x89PNG"), ("/long-og.mp4", b"ftyp")):
            with urllib.request.urlopen(self.base + name, timeout=10) as rr:
                self.assertIn(magic, rr.read(32))

    def test_app_redirects_http_to_https_except_the_long_domain(self):
        import http.client
        port = self.app.server_address[1]
        for host, expect in (("checkmyog.com", 301), (next(iter(server.Handler.HOST_PAGES)), 200)):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("GET", "/?x=1", headers={"Host": host, "Fly-Forwarded-Proto": "http"})
            r = c.getresponse()
            r.read()
            c.close()
            self.assertEqual(r.status, expect, host)
            if expect == 301:
                self.assertEqual(r.getheader("Location"), f"https://{host}/?x=1")
        self.assertEqual(self.get("/healthz")[0], 200)  # no proxy header: no redirect

    def test_inspect_json(self):
        code, _, body = self.get(f"/api/inspect?url=http://127.0.0.1:{self.port}/moved")
        self.assertEqual(code, 200)
        r = json.loads(body)
        self.assertEqual(r["card"]["title"], "Fixture page")
        self.assertEqual(r["card"]["og_tags"], 4)
        self.assertEqual(r["card"]["icon"], f"http://127.0.0.1:{self.port}/touch.png")
        self.assertEqual(r["poster"], {"format": "png", "width": 1200, "height": 630, "bytes": len(PNG_1200x630)})
        self.assertEqual(r["summary"]["everywhere_else"], "poster")
        self.assertEqual(r["summary"]["video_plays_in"], [])
        # No og:video is a note, not a failure: the fixture passes everything else.
        self.assertEqual(r["summary"]["checks_failed"], [])
        video_note = next(x for x in r["checks"] if x["label"] == "og:video (optional)")
        self.assertEqual((video_note["ok"], video_note["info"], video_note["group"]), (None, True, "tags"))
        self.assertEqual(r["summary"]["checks_passed"],
                         sum(1 for x in r["checks"] if x["ok"] is True and x["group"] != "platform"))
        self.assertIn("Facebook crawler sees og:image too", [x["label"] for x in r["checks"]])
        self.assertTrue(r["final_url"].endswith("/"))
        self.assertEqual(r["build"], server.BUILD)

    def test_inspect_markdown(self):
        code, ctype, body = self.get(f"/api/inspect?url=http://127.0.0.1:{self.port}/&format=md")
        self.assertEqual(code, 200)
        self.assertTrue(ctype.startswith("text/markdown"))
        self.assertTrue(body.startswith("# Check My OG: "))
        self.assertIn("| og:image present", body)          # labels keep their case: og:image, iMessage, yuv420p
        self.assertNotIn("Og:", body)
        self.assertIn("| Poster answers 200", body)         # "Fallback: " dropped, sentence starts with a capital
        self.assertIn("| Poster is PNG, JPEG, GIF or WebP", body)   # and the rest of the label is untouched
        self.assertIn("| NOTE | tags | og:video (optional) |", body)
        self.assertIn(" failed, 1 note\n", body)
        self.assertIn("- og:video: none (optional)", body)

    def test_refused_and_missing(self):
        self.assertEqual(self.get("/api/inspect")[0], 400)
        self.assertEqual(self.get("/api/inspect?url=http://10.0.0.1/")[0], 400)

    def test_templates_filled(self):
        for path in ("/", "/llms.txt", "/openapi.json"):
            code, _, body = self.get(path)
            self.assertEqual(code, 200, path)
            self.assertNotIn("{{", body, path)
            self.assertIn(server.PUBLIC_URL, body, path)
        json.loads(self.get("/openapi.json")[2])
        # The page carries the build id the API reports, so a stale tab can tell it is stale.
        self.assertRegex(server.BUILD, r"^[0-9a-f]{12}$")
        self.assertIn(f'const BUILD = "{server.BUILD}";', self.get("/")[2])
        self.assertEqual(json.loads(self.get("/api/stats")[2])["build"], server.BUILD)

    def test_static_range(self):
        size = os.path.getsize(os.path.join(server.ROOT, "og.mp4"))

        def fetch(rng):
            req = urllib.request.Request(self.base + "/og.mp4?v=1", headers={"Range": rng})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, r.headers, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.headers, e.read()
        code, h, body = fetch("bytes=0-1023")
        self.assertEqual((code, h["Content-Range"], len(body), h["Content-Type"]),
                         (206, f"bytes 0-1023/{size}", 1024, "video/mp4"))
        self.assertEqual(body[4:8], b"ftyp")
        code, h, body = fetch("bytes=-100")
        self.assertEqual((code, h["Content-Range"], len(body)), (206, f"bytes {size - 100}-{size - 1}/{size}", 100))
        self.assertEqual(fetch(f"bytes={size}-")[0], 416)
        code, h, body = fetch("")
        self.assertEqual((code, h["Accept-Ranges"], len(body)), (200, "bytes", size))

    def test_counter_counts_successes_and_persists(self):
        import tempfile
        orig = server.COUNTER_PATH, server._checks
        with tempfile.TemporaryDirectory() as d:
            server.COUNTER_PATH = os.path.join(d, "checks")
            with open(server.COUNTER_PATH, "w") as f:
                f.write("41")
            server.load_checks()
            try:
                self.assertEqual(json.loads(self.get("/api/stats")[2])["checks"], 41)
                r = json.loads(self.get(f"/api/inspect?url=http://127.0.0.1:{self.port}/")[2])
                self.assertEqual(r["total_checks"], 42)
                self.assertEqual(self.get("/api/inspect?url=http://10.0.0.1/")[0], 400)   # refused: not counted
                self.assertEqual(self.get("/api/inspect")[0], 400)
                req = urllib.request.Request(f"{self.base}/api/inspect?url=http://127.0.0.1:{self.port}/",
                                             headers={"X-Checkmyog-Trigger": "default"})
                with urllib.request.urlopen(req, timeout=10) as resp:      # the page's own load-time check
                    self.assertEqual(json.loads(resp.read())["total_checks"], 42)
                self.assertEqual(json.loads(self.get("/api/stats")[2])["checks"], 42)
                server._checks = 0
                server.load_checks()                         # a restart reads the file back
                self.assertEqual(server._checks, 42)
                self.assertFalse(os.path.exists(server.COUNTER_PATH + ".tmp"))
            finally:
                server.COUNTER_PATH, server._checks = orig

    def test_page_load_self_check_is_cached(self):
        calls = []
        orig_inspect = server.inspect
        server.inspect = lambda u: calls.append(u) or orig_inspect(u)
        server._self_cache.clear()
        url = f"{self.base}/api/inspect?url=http://127.0.0.1:{self.port}/"
        try:
            for _ in range(3):
                req = urllib.request.Request(url, headers={"X-Checkmyog-Trigger": "default"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    self.assertEqual(json.loads(resp.read())["summary"]["checks_failed"], [])
            self.assertEqual(len(calls), 1)                  # three page loads, one real check
            self.get(f"/api/inspect?url=http://127.0.0.1:{self.port}/")
            self.assertEqual(len(calls), 2)                  # a check someone asked for is always fresh
        finally:
            server.inspect = orig_inspect
            server._self_cache.clear()

    def test_busy_when_every_slot_is_taken(self):
        orig = server._slots
        server._slots = server.threading.BoundedSemaphore(1)
        server._slots.acquire()
        orig_acquire = server._slots.acquire
        server._slots.acquire = lambda timeout=None: orig_acquire(timeout=0.1)
        try:
            self.assertEqual(self.get(f"/api/inspect?url=http://127.0.0.1:{self.port}/")[0], 503)
        finally:
            server._slots = orig

    def test_healthz_and_static(self):
        self.assertEqual(self.get("/healthz")[:1], (200,))
        code, ctype, _ = self.get("/support.json")
        self.assertEqual((code, ctype), (200, "application/json"))

    def raw(self, path, method="GET", headers=None):
        req = urllib.request.Request(self.base + path, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_head_is_get_without_the_body(self):
        """Templates, endpoints, static files and 404s alike: same status and
        headers as GET, Content-Length of the real body, nothing after the headers."""
        for path in ("/", "/healthz", "/api/stats", "/llms.txt", "/support.json", "/og.mp4", "/nope"):
            with self.subTest(path=path):
                code, h, body = self.raw(path, "HEAD")
                gcode, gh, gbody = self.raw(path)
                self.assertEqual((code, body), (gcode, b""))
                self.assertEqual(h.get("Content-Type"), gh.get("Content-Type"))
                self.assertEqual(h.get("Content-Length"), str(len(gbody)))
        code, h, body = self.raw(f"/api/inspect?url=http://127.0.0.1:{self.port}/", "HEAD")
        self.assertEqual((code, h.get("Content-Type"), body), (200, "application/json", b""))
        self.assertEqual(self.raw("/api/inspect", "OPTIONS")[1].get("Access-Control-Allow-Methods"), "GET, HEAD, OPTIONS")


class Analytics(unittest.TestCase):
    def test_track_posts_event_and_hashes_visitor(self):
        got = []

        class Capture(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                got.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(200)
                self.end_headers()

        cap = serve(Capture)
        orig = server.POSTHOG_KEY, server.POSTHOG_HOST
        server.POSTHOG_KEY, server.POSTHOG_HOST = "phc_test", f"http://127.0.0.1:{cap.server_address[1]}"
        try:
            server.track("check_served", "203.0.113.9", "curl/8", {"url": "https://example.com/"})
            for _ in range(50):
                if got:
                    break
                threading.Event().wait(0.05)
        finally:
            server.POSTHOG_KEY, server.POSTHOG_HOST = orig
            cap.shutdown()
        path, ev = got[0]
        self.assertEqual(path, "/i/v0/e/")
        self.assertEqual((ev["api_key"], ev["event"]), ("phc_test", "check_served"))
        self.assertEqual(ev["properties"]["url"], "https://example.com/")
        self.assertNotIn("203.0.113.9", ev["distinct_id"])

    def test_no_key_no_request(self):
        orig = server.POSTHOG_KEY
        server.POSTHOG_KEY = ""
        try:
            self.assertIsNone(server.track("x", "1.2.3.4", "ua", {}))
        finally:
            server.POSTHOG_KEY = orig


if __name__ == "__main__":
    unittest.main()
