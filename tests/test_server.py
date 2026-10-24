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
        p.feed('<title> Hi </title><meta property="og:title" content="T"><meta name="twitter:card" content="summary_large_image">')
        self.assertEqual(p.tags, [("og:title", "T"), ("twitter:card", "summary_large_image")])
        self.assertEqual(p.title.strip(), "Hi")


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

    def test_inspect_json(self):
        code, _, body = self.get(f"/api/inspect?url=http://127.0.0.1:{self.port}/moved")
        self.assertEqual(code, 200)
        r = json.loads(body)
        self.assertEqual(r["card"]["title"], "Fixture page")
        self.assertEqual(r["poster"], {"format": "png", "width": 1200, "height": 630, "bytes": len(PNG_1200x630)})
        self.assertEqual(r["summary"]["everywhere_else"], "poster")
        self.assertEqual(r["summary"]["video_plays_in"], [])
        self.assertIn("og:video present", r["summary"]["checks_failed"])
        self.assertTrue(r["final_url"].endswith("/"))

    def test_inspect_markdown(self):
        code, ctype, body = self.get(f"/api/inspect?url=http://127.0.0.1:{self.port}/&format=md")
        self.assertEqual(code, 200)
        self.assertTrue(ctype.startswith("text/markdown"))
        self.assertTrue(body.startswith("# Check My OG: "))

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

    def test_healthz_and_static(self):
        self.assertEqual(self.get("/healthz")[:1], (200,))
        code, ctype, _ = self.get("/support.json")
        self.assertEqual((code, ctype), (200, "application/json"))


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
