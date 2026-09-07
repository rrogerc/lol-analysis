"""TFT HTTP serving reads one complete publication without running analysis."""
from contextlib import ExitStack
import gzip
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch

import tft
import tft_comps
import tft_site
import webapp


class Handler:
    def __init__(self, headers=None):
        self.headers = headers or {}
        self.sent_headers = {}
        self.wfile = io.BytesIO()
        self.code = None

    def send_response(self, code):
        self.code = code

    def send_header(self, name, value):
        self.sent_headers[name] = value

    def end_headers(self):
        pass

    def _json(self, value, code=200):
        self.code = code
        self.sent_headers["Cache-Control"] = "no-store"
        self.wfile.write(json.dumps(value).encode())


class TestTftSavedResponses(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(tft, "CACHE_DIR", str(self.directory)))
        self.stack.enter_context(patch.object(tft, "REFRESH_STATE_FILE", str(self.directory / "refresh.json")))
        for module, names in ((tft, ("load_snapshot", "engine", "api_meta", "warm", "cached_scenario")),
                              (tft_comps, ("warm", "api_meta", "cached_scenario"))):
            for name in names:
                self.stack.enter_context(patch.object(module, name, side_effect=AssertionError("HTTP must not calculate: " + name)))
        self.marker = self.directory / ".dashboard-ready"
        self.marker.write_text(json.dumps({"revision": "base-a", "compositionRevision": "comp-a", "site": {"siteGeneration": "site-a"}}))
        self.statuses = {
            "/api/tft/status.json": {"revision": "base-a", "ready": {"champion/cell": True}, "warmer": "idle"},
            "/api/tft/compositions/status.json": {"revision": "comp-a", "ready": {"c1-spread-mixed": True}, "warmer": "idle"},
        }
        self.payload = b'{"revision":"comp-a","results":[1,2,3],"value":-0.0}'
        self.identity = self.directory / "response.json"
        self.identity.write_bytes(self.payload)
        self.compressed = self.directory / "response.json.gz"
        self.compressed.write_bytes(gzip.compress(self.payload, mtime=0))
        def asset(url, *, compressed=False):
            if url not in ("/api/tft/compositions/c1-spread-mixed.json", "/api/tft/meta.json"):
                return None
            path = self.compressed if compressed else self.identity
            return {"path": path, "size": path.stat().st_size,
                    "etag": '"' + hashlib.sha256(path.read_bytes()).hexdigest() + '"',
                    "encoding": "gzip" if compressed else None}
        self.bundle = SimpleNamespace(manifest={"baselineRevision": "base-a", "compositionRevision": "comp-a"},
                                      json=lambda url: self.statuses[url], asset=asset)
        self.load = self.stack.enter_context(patch.object(tft_site, "load", return_value=self.bundle))

    def test_identity_and_gzip_serve_prebuilt_bytes_without_calculating(self):
        server = webapp.TftResponses()
        for accept in ("", "gzip", "br, gzip;q=0.8"):
            with self.subTest(accept=accept):
                handler = Handler({"Accept-Encoding": accept})
                server.send(handler, "/api/tft/meta.json")
                self.assertEqual(handler.code, 200)
                body = handler.wfile.getvalue()
                if accept:
                    self.assertEqual(handler.sent_headers["Content-Encoding"], "gzip")
                    body = gzip.decompress(body)
                self.assertEqual(body, self.payload)
                self.assertEqual(handler.sent_headers["Vary"], "Accept-Encoding")
                self.assertEqual(handler.sent_headers["Cache-Control"], "no-cache")

    def test_conditional_requests_use_the_selected_representation_etag(self):
        server = webapp.TftResponses()
        raw = Handler()
        server.send(raw, "/api/tft/meta.json")
        tag = raw.sent_headers["ETag"]
        conditional = Handler({"If-None-Match": '"different", W/' + tag})
        server.send(conditional, "/api/tft/meta.json")
        self.assertEqual(conditional.code, 304)
        self.assertEqual(conditional.wfile.getvalue(), b"")
        gzip_request = Handler({"Accept-Encoding": "gzip", "If-None-Match": tag})
        server.send(gzip_request, "/api/tft/meta.json")
        self.assertEqual(gzip_request.code, 200)
        self.assertNotEqual(tag, gzip_request.sent_headers["ETag"])

    def test_refresh_status_does_not_change_published_revision_or_readiness(self):
        server = webapp.TftResponses()
        Path(tft.REFRESH_STATE_FILE).write_text(json.dumps({"status": "running", "phase": "warming-compositions"}))
        for url, original in self.statuses.items():
            handler = Handler()
            server.send(handler, url)
            value = json.loads(handler.wfile.getvalue())
            self.assertEqual(value["revision"], original["revision"])
            self.assertEqual(value["ready"], original["ready"])
            self.assertEqual(value["warmer"], "idle")
            self.assertEqual(value["refresh"]["status"], "running")
        self.assertNotIn("refresh", self.statuses["/api/tft/status.json"])

    def test_new_marker_does_not_mix_files_into_running_publication(self):
        server = webapp.TftResponses()
        self.marker.write_text(json.dumps({"revision": "base-b", "compositionRevision": "comp-b", "site": {"siteGeneration": "site-b"}}))
        handler = Handler()
        server.send(handler, "/api/tft/compositions/c1-spread-mixed.json")
        self.assertEqual(handler.wfile.getvalue(), self.payload)
        self.load.assert_called_once_with({"siteGeneration": "site-a"})

    def test_missing_publication_returns_pending_without_fallback_calculation(self):
        self.marker.unlink()
        server = webapp.TftResponses()
        handler = Handler()
        server.send(handler, "/api/tft/meta.json")
        self.assertEqual(handler.code, 202)
        self.assertTrue(json.loads(handler.wfile.getvalue())["pending"])
        self.load.assert_not_called()

    def test_mismatched_marker_cannot_serve_a_mixed_generation(self):
        self.marker.write_text(json.dumps({"revision": "base-b", "compositionRevision": "comp-a", "site": {"siteGeneration": "site-a"}}))
        server = webapp.TftResponses()
        handler = Handler()
        server.send(handler, "/api/tft/meta.json")
        self.assertEqual(handler.code, 202)

    def test_unknown_path_is_not_resolved_as_a_file(self):
        server = webapp.TftResponses()
        handler = Handler()
        server.send(handler, "/api/tft/../../secret.json")
        self.assertEqual(handler.code, 404)

    def test_missing_file_does_not_send_success_headers(self):
        server = webapp.TftResponses()
        self.identity.unlink()
        handler = Handler()
        with self.assertRaises(FileNotFoundError):
            server.send(handler, "/api/tft/meta.json")
        self.assertIsNone(handler.code)

    def test_serve_starts_only_the_existing_lol_warmer(self):
        args = SimpleNamespace(no_warm=False, no_open=True, host="127.0.0.1", port=0)
        with patch.object(webapp, "AutoWarm") as warmer, \
             patch("http.server.ThreadingHTTPServer") as http_server, \
             patch.object(webapp.signal, "signal"):
            webapp.cmd_serve(args)
        self.assertEqual(warmer.call_args_list, [call(enabled=True)])
        warmer.return_value.stop.assert_called_once()
        http_server.return_value.serve_forever.assert_called_once()


class TestEncodingNegotiation(unittest.TestCase):
    def test_gzip_quality_and_explicit_refusal(self):
        for value in ("gzip", "br, gzip ; q=0.3", "*;q=1", "GZIP;q=1"):
            self.assertTrue(webapp._accepts_gzip(value), value)
        for value in ("", "br", "gzip;q=0, *;q=1", "gzip;q=broken", "gzip;q=NaN", "gzip;q=2"):
            self.assertFalse(webapp._accepts_gzip(value), value)


if __name__ == "__main__":
    unittest.main()
