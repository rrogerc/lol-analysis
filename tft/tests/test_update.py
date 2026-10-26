"""HTTP transport, patch-note parsing, staged reconciliation, reviews, and refresh orchestration."""

import contextlib
from contextlib import ExitStack
import copy
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import formatdate
import errno
import fcntl
import hashlib
import http.client
import io
import json
from pathlib import Path
import queue
import shutil
import socket
import ssl
import tempfile
import threading
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from tft import sim as tft, site as tft_site, update as tft_update
from tft.update import ReviewRequired, reconcile


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay


class Reply:
    def __init__(self, body=b"ok", headers=None, status=200, read_error=None):
        self.body = body
        self.headers = headers if headers is not None else {}
        self.status = status
        self.read_error = read_error
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.closed = True

    def read(self):
        if self.read_error is not None:
            raise self.read_error
        return self.body


class TestGet(unittest.TestCase):
    URL = "https://example.invalid/assets/set.json"

    def setUp(self):
        self.clock = Clock()

    def get(self, opener, **kwargs):
        return tft_update.get(self.URL, opener=opener, sleep=self.clock.sleep,
                            monotonic=self.clock.monotonic, wall_time=lambda: 1700000000,
                            **kwargs)

    def test_preserves_bytes_and_copies_case_insensitive_duplicate_headers(self):
        headers = Message()
        headers["Last-Modified"] = "Mon, 01 Jan 2024 00:00:00 GMT"
        headers["X-Source"] = "first"
        headers["X-Source"] = "second"
        body = b"\x00\xff\r\n{\"space\":  1}\n"
        reply = Reply(body, headers)
        opener = Mock(return_value=reply)
        result = self.get(opener, headers={"User-Agent": "test", "Accept": "*/*"})
        self.assertEqual(result.body, body)
        self.assertEqual(result.headers.get("last-modified"), headers["Last-Modified"])
        self.assertEqual(result.headers.get_all("x-source"), ["first", "second"])
        self.assertIsNot(result.headers, headers)
        self.assertEqual((result.status, result.attempts), (200, 1))
        request = opener.call_args.args[0]
        self.assertEqual((request.get_method(), request.full_url), ("GET", self.URL))
        self.assertEqual(request.get_header("User-agent"), "test")
        self.assertEqual(opener.call_args.kwargs, {"timeout": 30.0})
        self.assertTrue(reply.closed)
        self.assertEqual(self.clock.sleeps, [])

    def test_recovers_from_temporary_dns(self):
        opener = Mock(side_effect=[URLError(socket.gaierror(socket.EAI_AGAIN, "temporary DNS")), Reply()])
        result = self.get(opener)
        self.assertEqual((result.body, result.attempts), (b"ok", 2))
        self.assertEqual(self.clock.sleeps, [1.0])
        self.assertEqual(opener.call_count, 2)

    def test_recovers_from_observed_eai_noname_dns_failure(self):
        opener = Mock(side_effect=[URLError(socket.gaierror(socket.EAI_NONAME, "name lookup failed")), Reply()])
        self.assertEqual(self.get(opener).attempts, 2)
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(self.clock.sleeps, [1])

    def test_retries_only_transient_transport_errors(self):
        failures = [TimeoutError("timeout"), ConnectionResetError("reset"),
            ConnectionAbortedError("aborted"), ConnectionRefusedError("refused"),
            BrokenPipeError("pipe"), OSError(errno.ENETUNREACH, "unreachable"),
            http.client.RemoteDisconnected("closed"), http.client.IncompleteRead(b"partial", 10),
            ssl.SSLEOFError(8, "closed"), URLError(TimeoutError("wrapped timeout"))]
        for error in failures:
            with self.subTest(error=type(error).__name__):
                opener = Mock(side_effect=[error, Reply()])
                self.assertEqual(self.get(opener, max_attempts=2).attempts, 2)
                self.assertEqual(opener.call_count, 2)

    def test_read_failure_closes_response_and_restarts_entire_download(self):
        first = Reply(b"discard", read_error=http.client.IncompleteRead(b"part", 8))
        second = Reply(b"whole new body")
        opener = Mock(side_effect=[first, second])
        result = self.get(opener)
        self.assertEqual(result.body, b"whole new body")
        self.assertTrue(first.closed and second.closed)

    def test_retry_http_statuses_and_close_error_streams(self):
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                stream = io.BytesIO(b"error")
                error = HTTPError(self.URL, status, "server failure", {}, stream)
                opener = Mock(side_effect=[error, Reply()])
                self.assertEqual(self.get(opener).attempts, 2)
                self.assertTrue(stream.closed)

    def test_error_status_returned_by_custom_opener_also_retries(self):
        first = Reply(status=503)
        opener = Mock(side_effect=[first, Reply()])
        self.assertEqual(self.get(opener).attempts, 2)
        self.assertTrue(first.closed)

    def test_404_and_other_permanent_statuses_are_not_retried(self):
        for status in (400, 401, 403, 404, 405, 410, 422, 501):
            with self.subTest(status=status):
                opener = Mock(side_effect=HTTPError(self.URL, status, "failure", {"Retry-After": "1"}, None))
                with self.assertRaises(HTTPError) as caught:
                    self.get(opener)
                self.assertEqual((caught.exception.code, caught.exception.attempts), (status, 1))
                self.assertIn("permanent failure", str(caught.exception))
                self.assertEqual(opener.call_count, 1)
                self.assertEqual(self.clock.sleeps, [])

    def test_permanent_dns_certificates_and_unknown_os_errors_do_not_retry(self):
        for error in (URLError(socket.gaierror(socket.EAI_FAIL, "nonrecoverable DNS failure")),
                      URLError(ssl.SSLCertVerificationError(1, "bad certificate")),
                      OSError(errno.EACCES, "denied"), ValueError("invalid opener configuration")):
            with self.subTest(error=type(error).__name__):
                opener = Mock(side_effect=error)
                with self.assertRaises(tft_update.FetchError) as caught:
                    self.get(opener)
                self.assertIn("permanent failure", str(caught.exception))
                self.assertEqual(opener.call_count, 1)

    def test_attempt_limit_preserves_failure_context(self):
        opener = Mock(side_effect=URLError(ConnectionResetError(errno.ECONNRESET, "peer reset")))
        with self.assertRaises(URLError) as caught:
            self.get(opener, max_attempts=3)
        self.assertEqual(opener.call_count, 3)
        self.assertEqual(self.clock.sleeps, [1.0, 2.0])
        self.assertEqual(caught.exception.attempts, 3)
        self.assertIn(self.URL, str(caught.exception))
        self.assertIn("attempt 3/3", str(caught.exception))
        self.assertIn("ConnectionResetError", str(caught.exception))

    def test_remaining_total_budget_reduces_later_attempt_timeout(self):
        timeouts = []

        def fail(request, *, timeout):
            timeouts.append(timeout)
            self.clock.now += timeout
            raise TimeoutError("request timeout")

        with self.assertRaises(tft_update.FetchError) as caught:
            self.get(fail, max_attempts=4, total_timeout=5, attempt_timeout=4, backoff=0.25)
        self.assertEqual(timeouts, [4, 0.75])
        self.assertEqual(self.clock.sleeps, [0.25])
        self.assertEqual(caught.exception.elapsed, 5)
        self.assertIn("total retry deadline exhausted", str(caught.exception))

    def test_does_not_accept_success_that_arrives_after_total_deadline(self):
        def late(request, *, timeout):
            self.clock.now += 6
            return Reply()

        with self.assertRaises(tft_update.FetchError) as caught:
            self.get(late, total_timeout=5)
        self.assertEqual(caught.exception.attempts, 1)
        self.assertIn("deadline", str(caught.exception))

    def test_watchdog_stops_waiting_for_blocked_opener_without_real_sleep(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        reply = Reply()

        class ClosingReply(Reply):
            def __exit__(self, *unused):
                reply.closed = True
                finished.set()

        def blocked(request, *, timeout):
            entered.set()
            release.wait()
            return ClosingReply()

        def deadline(*args, **kwargs):
            self.assertTrue(entered.wait(timeout=1))
            raise queue.Empty

        try:
            with patch.object(tft_update.queue.Queue, "get", side_effect=deadline):
                with self.assertRaises(tft_update.FetchError) as caught:
                    self.get(blocked, max_attempts=1)
            self.assertIn("TimeoutError", str(caught.exception))
            self.assertFalse(reply.closed)
        finally:
            release.set()
            self.assertTrue(finished.wait(timeout=1))
        self.assertTrue(reply.closed)

    def test_retry_after_delta_seconds_overrides_shorter_backoff(self):
        opener = Mock(side_effect=[HTTPError(self.URL, 429, "rate limit", {"retry-after": "7"}, None), Reply()])
        self.assertEqual(self.get(opener).attempts, 2)
        self.assertEqual(self.clock.sleeps, [7])

    def test_retry_after_http_date(self):
        date = formatdate(1700000010, usegmt=True)
        opener = Mock(side_effect=[HTTPError(self.URL, 503, "retry later", {"Retry-After": date}, None), Reply()])
        self.assertEqual(self.get(opener).attempts, 2)
        self.assertEqual(self.clock.sleeps, [10])

    def test_invalid_or_past_retry_after_falls_back_to_backoff(self):
        for value in ("invalid", "-3", "1.5", formatdate(1699999900, usegmt=True)):
            with self.subTest(value=value):
                before = len(self.clock.sleeps)
                opener = Mock(side_effect=[HTTPError(self.URL, 503, "retry", {"Retry-After": value}, None), Reply()])
                self.assertEqual(self.get(opener).attempts, 2)
                self.assertEqual(self.clock.sleeps[before:], [1])

    def test_does_not_retry_earlier_than_retry_after_when_bound_is_too_short(self):
        for value, options in (("20", {}), ("7", {"total_timeout": 5}), ("9" * 100, {})):
            with self.subTest(value=value):
                opener = Mock(side_effect=HTTPError(self.URL, 429, "retry", {"Retry-After": value}, None))
                with self.assertRaises(HTTPError) as caught:
                    self.get(opener, **options)
                self.assertEqual(caught.exception.code, 429)
                self.assertEqual(opener.call_count, 1)
                self.assertEqual(self.clock.sleeps, [])

    def test_terminal_errors_redact_url_credentials_query_fragment_and_reason(self):
        url = "https://user:password@example.invalid/source.json?token=secret#private"
        failures = (HTTPError(url, 404, "secret password token=secret", {}, None),
                    URLError(ConnectionResetError("secret password " + url)))
        for error in failures:
            with self.subTest(error=type(error).__name__):
                try:
                    tft_update.get(url, opener=Mock(side_effect=error), max_attempts=1,
                                 monotonic=self.clock.monotonic, sleep=self.clock.sleep)
                except (HTTPError, tft_update.FetchError) as terminal:
                    rendered = "".join(traceback.format_exception(terminal))
                    self.assertIn("https://example.invalid/source.json", str(terminal))
                    self.assertEqual(terminal.source_url, "https://example.invalid/source.json")
                    for secret in ("user:", "password", "token=", "secret", "#private"):
                        self.assertNotIn(secret, str(terminal))
                    # Traceback source lines may contain the local variable name
                    # but never the original exception reason or credential URL.
                    self.assertNotIn(url, rendered)
                    self.assertNotIn("secret password", rendered)
                else:
                    self.fail("the injected failure must remain visible")

    def test_json_parse_and_schema_errors_stay_outside_retry_boundary(self):
        for body in (b"{invalid", b'{"wrong": "schema"}'):
            opener = Mock(return_value=Reply(body))
            raw = self.get(opener).body
            with self.assertRaises((json.JSONDecodeError, KeyError)):
                json.loads(raw)["required"]
            self.assertEqual(opener.call_count, 1)
            self.assertEqual(self.clock.sleeps, [])

    def test_injected_default_opener_patch_point(self):
        with patch.object(tft_update.urllib.request, "urlopen", return_value=Reply()) as opener:
            self.assertEqual(tft_update.get(self.URL).body, b"ok")
        self.assertEqual(opener.call_count, 1)

    def test_transient_classification_survives_terminal_error_wrapping(self):
        for error, retryable in ((URLError(socket.gaierror(socket.EAI_NONAME, "DNS")), True),
                                 (HTTPError(self.URL, 503, "unavailable", {}, None), True),
                                 (HTTPError(self.URL, 404, "missing", {}, None), False),
                                 (ssl.SSLCertVerificationError(1, "certificate"), False)):
            with self.subTest(error=type(error).__name__, retryable=retryable):
                self.assertEqual(tft_update.is_transient(error), retryable)
                with self.assertRaises(URLError) as caught:
                    self.get(Mock(side_effect=error), max_attempts=1)
                self.assertEqual(tft_update.is_transient(caught.exception), retryable)

    def test_retry_callback_has_sanitized_context_and_excludes_permanent_errors(self):
        url = "https://user:password@example.invalid/source?token=secret#private"
        callback = Mock()
        opener = Mock(side_effect=[HTTPError(url, 503, "secret", {"Retry-After": "2"}, None), Reply()])
        tft_update.get(url, opener=opener, monotonic=self.clock.monotonic,
                     sleep=self.clock.sleep, on_retry=callback)
        callback.assert_called_once_with({"url": "https://example.invalid/source", "attempt": 1,
            "nextAttempt": 2, "maxAttempts": 4, "delay": 2, "elapsed": 0,
            "errorType": "HTTPError", "status": 503})
        callback.reset_mock()
        with self.assertRaises(HTTPError):
            self.get(Mock(side_effect=HTTPError(self.URL, 404, "missing", {}, None)), on_retry=callback)
        callback.assert_not_called()

    def test_late_timed_out_attempt_cannot_change_returned_result_or_retry_request(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        requests = []
        original_get = queue.Queue.get
        waits = 0

        class LateReply(Reply):
            def __exit__(self, *unused):
                super().__exit__(*unused)
                finished.set()

        def opener(request, *, timeout):
            requests.append(request)
            if len(requests) == 1:
                entered.set()
                release.wait()
                request.add_header("X-Late", "late mutation")
                return LateReply(b"late")
            return Reply(b"winner", {"X-Source": "winner"})

        def wait(outcome, *args, **kwargs):
            nonlocal waits
            waits += 1
            if waits == 1:
                self.assertTrue(entered.wait(timeout=1))
                raise queue.Empty
            return original_get(outcome, *args, **kwargs)

        try:
            with patch.object(tft_update.queue.Queue, "get", new=wait):
                result = self.get(opener, max_attempts=2)
            self.assertEqual(result.body, b"winner")
        finally:
            release.set()
            self.assertTrue(finished.wait(timeout=1))
        self.assertEqual((result.body, result.headers["X-Source"]), (b"winner", "winner"))
        self.assertIsNot(requests[0], requests[1])
        self.assertIsNone(requests[1].get_header("X-late"))

    def test_blocked_workers_are_bounded_across_multiple_get_calls(self):
        release = threading.Event()
        entered = [threading.Event(), threading.Event()]
        finished = [threading.Event(), threading.Event()]
        calls = []
        waits = 0

        class NonblockingSlots:
            def __init__(self):
                self.slots = threading.BoundedSemaphore(2)

            def acquire(self, *, timeout):
                return self.slots.acquire(blocking=False)

            def release(self):
                self.slots.release()

        class ClosingReply(Reply):
            def __init__(self, index):
                super().__init__()
                self.index = index

            def __exit__(self, *unused):
                finished[self.index].set()

        def blocked(request, *, timeout):
            index = len(calls)
            calls.append(request)
            entered[index].set()
            release.wait()
            return ClosingReply(index)

        def deadline(*args, **kwargs):
            nonlocal waits
            self.assertTrue(entered[waits].wait(timeout=1))
            waits += 1
            raise queue.Empty

        try:
            with patch.object(tft_update, "_worker_slots", NonblockingSlots()), \
                 patch.object(tft_update.queue.Queue, "get", side_effect=deadline):
                for _ in range(5):
                    with self.assertRaises(tft_update.FetchError):
                        self.get(blocked, max_attempts=1)
                self.assertEqual(len(calls), 2)
        finally:
            release.set()
            self.assertTrue(all(event.wait(timeout=1) for event in finished))

    def test_invalid_budgets_fail_before_opening(self):
        invalid = [{"max_attempts": value} for value in (0, 11, True, 1.5)]
        invalid += [{key: value} for key in ("total_timeout", "attempt_timeout", "backoff", "max_retry_after")
                    for value in (-1, float("inf"), float("nan"), True)]
        invalid += [{"total_timeout": 0}, {"attempt_timeout": 0}]
        for options in invalid:
            with self.subTest(options=options):
                opener = Mock()
                with self.assertRaises(ValueError):
                    self.get(opener, **options)
                opener.assert_not_called()


class TestTftFetchIntegration(unittest.TestCase):
    def test_fetch_bytes_keeps_urlopen_patchpoint_and_reports_sanitized_recovery(self):
        url = "https://user:password@example.invalid/source?token=secret#private"
        progress = Mock()
        token = tft._FETCH_PROGRESS.set(progress)
        try:
            with patch.object(tft.urllib.request, "urlopen", side_effect=[
                    URLError(socket.gaierror(socket.EAI_NONAME, "temporary name lookup")), Reply(b"original bytes")]) as opener, \
                 patch.object(tft_update.time, "sleep") as sleep, patch("builtins.print") as logged:
                self.assertEqual(tft.fetch_bytes(url), b"original bytes")
        finally:
            tft._FETCH_PROGRESS.reset(token)
        self.assertEqual(opener.call_count, 2)
        sleep.assert_called_once_with(1.0)
        self.assertEqual(progress.call_args.kwargs["transport"]["state"], "recovered")
        self.assertEqual(progress.call_args.kwargs["transport"]["attempts"], 2)
        text = " ".join(str(call) for call in logged.call_args_list + progress.call_args_list)
        self.assertIn("https://example.invalid/source", text)
        for secret in ("user:", "password", "token=secret", "#private"):
            self.assertNotIn(secret, text)

    def test_fetch_source_preserves_exact_raw_digest_and_last_modified(self):
        raw = b'{  "set": 18, "values": [1, 2] }\n'
        reply = Reply(raw, {"last-modified": "Thu, 10 Sep 2026 12:00:00 GMT"})
        with patch.object(tft.urllib.request, "urlopen", return_value=reply) as opener:
            value, source = tft.fetch_source("https://example.invalid/source")
        self.assertEqual(value, {"set": 18, "values": [1, 2]})
        self.assertEqual(source, {"url": "https://example.invalid/source",
            "lastModified": "Thu, 10 Sep 2026 12:00:00 GMT", "sha256": hashlib.sha256(raw).hexdigest()})
        self.assertEqual(opener.call_count, 1)

    def test_fetch_source_json_failure_is_not_retried(self):
        with patch.object(tft.urllib.request, "urlopen", return_value=Reply(b"not JSON")) as opener, \
             patch.object(tft_update.time, "sleep") as sleep:
            with self.assertRaises(json.JSONDecodeError):
                tft.fetch_source("https://example.invalid/source")
        self.assertEqual(opener.call_count, 1)
        sleep.assert_not_called()


def change(what, old, new, update="SEPTEMBER 3RD", section="CHAMPIONS"):
    return {"what": what, "old": old, "new": new, "update": update,
            "section": section, "major": "Mid-Patch Updates"}


def entry(c):
    return {"text": f"{c['what']}: {c['old']} ⇒ {c['new']}", "parent": "",
            **{k: c[k] for k in ("update", "section", "major")}}


def unit(name, cost=4):
    return {"apiName": "TFT18_" + name.replace(" ", ""), "name": name,
            "cost": cost, "shopUnit": True, "role": "caster", "traits": [],
            "traitApiNames": [], "assetNames": [], "curveTable": {},
            "stats": {"hp": 850, "damage": 40, "attackSpeed": .75, "armor": 40,
                      "magicResist": 40, "initialMana": 0, "mana": 30, "range": 4},
            "ability": {"name": "Spell", "desc": "A spell.", "attributeCalcs": {}}}


class TestReconcile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        amumu, yi, soraka = unit("Amumu"), unit("Master Yi", 3), unit("Soraka")
        amumu["stats"].update(initialMana=30, mana=140)
        amumu["curveTable"]["PassiveHealPercent"] = [[1, .022], [2, .022], [3, .04], [4, .04]]
        yi["stats"].update(armor=60, magicResist=60)
        yi["extraAbilities"] = {"AP": {"variant": "AP", "stats": deepcopy(yi["stats"]),
                                        "curveTable": {}, "attributeCalcs": {}}}
        soraka["curveTable"] = {"DamageAP": [[1, 190], [2, 285], [3, 1000], [4, 2200]],
                                "Shield": [[1, 200], [2, 300], [3, 450], [4, 800]]}
        soraka["ability"]["attributeCalcs"]["MagicDamageCalc1"] = {
            "resolved": True, "terms": [{"type": "scaled", "op": "add", "scaling": "AbilityPower",
                                            "row": "DamageAP", "coefficient": [190, 285, 1000, 2200]}],
            "values": [190, 285, 1000, 2200]}
        self.raw = {"_metadata": {"set": "TFTSet18", "patch": "pbe", "generated": "old"},
                    "units": [amumu, yi, soraka], "traits": [], "items": [],
                    "roles": {"caster": "Magic Caster"},
                    "roleData": {"caster": {"name": "Magic Caster", "roleTags": ["Role.Magic", "Role.Caster"]}}}
        self.overrides = {"units": {
            "TFT18_Amumu": {"stats": {"mana": 125}, "curve": {"PassiveHealPercent": [.025, .025]}},
            "TFT18_MasterYi": {"stats": {"armor": 55, "mr": 55}, "forms": {"AP": {"stats": {"armor": 55, "mr": 55}}}},
            "TFT18_Soraka": {"curve": {"DamageAP": [225, 335]}}}, "items": {}, "traits": {}}
        old_changes = [change("Amumu Heal Max HP %", "2.2%", "2.5%", "AUGUST 31ST"),
                       change("Master Yi Resists", "60", "55", "AUGUST 31ST"),
                       change("Soraka Initial Star Damage", "190/285 AP", "225/335 AP", "AUGUST 31ST")]
        self.notes = {"patch": "18.1d", "basePatch": "18.1", "url": tft.PATCH_NOTES_URL.format(slug="18-1"),
                      "updates": ["AUGUST 31ST"], "changes": old_changes,
                      "notes": [entry(c) for c in old_changes] + [{"text": "Previously reviewed combat mechanics.", "section": "BUG FIXES"}]}
        checks = []
        for target, expected, c in [
            ({"kind": "unit", "api": "TFT18_Amumu", "row": "PassiveHealPercent", "stars": [1, 2]}, [.025, .025], old_changes[0]),
            ({"kind": "unit", "api": "TFT18_Soraka", "row": "DamageAP", "stars": [1, 2]}, [225, 335], old_changes[2]),
        ]:
            checks.append({"id": str(len(checks)), "what": c["what"], "target": target, "expected": expected, "patchLine": c,
                           "source": {"url": self.notes["url"]}})
        for form in [None, "AP"]:
            for stat in ["armor", "mr"]:
                target = {"kind": "unit", "api": "TFT18_MasterYi", "stat": stat}
                if form:
                    target["form"] = form
                checks.append({"id": str(len(checks)), "what": "Master Yi Resists", "target": target,
                               "expected": [55], "patchLine": old_changes[1], "source": {"url": self.notes["url"]}})
        self.audit = {"patch": "18.1d", "checks": checks, "unresolved": [{"id": "existing-limitation"}],
                      "lookupHash": tft.json_hash(self.raw), "binsHash": tft.json_hash({}), "patchNotesHash": tft.json_hash(self.notes)}
        self.previous = self.snapshot("previous", self.raw, self.notes, self.overrides, self.audit)

    def snapshot(self, name, raw, notes, overrides, audit, bins=None):
        directory = Path(self.tmp.name) / name
        directory.mkdir(exist_ok=True)
        values = {"metatft.json": raw, "meta.json": {"set": 18, "patch": notes["patch"]},
                  "patchnotes.json": notes, "overrides.json": overrides, "audit.json": audit,
                  "bins.json": bins or {}}
        for filename, data in values.items():
            (directory / filename).write_text(json.dumps(data))
        return tft.Snapshot(18, notes["patch"], directory=directory)

    def candidate(self, changes=(), raw=None, patch="18.1e", bullets=(), bins=None, next_minor=False):
        notes = deepcopy(self.notes)
        notes["patch"] = patch
        notes["basePatch"] = patch.rstrip("abcdefghijklmnopqrstuvwxyz")
        notes["url"] = tft.PATCH_NOTES_URL.format(slug=notes["basePatch"].replace(".", "-"))
        notes["changes"] = list(changes) + ([] if next_minor else notes["changes"])
        notes["notes"] = [entry(c) for c in changes] + list(bullets) + ([] if next_minor else notes["notes"])
        notes["updates"] = list(dict.fromkeys([c["update"] for c in changes] + notes["updates"]))
        result = self.snapshot("candidate", raw or self.raw, notes, self.overrides, self.audit, bins)
        return result, notes

    def publishable(self, candidate, notes):
        overrides, audit = reconcile(candidate, self.previous, notes)
        checked = self.snapshot("checked", candidate.raw, notes, overrides, audit, candidate.bins)
        findings, _ = tft.check_audit(checked, notes)
        self.assertTrue(all(f["status"] == "current" for f in findings), findings)
        return checked, audit

    def test_unchanged_feeds_and_input_objects_are_preserved(self):
        before = deepcopy(self.previous.__dict__)
        a, b = reconcile(self.previous, self.previous, self.notes)
        self.assertEqual(a, self.overrides)
        self.assertEqual(b["checks"], self.audit["checks"])
        self.assertEqual(b["unresolved"], self.audit["unresolved"])
        self.assertEqual(before, self.previous.__dict__)
        self.assertEqual((a, b), reconcile(self.previous, self.previous, self.notes))

    def test_metadata_only_changes_rebind_exact_hash(self):
        raw = deepcopy(self.raw)
        raw["_metadata"].update(generated="fresh", coreHash="new")
        candidate, notes = self.candidate(raw=raw, patch="18.1d")
        _, audit = self.publishable(candidate, notes)
        self.assertEqual(audit["lookupHash"], tft.json_hash(raw))
        self.assertNotEqual(audit["lookupHash"], self.audit["lookupHash"])

    def test_known_heal_percentage_and_all_master_yi_forms(self):
        candidate, notes = self.candidate([change("Amumu Heal Max HP %", "2.5%", "3%"), change("Master Yi Resists", "55", "50")])
        snap, audit = self.publishable(candidate, notes)
        self.assertEqual([tft.curve_at(snap.units["TFT18_Amumu"]["curve"]["PassiveHealPercent"], s) for s in (1, 2, 3, 4)], [.03, .03, .04, .04])
        yi = snap.units["TFT18_MasterYi"]
        self.assertEqual([yi["stats"]["armor"], yi["stats"]["mr"], yi["forms"]["AP"]["stats"]["armor"], yi["forms"]["AP"]["stats"]["mr"]], [50] * 4)
        self.assertEqual(len(audit["automatic"]["appliedChanges"]), 2)

    def test_same_patch_revision_and_next_minor_same_set(self):
        for patch in ["18.1d", "18.2"]:
            with self.subTest(patch=patch):
                candidate, notes = self.candidate([change("Amumu Heal Max HP %", "2.5%", "2.75%")], patch=patch, next_minor=patch == "18.2")
                snap, audit = self.publishable(candidate, notes)
                self.assertAlmostEqual(tft.curve_at(snap.units["TFT18_Amumu"]["curve"]["PassiveHealPercent"], 1), .0275)
                self.assertEqual(audit["patchNotesHash"], tft.json_hash(notes))

    def test_cumulative_hotfixes_are_applied_oldest_first(self):
        newest = change("Amumu Heal Max HP %", "3%", "3.5%", "SEPTEMBER 4TH")
        older = change("Amumu Heal Max HP %", "2.5%", "3%", "SEPTEMBER 3RD")
        candidate, notes = self.candidate([newest, older])
        snap, _ = self.publishable(candidate, notes)
        self.assertAlmostEqual(tft.curve_at(snap.units["TFT18_Amumu"]["curve"]["PassiveHealPercent"], 1), .035)

    def test_mana_pair_and_extended_star_array(self):
        candidate, notes = self.candidate([change("Amumu Mana", "30/125", "20/110"), change("Soraka Initial Star Damage", "225/335/1000 AP", "250/375/1100 AP")])
        snap, _ = self.publishable(candidate, notes)
        self.assertEqual((snap.units["TFT18_Amumu"]["stats"]["initialMana"], snap.units["TFT18_Amumu"]["stats"]["mana"]), (20, 110))
        self.assertEqual([tft.curve_at(snap.units["TFT18_Soraka"]["curve"]["DamageAP"], s) for s in range(1, 5)], [250, 375, 1100, 2200])

    def test_unambiguous_new_base_stat_and_ability_row(self):
        candidate, notes = self.candidate([change("Soraka Health", "850", "900"), change("Soraka Ability Shield", "200/300/450", "240/360/540")])
        snap, _ = self.publishable(candidate, notes)
        self.assertEqual(snap.units["TFT18_Soraka"]["stats"]["hp"], 900)
        self.assertEqual(tft.curve_at(snap.units["TFT18_Soraka"]["curve"]["Shield"], 2), 360)

    def test_source_catching_up_with_verified_correction(self):
        raw = deepcopy(self.raw)
        raw["units"][0]["curveTable"]["PassiveHealPercent"] = [[1, .025], [2, .025], [3, .04], [4, .04]]
        candidate, notes = self.candidate(raw=raw, patch="18.1d")
        self.publishable(candidate, notes)

    def test_bad_lookup_value_cannot_hide_behind_override(self):
        raw = deepcopy(self.raw)
        raw["units"][0]["curveTable"]["PassiveHealPercent"][0][1] = .9
        candidate, notes = self.candidate(raw=raw)
        with self.assertRaisesRegex(ReviewRequired, "unexplained lookup"):
            reconcile(candidate, self.previous, notes)

    def test_unrelated_numeric_change_is_not_covered_by_hotfix(self):
        raw = deepcopy(self.raw)
        raw["units"][2]["stats"]["hp"] = 999
        candidate, notes = self.candidate([change("Amumu Heal Max HP %", "2.5%", "3%")], raw=raw)
        with self.assertRaisesRegex(ReviewRequired, "base-stat change"):
            reconcile(candidate, self.previous, notes)

    def test_unknown_mechanics_bullet_blocks_even_when_numeric_checks_pass(self):
        candidate, notes = self.candidate(bullets=[{"text": "Soraka now applies a stacking burn on every cast.", "section": "BUG FIXES"}])
        with self.assertRaisesRegex(ReviewRequired, "mechanics bullet"):
            reconcile(candidate, self.previous, notes)

    def test_numeric_line_with_new_mechanics_is_not_treated_as_just_damage(self):
        candidate, notes = self.candidate([change("Soraka Initial Star Damage", "225/335 AP", "250/375 AP and now burns enemies")])
        with self.assertRaisesRegex(ReviewRequired, "unsupported numeric"):
            reconcile(candidate, self.previous, notes)

    def test_scaling_type_cannot_change_under_a_known_numeric_label(self):
        candidate, notes = self.candidate([change('Soraka Initial Star Damage', '225/335 AP', '250/375 AD')])
        with self.assertRaisesRegex(ReviewRequired, 'units/scaling changed'):
            reconcile(candidate, self.previous, notes)

    def test_caught_up_base_stats_retire_redundant_corrections(self):
        raw = deepcopy(self.raw)
        yi = raw['units'][1]
        yi['stats'].update(armor=55, magicResist=55)
        yi['extraAbilities']['AP']['stats'].update(armor=55, magicResist=55)
        candidate, notes = self.candidate(raw=raw, patch='18.1d')
        snap, audit = self.publishable(candidate, notes)
        self.assertEqual(snap.overrides['units']['TFT18_MasterYi']['stats'], {})
        self.assertEqual(snap.overrides['units']['TFT18_MasterYi']['forms']['AP']['stats'], {})
        self.assertEqual(len(audit['automatic']['retiredStatCorrections']), 4)

    def test_ambiguous_ability_rows_require_review(self):
        raw = deepcopy(self.raw)
        raw["units"][2]["curveTable"]["OtherShield"] = deepcopy(raw["units"][2]["curveTable"]["Shield"])
        self.audit["lookupHash"] = tft.json_hash(raw)
        self.previous = self.snapshot("previous", raw, self.notes, self.overrides, self.audit)
        candidate, notes = self.candidate([change("Soraka Ability Shield", "200/300/450", "220/330/500")], raw=raw)
        with self.assertRaisesRegex(ReviewRequired, "multiple curve rows"):
            reconcile(candidate, self.previous, notes)

    def test_unique_numbers_cannot_bind_healing_to_a_shield_row(self):
        candidate, notes = self.candidate([change('Soraka Ability Heal', '200/300/450', '240/360/540')])
        with self.assertRaisesRegex(ReviewRequired, 'zero or multiple curve rows'):
            reconcile(candidate, self.previous, notes)

    def test_novel_ability_label_cannot_add_arbitrary_mechanics_qualifiers(self):
        candidate, notes = self.candidate([change('Soraka Ability Shield On Every Attack', '200/300/450', '240/360/540')])
        with self.assertRaisesRegex(ReviewRequired, 'explicit audit mapping'):
            reconcile(candidate, self.previous, notes)

    def test_unique_auto_attack_and_control_rows_are_not_ability_amounts(self):
        for row, label in [('AutoAttackDamage', 'Ability Damage'), ('BasicAttackDamage', 'Ability Damage'),
                           ('ShieldDuration', 'Ability Shield'), ('ShieldCount', 'Ability Shield'),
                           ('DamageRadius', 'Ability Damage'), ('HealTickRate', 'Ability Heal')]:
            with self.subTest(row=row):
                raw = deepcopy(self.raw)
                raw['units'][2]['curveTable'][row] = [[1, 11], [2, 22], [3, 33], [4, 44]]
                self.audit['lookupHash'] = tft.json_hash(raw)
                self.previous = self.snapshot('previous', raw, self.notes, self.overrides, self.audit)
                candidate, notes = self.candidate([change('Soraka ' + label, '11/22/33', '12/24/36')], raw=raw)
                with self.assertRaisesRegex(ReviewRequired, 'zero or multiple curve rows'):
                    reconcile(candidate, self.previous, notes)

    def test_changed_mechanics_decimal_and_negative_sign_are_not_normalized_away(self):
        reviewed = {'text': 'Champions now heal 1.5% of their Health.', 'section': 'BUG FIXES'}
        self.notes['notes'].append(reviewed)
        self.audit['patchNotesHash'] = tft.json_hash(self.notes)
        self.previous = self.snapshot('previous', self.raw, self.notes, self.overrides, self.audit)
        for text in ['Champions now heal 15% of their Health.', 'Champions now heal -1.5% of their Health.']:
            with self.subTest(text=text):
                candidate, notes = self.candidate(bullets=[{**reviewed, 'text': text}])
                with self.assertRaisesRegex(ReviewRequired, 'mechanics bullet'):
                    reconcile(candidate, self.previous, notes)

    def test_successive_reviews_keep_provenance_without_growing_on_unchanged_feeds(self):
        candidate, notes = self.candidate([change('Amumu Heal Max HP %', '2.5%', '3%')])
        first, first_audit = self.publishable(candidate, notes)
        overrides, audit = reconcile(first, first, notes)
        self.assertEqual(audit, first_audit)
        second_notes = deepcopy(notes)
        second_notes['patch'] = '18.1f'
        second_notes['updates'].insert(0, 'SEPTEMBER 4TH')
        c = change('Amumu Heal Max HP %', '3%', '3.5%', 'SEPTEMBER 4TH')
        second_notes['changes'].insert(0, c)
        second_notes['notes'].insert(0, entry(c))
        second = self.snapshot('second', first.raw, second_notes, overrides, audit)
        _, next_audit = reconcile(second, first, second_notes)
        self.assertEqual(next_audit['automaticHistory'][-1], first_audit['automatic'])

    def test_formula_changes_require_review(self):
        raw = deepcopy(self.raw)
        raw["units"][2]["ability"]["attributeCalcs"]["MagicDamageCalc1"]["terms"][0]["op"] = "multiply"
        candidate, notes = self.candidate(raw=raw)
        with self.assertRaisesRegex(ReviewRequired, "formula"):
            reconcile(candidate, self.previous, notes)

    def test_timing_changes_require_review(self):
        candidate, notes = self.candidate(bins={"unit": {"castTime": .5}})
        with self.assertRaisesRegex(ReviewRequired, "timing/bin"):
            reconcile(candidate, self.previous, notes)

    def test_new_champions_require_review(self):
        raw = deepcopy(self.raw)
        raw["units"].append(unit("Brand"))
        candidate, notes = self.candidate(raw=raw)
        with self.assertRaisesRegex(ReviewRequired, "new or removed unit"):
            reconcile(candidate, self.previous, notes)

    def test_wrong_source_and_incomplete_document_require_review(self):
        candidate, notes = self.candidate()
        for bad in [{**notes, "url": "https://example.com/patch"}, {**notes, "notes": []}]:
            with self.subTest(bad=bad["url"]):
                with self.assertRaises(ReviewRequired):
                    reconcile(candidate, self.previous, bad)
        self.previous.audit["lookupHash"] = "unbound"
        with self.assertRaisesRegex(ReviewRequired, "previous audit lookupHash"):
            reconcile(candidate, self.previous, notes)

    def test_cosmetic_bullet_is_recorded_outside_scope(self):
        candidate, notes = self.candidate(bullets=[{"text": "Fixed champion icons on the loading screen.", "section": "BUG FIXES"}])
        _, audit = self.publishable(candidate, notes)
        self.assertTrue(audit["automatic"]["outOfScope"])

    def test_raw_numeric_update_with_consistent_calculation_cache(self):
        raw = deepcopy(self.raw)
        row = raw['units'][2]['curveTable']['DamageAP']
        row[0][1], row[1][1] = 250, 375
        calc = raw['units'][2]['ability']['attributeCalcs']['MagicDamageCalc1']
        calc['terms'][0]['coefficient'][:2] = [250, 375]
        calc['values'][:2] = [250, 375]
        candidate, notes = self.candidate([change('Soraka Initial Star Damage', '225/335 AP', '250/375 AP')], raw=raw)
        self.publishable(candidate, notes)

    def test_wrong_cached_output_cannot_hide_behind_valid_row_update(self):
        raw = deepcopy(self.raw)
        raw['units'][2]['curveTable']['DamageAP'][:2] = [[1, 250], [2, 375]]
        calc = raw['units'][2]['ability']['attributeCalcs']['MagicDamageCalc1']
        calc['terms'][0]['coefficient'][:2] = [250, 375]
        calc['values'][:2] = [250, 999]
        candidate, notes = self.candidate([change('Soraka Initial Star Damage', '225/335 AP', '250/375 AP')], raw=raw)
        with self.assertRaisesRegex(ReviewRequired, 'calculation cache'):
            reconcile(candidate, self.previous, notes)

    def test_repeated_historical_change_in_a_new_hotfix_is_not_skipped(self):
        old = change('Amumu Heal Max HP %', '2.5%', '3%', 'AUGUST 29TH')
        revert = change('Amumu Heal Max HP %', '3%', '2.5%', 'AUGUST 30TH')
        self.notes['changes'].extend([revert, old])
        self.notes['notes'].extend([entry(revert), entry(old)])
        self.notes['updates'].extend(['AUGUST 30TH', 'AUGUST 29TH'])
        self.audit['patchNotesHash'] = tft.json_hash(self.notes)
        self.previous = self.snapshot('previous', self.raw, self.notes, self.overrides, self.audit)
        candidate, notes = self.candidate([change('Amumu Heal Max HP %', '2.5%', '3%', 'SEPTEMBER 3RD')])
        snap, _ = self.publishable(candidate, notes)
        self.assertEqual(tft.curve_at(snap.units['TFT18_Amumu']['curve']['PassiveHealPercent'], 1), .03)

    def test_riftbeast_corrected_dependent_breakpoint_requires_review(self):
        line = change('Riftbeast (7) Stats', '6% AD/AP/AS', '5% AD/AP/AS', 'AUGUST 31ST', 'TRAITS')
        self.raw['traits'].append({'apiName': 'DA_Riftbeast18', 'name': 'Riftbeast',
                                  'effects': [{'minUnits': n, 'style': style} for n, style in [(3, 1), (5, 3), (7, 4), (10, 4)]],
                                  'curveTable': {r: [[0, 0], [3, v], [4, v]] for r, v in [('CapstoneAD', .06), ('CapstoneAP', .06), ('CapstoneASPD', 1.06)]}})
        self.overrides['traits']['DA_Riftbeast18'] = {'curve': {r: {'3': v, '4': v} for r, v in [('CapstoneAD', .05), ('CapstoneAP', .05), ('CapstoneASPD', 1.05)]}}
        for row, value in [('CapstoneAD', .05), ('CapstoneAP', .05), ('CapstoneASPD', 1.05)]:
            self.audit['checks'].append({'what': line['what'], 'target': {'kind': 'trait', 'api': 'DA_Riftbeast18', 'row': row, 'columns': [3]},
                                         'expected': [value], 'patchLine': line})
        self.notes['changes'].append(line)
        self.notes['notes'].append(entry(line))
        self.audit.update(lookupHash=tft.json_hash(self.raw), patchNotesHash=tft.json_hash(self.notes))
        self.previous = self.snapshot('previous', self.raw, self.notes, self.overrides, self.audit)
        candidate, notes = self.candidate([change('Riftbeast (7) Stats', '5% AD/AP/AS', '4% AD/AP/AS', section='TRAITS')])
        with self.assertRaisesRegex(ReviewRequired, 'corrected dependent columns'):
            reconcile(candidate, self.previous, notes)

    def test_wrong_set_and_malformed_lookup_fail_closed(self):
        candidate, notes = self.candidate()
        candidate.raw['_metadata']['set'] = 'TFTSet19'
        with self.assertRaisesRegex(ReviewRequired, 'wrong TFT set'):
            reconcile(candidate, self.previous, notes)
        candidate.raw['_metadata']['set'] = 'TFTSet18'
        candidate.raw['units'] = None
        with self.assertRaisesRegex(ReviewRequired, 'malformed'):
            reconcile(candidate, self.previous, notes)

    def test_excluded_item_definition_change_retires_old_checks(self):
        api = 'DA_Artifact_Test'
        self.raw['items'].append({'apiName': api, 'name': 'Test Artifact', 'curveTable': {'Health': [[1, 300]]}})
        self.overrides['items'][api] = {'curve': {'Health': 400}}
        self.audit['checks'].append({'what': 'Test Artifact Health', 'target': {'kind': 'item', 'api': api, 'row': 'Health', 'columns': [1]},
                                     'expected': [400], 'patchLine': {'what': 'Test Artifact Health', 'old': '300', 'new': '400'}})
        self.audit['lookupHash'] = tft.json_hash(self.raw)
        self.previous = self.snapshot('previous', self.raw, self.notes, self.overrides, self.audit)
        raw = deepcopy(self.raw)
        raw['items'][0]['curveTable']['Health'][0][1] = 450
        candidate, notes = self.candidate(raw=raw)
        snap, audit = self.publishable(candidate, notes)
        self.assertNotIn(api, snap.overrides['items'])
        self.assertTrue(any(c['target']['api'] == api for c in audit['outOfScopeChecks']))

    def test_excluded_stage_array_is_recorded_without_constant_override(self):
        api = 'DA_Artifact_Test'
        self.raw['items'].append({'apiName': api, 'name': 'Test Artifact', 'curveTable': {'Damage': [[1, 30], [3, 60]]}})
        line = change('Test Artifact Damage', '20/20/40', '30/30/60', 'AUGUST 31ST', 'ARTIFACTS')
        for column, value in enumerate([30, 30, 60], 1):
            self.audit['checks'].append({'what': line['what'], 'target': {'kind': 'item', 'api': api, 'row': 'Damage', 'columns': [column]},
                                         'expected': [value], 'patchLine': line})
        self.audit['lookupHash'] = tft.json_hash(self.raw)
        self.previous = self.snapshot('previous', self.raw, self.notes, self.overrides, self.audit)
        candidate, notes = self.candidate([change('Test Artifact Damage', '30/30/60', '40/40/70', section='ARTIFACTS')])
        snap, audit = self.publishable(candidate, notes)
        self.assertNotIn(api, snap.overrides['items'])
        self.assertEqual(len([c for c in audit['outOfScopeChecks'] if c['target']['api'] == api]), 3)


fixtures = SimpleNamespace(TestReconcile=TestReconcile, change=change, entry=entry, unit=unit)


class Base(unittest.TestCase):
    setUp = fixtures.TestReconcile.setUp
    snapshot = fixtures.TestReconcile.snapshot
    candidate = fixtures.TestReconcile.candidate

    def manifest(self, candidate, notes, **records):
        return {'schema': 1, 'patch': candidate.patch, 'previousPatch': self.previous.patch, 'source': notes['url'],
                'bindings': {'lookupHash': tft.json_hash(candidate.raw), 'binsHash': tft.json_hash(candidate.bins),
                             'patchNotesHash': tft.json_hash(notes), 'previousAuditHash': tft.json_hash(self.previous.audit)},
                'mappings': [], 'dispositions': [], **records}

    def reviewed(self, candidate, notes, review):
        overrides, audit = tft_update.reconcile(candidate, self.previous, notes, review=review)
        checked = self.snapshot('checked', candidate.raw, notes, overrides, audit, candidate.bins)
        findings, _ = tft.check_audit(checked, notes)
        self.assertTrue(all(f['status'] == 'current' for f in findings), findings)
        return checked, audit

    def rebase(self, raw, overrides=None):
        """Make `raw` the previous lookup (the audit is bound to it)."""
        self.raw = raw
        self.overrides = overrides or self.overrides
        self.audit['lookupHash'] = tft.json_hash(raw)
        self.previous = self.snapshot('previous', raw, self.notes, self.overrides, self.audit)


class TestWrongRowGuard(Base):
    """Riot: "Nidalee AP Form: Nidalee Empowered Attack Damage: 285/425 AP ⇒ 300/450 AP"."""
    LINE = fixtures.change('Nidalee AP Form: Nidalee Empowered Attack Damage', '285/425 AP', '300/450 AP')

    def setUp(self):
        super().setUp()
        nidalee = fixtures.unit('Nidalee')
        nidalee['curveTable'] = {'EmpoweredDamage': [[1, 170], [2, 255], [3, 2000], [4, 3000]],
                                 'ThirdAttackEmpoweredDamage': [[1, 320], [2, 480], [3, 3000], [4, 5000]],
                                 'TrapApplyDamage': [[1, 50], [2, 75], [3, 100], [4, 100]]}
        raw = deepcopy(self.raw)
        raw['units'].append(nidalee)
        self.rebase(raw)

    def mapping(self, row, **extra):
        target = {'kind': 'unit', 'api': 'TFT18_Nidalee', 'row': row, 'stars': [1, 2]}
        return {'change': self.LINE, 'target': target, 'expected': [300, 450],
                'observedBefore': tft_update._values(self.previous, target),
                'numericEncoding': {'scale': 1, 'offset': 0}, 'reason': 'Reviewed.', **extra}

    def test_the_18_2_nidalee_mapping_is_refused_without_an_acknowledgement(self):
        candidate, notes = self.candidate([self.LINE])
        review = self.manifest(candidate, notes, mappings=[self.mapping('EmpoweredDamage')])
        with self.assertRaisesRegex(tft_update.ReviewRequired, r'EmpoweredDamage may be the wrong row \(ThirdAttackEmpoweredDamage held \[320, 480\]'):
            tft_update.reconcile(candidate, self.previous, notes, review=review)

    def test_the_corrected_mapping_passes_and_leaves_the_ordinary_javelins_alone(self):
        candidate, notes = self.candidate([self.LINE])
        checked, _ = self.reviewed(candidate, notes, self.manifest(candidate, notes, mappings=[self.mapping('ThirdAttackEmpoweredDamage')]))
        curve = checked.units['TFT18_Nidalee']['curve']
        self.assertEqual([tft.curve_at(curve['EmpoweredDamage'], s) for s in (1, 2, 3, 4)], [170, 255, 2000, 3000])
        self.assertEqual([tft.curve_at(curve['ThirdAttackEmpoweredDamage'], s) for s in (1, 2, 3, 4)], [300, 450, 3000, 5000])

    def test_an_acknowledgement_names_the_rows_it_rules_out_and_is_kept_in_the_audit(self):
        candidate, notes = self.candidate([self.LINE])
        ack = {'rows': ['ThirdAttackEmpoweredDamage'], 'reason': 'Deliberate: documented elsewhere.'}
        review = self.manifest(candidate, notes, mappings=[self.mapping('EmpoweredDamage', siblingRowAcknowledgement=ack)])
        _, audit = self.reviewed(candidate, notes, review)
        self.assertEqual(audit['reviews'][-1]['mappings'][0]['siblingRowAcknowledgement'], ack)
        for bad in ({'rows': ['TrapApplyDamage'], 'reason': 'Names the wrong sibling.'},
                    {'rows': ['ThirdAttackEmpoweredDamage'], 'reason': ' '}, {'rows': [], 'reason': 'x'}, 'yes'):
            with self.subTest(bad=bad), self.assertRaises(tft_update.ReviewRequired):
                review = self.manifest(candidate, notes, mappings=[self.mapping('EmpoweredDamage', siblingRowAcknowledgement=bad)])
                tft_update.reconcile(candidate, self.previous, notes, review=review)

    def test_an_acknowledgement_nothing_calls_for_is_rejected(self):
        candidate, notes = self.candidate([self.LINE])
        ack = {'rows': ['EmpoweredDamage'], 'reason': 'Not needed.'}
        review = self.manifest(candidate, notes, mappings=[self.mapping('ThirdAttackEmpoweredDamage', siblingRowAcknowledgement=ack)])
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'no sibling row fits the note better'):
            tft_update.reconcile(candidate, self.previous, notes, review=review)

    def test_a_stale_source_with_no_better_sibling_needs_no_acknowledgement(self):
        # Soraka's DamageAP holds 225/335 against a stated old 260/390 (14% off); Shield 200/300 is further.
        line = fixtures.change('Soraka Initial Star Damage', '260/390 AP', '280/420 AP')
        candidate, notes = self.candidate([line])
        target = {'kind': 'unit', 'api': 'TFT18_Soraka', 'row': 'DamageAP', 'stars': [1, 2]}
        mapping = {'change': line, 'target': target, 'expected': [280, 420], 'observedBefore': [225, 335],
                   'numericEncoding': {'scale': 1, 'offset': 0}, 'reason': 'Stale source, right row.'}
        self.reviewed(candidate, notes, self.manifest(candidate, notes, mappings=[mapping]))

    def test_upstream_moving_a_sibling_to_the_new_value_is_the_second_sign(self):
        # The mapped row matches the note's old value exactly, so the first sign is silent; but the
        # staged lookup already carries 300/450 on the sibling and left the mapped row alone.
        line = fixtures.change('Nidalee Ability Damage', '170/255 AP', '300/450 AP')
        raw = deepcopy(self.raw)
        raw['units'][-1]['curveTable']['ThirdAttackEmpoweredDamage'] = [[1, 300], [2, 450], [3, 3000], [4, 5000]]
        candidate, notes = self.candidate([line], raw=raw)
        target = {'kind': 'unit', 'api': 'TFT18_Nidalee', 'row': 'EmpoweredDamage', 'stars': [1, 2]}
        mapping = {'change': line, 'target': target, 'expected': [300, 450], 'observedBefore': [170, 255],
                   'numericEncoding': {'scale': 1, 'offset': 0}, 'reason': 'Exact old value.'}
        with self.assertRaisesRegex(tft_update.ReviewRequired, r"ThirdAttackEmpoweredDamage went from \[320, 480\] to the note's new \[300, 450\]"):
            tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, mappings=[mapping]))

    def test_our_own_override_on_a_sibling_is_not_upstream_moving(self):
        # 18.2's Radiant Hand of Justice: AD/AP pinned at 0.35 over an unchanged raw 0.3, and an
        # Omnivamp note whose new value happens to be 0.3.
        raw = deepcopy(self.raw)
        raw['units'][-1]['curveTable'].update(OmnivampBase=[[1, .24]], ADBase=[[1, .3]])
        overrides = deepcopy(self.overrides)
        overrides['units']['TFT18_Nidalee'] = {'curve': {'ADBase': [.35]}}
        self.rebase(raw, overrides)
        line = fixtures.change('Nidalee Base Omnivamp', '24%', '30%')
        candidate, notes = self.candidate([line])
        target = {'kind': 'unit', 'api': 'TFT18_Nidalee', 'row': 'OmnivampBase', 'stars': [1]}
        mapping = {'change': line, 'target': target, 'expected': [.3], 'observedBefore': [.24],
                   'numericEncoding': {'scale': .01, 'offset': 0}, 'reason': 'Exact old value.'}
        self.reviewed(candidate, notes, self.manifest(candidate, notes, mappings=[mapping]))

    def test_an_acknowledgement_belongs_to_a_numeric_note_on_a_curve_row(self):
        line = fixtures.change('Soraka Health', '800', '900')
        candidate, notes = self.candidate([line])
        mapping = {'change': line, 'target': {'kind': 'unit', 'api': 'TFT18_Soraka', 'stat': 'hp'}, 'expected': [900],
                   'observedBefore': [850], 'reason': 'Stale.', 'siblingRowAcknowledgement': {'rows': ['armor'], 'reason': 'x'}}
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'only applies to a numeric note mapped to a curve row'):
            tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, mappings=[mapping]))

    def test_attack_in_a_row_name_no_longer_hides_an_ability_amount_from_discovery(self):
        for row, expected in [('ThirdAttackEmpoweredDamage', {'damage'}), ('MiniDamagePerAttack', {'damage'}),
                              ('ThirdAttackHeal', {'heal'}), ('AutoAttackDamage', set()), ('BasicAttackDamage', set()),
                              ('BonusAttackDamage', set()), ('EmpoweredAttackDamage', set()), ('AttackSpeedBuff', set()),
                              ('NumEmpoweredAttacks', set())]:
            with self.subTest(row=row):
                self.assertEqual(tft_update._row_semantics(row), expected)
        # the automatic path now finds the third javelin by its exact old value, and only that row
        line = fixtures.change('Nidalee Ability Damage', '320/480 AP', '300/450 AP')
        raw = deepcopy(self.raw)
        raw['units'][-1]['ability']['attributeCalcs']['MagicDamageCalc2'] = {'terms': [
            {'type': 'scaled', 'op': 'add', 'scaling': 'AbilityPower', 'row': 'ThirdAttackEmpoweredDamage',
             'coefficient': [320, 480, 3000, 5000]}], 'values': [320, 480, 3000, 5000]}
        self.rebase(raw)
        candidate, notes = self.candidate([line])
        overrides, _ = tft_update.reconcile(candidate, self.previous, notes)
        self.assertEqual(overrides['units']['TFT18_Nidalee']['curve'], {'ThirdAttackEmpoweredDamage': [300, 450, 3000, 5000]})


class TestObservedLookupMappings(Base):
    """A reviewed official value can override a separately recorded lookup conflict."""
    OLD, EXPECTED, LOOKUP = [68, 102, 155, 165], [73, 108, 165, 175], [73, 108, 165, 280]

    def setUp(self):
        super().setUp()
        self.target = {'kind': 'unit', 'api': 'TFT18_Xayah', 'row': 'DamageAD', 'stars': [1, 2, 3, 4]}
        self.line = fixtures.change('Xayah Ability Damage', '68/102/155/165 AD', '73/108/165/175 AD')
        xayah = fixtures.unit('Xayah', 1)
        xayah['curveTable']['DamageAD'] = [[star, value] for star, value in enumerate(self.OLD, 1)]
        calc = {'resolved': True, 'terms': [{'type': 'scaled', 'op': 'add', 'scaling': 'AttackDamage',
                                            'row': 'DamageAD', 'coefficient': deepcopy(self.OLD)}],
                'values': deepcopy(self.OLD)}
        xayah['ability']['attributeCalcs'] = {'PhysicalDamageCalc1': deepcopy(calc)}
        xayah['attributeCalcs'] = {'PhysicalDamageCalc1': deepcopy(calc)}
        raw = deepcopy(self.raw)
        raw['units'].append(xayah)
        self.rebase(raw)

    def staged_raw(self, fourth=280):
        raw = deepcopy(self.raw)
        xayah = raw['units'][-1]
        values = [*self.LOOKUP[:3], fourth]
        xayah['curveTable']['DamageAD'] = [[star, value] for star, value in enumerate(values, 1)]
        for block in (xayah, xayah['ability']):
            calc = block['attributeCalcs']['PhysicalDamageCalc1']
            calc['terms'][0]['coefficient'] = deepcopy(values)
            calc['values'] = deepcopy(values)
        return raw

    def mapping(self, *, observed=True):
        record = {'change': self.line, 'target': deepcopy(self.target), 'observedBefore': deepcopy(self.OLD),
                  'expected': deepcopy(self.EXPECTED), 'numericEncoding': {'scale': 1, 'offset': 0},
                  'reason': 'Official four-star damage is 175; the staged lookup says 280 and remains a source conflict.'}
        if observed:
            record['observedLookup'] = deepcopy(self.LOOKUP)
        return record

    def test_exact_observed_lookup_acknowledges_the_conflict_but_final_model_uses_expected(self):
        candidate, notes = self.candidate([self.line], raw=self.staged_raw())
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'unverified coefficient'):
            tft_update.reconcile(candidate, self.previous, notes,
                                 review=self.manifest(candidate, notes, mappings=[self.mapping(observed=False)]))
        checked, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, mappings=[self.mapping()]))
        self.assertEqual(tft_update._values(checked, self.target), self.EXPECTED)
        self.assertEqual(checked.units['TFT18_Xayah']['calcs']['PhysicalDamageCalc1']['terms'][0]['coefficient'], self.EXPECTED)
        self.assertEqual(checked.raw['units'][-1]['curveTable']['DamageAD'][-1], [4, 280])
        check = next(c for c in audit['checks'] if c['target'] == self.target)
        self.assertEqual((check['expected'], check['observedLookup'], check['fixedScope']),
                         (self.EXPECTED, self.LOOKUP, True))

    def test_observed_lookup_requires_finite_numeric_values_at_every_target_coordinate(self):
        candidate, notes = self.candidate([self.line], raw=self.staged_raw())
        invalid = (None, [], [73, 108, 165], (73, 108, 165, 280), ['73', 108, 165, 280],
                   [True, 108, 165, 280], [float('nan'), 108, 165, 280], [float('inf'), 108, 165, 280])
        for values in invalid:
            with self.subTest(values=values), self.assertRaisesRegex(tft_update.ReviewRequired, 'invalid reviewed observedLookup'):
                record = {**self.mapping(), 'observedLookup': values}
                tft_update.reconcile(candidate, self.previous, notes,
                                     review=self.manifest(candidate, notes, mappings=[record]))

    def test_observed_lookup_matches_override_free_source_and_rejects_stale_values(self):
        candidate, notes = self.candidate([self.line], raw=self.staged_raw())
        overrides = deepcopy(self.overrides)
        overrides['units']['TFT18_Xayah'] = {'curve': {'DamageAD': deepcopy(self.EXPECTED)}}
        effective = self.snapshot('effective', candidate.raw, notes, overrides, self.audit)
        self.assertEqual(tft_update._values(effective, self.target), self.EXPECTED)
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'staged lookup does not hold.*observedLookup'):
            tft_update.reconcile(effective, self.previous, notes,
                                 review=self.manifest(effective, notes, mappings=[{**self.mapping(), 'observedLookup': self.EXPECTED}]))
        self.reviewed(effective, notes, self.manifest(effective, notes, mappings=[self.mapping()]))
        candidate, notes = self.candidate([self.line], raw=self.staged_raw(fourth=281))
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'staged lookup does not hold.*observedLookup'):
            tft_update.reconcile(candidate, self.previous, notes,
                                 review=self.manifest(candidate, notes, mappings=[self.mapping()]))

    def test_observed_lookup_does_not_cover_an_unrelated_stat_or_unmapped_star(self):
        for case in ('stat', 'star'):
            with self.subTest(case=case):
                raw, record = self.staged_raw(), self.mapping()
                if case == 'stat':
                    raw['units'][-1]['stats']['armor'] += 1
                    message = 'unexplained base-stat change'
                else:
                    record['target']['stars'] = [1, 2, 3]
                    for key in ('expected', 'observedBefore', 'observedLookup'):
                        record[key] = record[key][:3]
                    message = 'unverified coefficient'
                candidate, notes = self.candidate([self.line], raw=raw)
                with self.assertRaisesRegex(tft_update.ReviewRequired, message):
                    tft_update.reconcile(candidate, self.previous, notes,
                                         review=self.manifest(candidate, notes, mappings=[record]))

    def test_observed_lookup_does_not_permit_a_second_coordinate_decision(self):
        candidate, notes = self.candidate([self.line], raw=self.staged_raw())
        lookup = TestLookupChanges.record(self, self.target, self.LOOKUP, decision='keep-reviewed-value')
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'same coordinate twice'):
            tft_update.reconcile(candidate, self.previous, notes,
                                 review=self.manifest(candidate, notes, mappings=[self.mapping()], lookupChanges=[lookup]))

    def test_a_persisted_lookup_conflict_does_not_approve_a_changed_future_feed(self):
        candidate, notes = self.candidate([self.line], raw=self.staged_raw())
        checked, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, mappings=[self.mapping()]))
        _, repeated = tft_update.reconcile(checked, checked, notes)
        self.assertEqual(repeated, audit)
        future = self.snapshot('future', self.staged_raw(fourth=281), notes, checked.overrides, audit)
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'unverified coefficient'):
            tft_update.reconcile(future, checked, notes)


class TestPublished18_2Review(unittest.TestCase):
    """The real 18.1d -> 18.2 review, replayed on the archived inputs."""
    WHAT = 'Nidalee AP Form: Nidalee Empowered Attack Damage'

    @classmethod
    def setUpClass(cls):
        cls.previous = tft.load_snapshot(18, '18.1d')
        active = Path(tft.load_snapshot(18, '18.2').dir)
        cls.manifest = json.loads((active.parent / 'patch-reviews' / '18.2.json').read_text())
        cls.tmp = tempfile.TemporaryDirectory()
        staging = Path(cls.tmp.name) / '18.2'
        staging.mkdir()
        for name in ('metatft.json', 'bins.json', 'patchnotes.json', 'meta.json'):
            shutil.copyfile(active / name, staging / name)
        shutil.copyfile(Path(cls.previous.dir) / 'overrides.json', staging / 'overrides.json')
        cls.candidate = tft.Snapshot(18, '18.2', directory=str(staging))
        cls.notes = json.loads((staging / 'patchnotes.json').read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_every_published_mapping_passes_the_guard_without_an_acknowledgement(self):
        self.assertFalse([m for m in self.manifest['mappings'] if 'siblingRowAcknowledgement' in m])
        review = tft_update._BoundReview(self.candidate, self.previous, self.notes, self.manifest)
        self.assertEqual(sum(len(v) for v in review.changes.values()) + sum(len(v) for v in review.notes.values()), 98)

    def test_the_original_nidalee_mapping_would_not_have_passed(self):
        wrong = deepcopy(self.manifest)
        mapping = next(m for m in wrong['mappings'] if m.get('change', {}).get('what') == self.WHAT)
        self.assertEqual((mapping['target']['row'], mapping['observedBefore'], mapping['expected']),
                         ('ThirdAttackEmpoweredDamage', [320, 480], [300, 450]))
        mapping['target']['row'], mapping['observedBefore'] = 'EmpoweredDamage', [170, 255]
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'EmpoweredDamage may be the wrong row .ThirdAttackEmpoweredDamage'):
            tft_update._BoundReview(self.candidate, self.previous, self.notes, wrong)

    def test_the_published_snapshot_throws_170_255_javelins_and_a_300_450_third(self):
        snap = tft.load_snapshot(18, '18.2')
        nidalee = snap.unit('Nidalee')
        self.assertEqual([tft.curve_at(nidalee['curve']['EmpoweredDamage'], s) for s in (1, 2, 3, 4)], [170, 255, 2000, 3000])
        self.assertEqual([tft.curve_at(nidalee['curve']['ThirdAttackEmpoweredDamage'], s) for s in (1, 2, 3, 4)], [300, 450, 3000, 5000])
        calcs = {name.split('.')[-1]: calc['terms'][0]['coefficient'] for name, calc in nidalee['calcs'].items()
                 if calc['terms'] and calc['terms'][0].get('row', '').endswith('EmpoweredDamage')}
        self.assertEqual(calcs, {'MagicDamageCalc1': [170, 255, 2000, 3000], 'MagicDamageCalc2': [300, 450, 3000, 5000]})
        self.assertNotIn('EmpoweredDamage', snap.overrides['units']['TFT18_Nidalee']['curve'])
        findings, _ = tft.check_patch_notes(snap)
        self.assertEqual([f for f in findings if f['status'] != 'current'], [])
        self.assertEqual(snap.audit['reviews'], [self.manifest])
        self.assertEqual(snap.audit['automatic']['reviewManifestHash'], tft.json_hash(self.manifest))


class TestPublished18_3Review(unittest.TestCase):
    """The real 18.2b -> 18.3 review, replayed on the archived inputs (patch-reviews/18.3-review.md)."""

    @classmethod
    def setUpClass(cls):
        cls.previous = tft.load_snapshot(18, '18.2b')
        cls.published = tft.load_snapshot(18, '18.3')
        active = Path(cls.published.dir)
        cls.manifest = json.loads((active.parent / 'patch-reviews' / '18.3.json').read_text())
        cls.tmp = tempfile.TemporaryDirectory()
        staging = Path(cls.tmp.name) / '18.3'
        staging.mkdir()
        for name in ('metatft.json', 'bins.json', 'patchnotes.json', 'meta.json', 'communitydragon.json'):
            shutil.copyfile(active / name, staging / name)
        shutil.copyfile(Path(cls.previous.dir) / 'overrides.json', staging / 'overrides.json')
        cls.candidate = tft.Snapshot(18, '18.3', directory=str(staging))
        cls.notes = json.loads((staging / 'patchnotes.json').read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def reconcile(self, manifest):
        with contextlib.redirect_stderr(io.StringIO()):   # Ivern's unmodeled-scaling warning
            return tft_update.reconcile(self.candidate, self.previous, self.notes, review=manifest)

    def test_the_manifest_reproduces_the_published_overrides_and_checks(self):
        overrides, audit = self.reconcile(self.manifest)
        self.assertEqual(overrides, self.published.overrides)
        self.assertEqual(audit['checks'], self.published.audit['checks'])
        self.assertEqual(len(audit['checks']), 147)
        self.assertEqual(len(audit['automatic']['appliedChanges']), 29)
        self.assertEqual(audit['sourceCrossCheck'], self.published.audit['sourceCrossCheck'])

    def test_without_its_records_the_patch_stops_where_the_scheduled_runs_did(self):
        bare = {**self.manifest, 'mappings': [], 'dispositions': [], 'sourceDisagreements': []}
        with self.assertRaisesRegex(tft_update.ReviewRequired,
                                    'Blossom Charms Animate Shop Duration: trait change needs an explicit reviewed field mapping'):
            self.reconcile(bare)

    def test_the_gromp_base_ad_row_needs_its_acknowledgement(self):
        # A single base value spread over four stars sits 70% from the per-star
        # AutoAttackDamage row, and the 30%-slow rows look closer to the guard.
        stripped = deepcopy(self.manifest)
        mapping = next(m for m in stripped['mappings'] if m['target'].get('row') == 'AutoAttackDamage')
        self.assertEqual(mapping['siblingRowAcknowledgement']['rows'], ['SlowAmountAD', 'SlowAmountAd'])
        del mapping['siblingRowAcknowledgement']
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'AutoAttackDamage may be the wrong row'):
            tft_update._BoundReview(self.candidate, self.previous, self.notes, stripped)

    def test_the_published_decisions(self):
        snap = self.published
        at = lambda curve, positions: [tft.curve_at(curve, p) for p in positions]
        # Riot's note (and TFTraits), not the live client's 2/3/5/8
        self.assertEqual(at(snap.traits['DA_18_Invoker']['curve']['InvokerManaBonus'], (1, 2, 3, 4)), [3, 4, 6, 8])
        # 18.2 applied 60% while the row said 40%; 18.3 states the 60%
        nidalee = snap.unit('Nidalee')
        self.assertEqual(at(nidalee['forms']['AD']['curve']['ArmorIgnoreRatio'], (1, 2, 3)), [0.6, 0.6, 0.8])
        # The AD form's attack damage comes from its own row, so the stat alone would not reach a fight
        gromp = snap.unit('Gromp')
        self.assertEqual(tft.kit_spec(gromp, 1, 'AD')['baseAd'], 50)
        self.assertEqual(tft.kit_spec(gromp, 1, 'AD')['stats']['as'], 0.75)
        self.assertEqual(tft.kit_spec(gromp, 1, 'AP')['baseAd'], 30)
        findings, _ = tft.check_patch_notes(snap)
        self.assertEqual([f for f in findings if f['status'] != 'current'], [])
        explained = {(d['api'], d['stat']): (d['effective'], d['communitydragon'], d['disposition'])
                     for d in snap.audit['sourceCrossCheck']['explained']}
        self.assertEqual(explained[('TFT18_MasterYi', 'ad')], (60.0, 62.0, 'unresolved-riot-note-kept'))
        self.assertEqual(len(explained), 7)
        self.assertEqual([r['patch'] for r in snap.audit['reviews']], ['18.2', '18.2b', '18.3'])
        self.assertEqual(snap.audit['reviews'][-1], self.manifest)
        self.assertEqual(snap.audit['automatic']['reviewManifestHash'], tft.json_hash(self.manifest))


class TestLookupChanges(Base):
    def record(self, target, lookup, decision='accept-lookup', **extra):
        before = tft_update._values(self.previous, target)
        return {'target': target, 'decision': decision, 'observedBefore': before, 'lookup': lookup,
                'expected': lookup if decision == 'accept-lookup' else before,
                'evidence': ['CommunityDragon says so too.'], 'reason': 'The regenerated lookup caught up.', **extra}

    def test_an_unannounced_value_is_accepted_only_through_its_own_record(self):
        raw = deepcopy(self.raw)
        raw['units'][2]['stats']['hp'] = 900
        candidate, notes = self.candidate(raw=raw)
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'base-stat change'):
            tft_update.reconcile(candidate, self.previous, notes)
        record = self.record({'kind': 'unit', 'api': 'TFT18_Soraka', 'stat': 'hp'}, [900])
        checked, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, lookupChanges=[record]))
        self.assertEqual(checked.units['TFT18_Soraka']['stats']['hp'], 900)
        self.assertNotIn('stats', checked.overrides['units']['TFT18_Soraka'])     # the lookup supplies it, unpinned
        self.assertEqual(audit['automatic']['lookupChanges'][0]['decision'], 'accept-lookup')
        self.assertEqual(audit['automatic']['policyVersion'], 2)

    def test_accepting_a_value_on_a_pinned_row_moves_the_pin_and_splits_no_other_star(self):
        raw = deepcopy(self.raw)
        raw['units'][2]['curveTable']['DamageAP'][2][1] = 1100
        calc = raw['units'][2]['ability']['attributeCalcs']['MagicDamageCalc1']
        calc['terms'][0]['coefficient'][2] = calc['values'][2] = 1100
        candidate, notes = self.candidate(raw=raw)
        record = self.record({'kind': 'unit', 'api': 'TFT18_Soraka', 'row': 'DamageAP', 'stars': [3]}, [1100])
        checked, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, lookupChanges=[record]))
        self.assertEqual(checked.overrides['units']['TFT18_Soraka']['curve']['DamageAP'], [225, 335, 1100, 2200])
        damage = [c for c in audit['checks'] if c['target'].get('row') == 'DamageAP']
        self.assertEqual([(c['target']['stars'], c['expected']) for c in damage], [([1, 2], [225, 335])])

    def test_a_better_source_can_keep_the_reviewed_value_against_the_lookup(self):
        raw = deepcopy(self.raw)
        raw['units'][0]['curveTable']['PassiveHealPercent'] = [[1, .03], [2, .03], [3, .04], [4, .04]]
        candidate, notes = self.candidate(raw=raw)
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'unexplained lookup'):
            tft_update.reconcile(candidate, self.previous, notes)
        target = {'kind': 'unit', 'api': 'TFT18_Amumu', 'row': 'PassiveHealPercent', 'stars': [1, 2]}
        record = self.record(target, [.03, .03], decision='keep-reviewed-value')
        checked, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, lookupChanges=[record]))
        self.assertEqual(tft_update._values(checked, target), [.025, .025])
        kept = audit['checks'][-1]
        self.assertEqual((kept['expected'], kept['lookupValue'], kept['manualOnly']), ([.025, .025], [.03, .03], True))

    def test_a_record_must_describe_the_exact_staged_change(self):
        raw = deepcopy(self.raw)
        raw['units'][2]['stats']['hp'] = 900
        candidate, notes = self.candidate(raw=raw)
        target = {'kind': 'unit', 'api': 'TFT18_Soraka', 'stat': 'hp'}
        good = self.record(target, [900])
        cases = {'staged lookup does not hold': {'lookup': [950], 'expected': [950]},
                 'baseline no longer matches': {'observedBefore': [800]},
                 'needs decision': {'decision': 'whatever'},
                 'needs decision ': {'expected': [850]},
                 'needs evidence': {'evidence': []},
                 'needs an explanation': {'reason': ''},
                 'invalid reviewed lookup': {'lookup': ['900']}}
        for message, change in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(tft_update.ReviewRequired, message.strip()):
                tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, lookupChanges=[{**good, **change}]))
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'same coordinate twice'):
            tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, lookupChanges=[good, good]))
        unchanged = self.record({'kind': 'unit', 'api': 'TFT18_Soraka', 'stat': 'armor'}, [40])
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'not a change'):
            tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, lookupChanges=[good, unchanged]))

    def test_a_coordinate_cannot_be_both_mapped_from_a_note_and_accepted_from_the_lookup(self):
        line = fixtures.change('Soraka Health', '850', '900')
        raw = deepcopy(self.raw)
        raw['units'][2]['stats']['hp'] = 900
        candidate, notes = self.candidate([line], raw=raw)
        target = {'kind': 'unit', 'api': 'TFT18_Soraka', 'stat': 'hp'}
        mapping = {'change': line, 'target': target, 'expected': [900], 'observedBefore': [850], 'reason': 'Note.'}
        review = self.manifest(candidate, notes, mappings=[mapping], lookupChanges=[self.record(target, [900])])
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'same coordinate twice'):
            tft_update.reconcile(candidate, self.previous, notes, review=review)

    def test_a_summon_and_a_traits_inactive_column_have_an_address(self):
        golem = fixtures.unit('Golem')
        golem.update(shopUnit=False, apiName='TFT18_GolemSummon')
        raw = deepcopy(self.raw)
        raw['units'].append(golem)
        raw['traits'].append({'apiName': 'DA_18_Solar', 'name': 'Solar', 'effects': [{'minUnits': 3, 'style': 1}],
                              'curveTable': {'PercentIncreasePer3Star': [[0, .015], [1, .015]]}})
        self.rebase(raw)
        changed = deepcopy(raw)
        changed['units'][-1]['stats']['hp'] = 900
        changed['traits'][-1]['curveTable']['PercentIncreasePer3Star'] = [[0, .01], [1, .01]]
        candidate, notes = self.candidate(raw=changed)
        records = [self.record({'kind': 'unit', 'api': 'TFT18_GolemSummon', 'stat': 'hp'}, [900]),
                   self.record({'kind': 'trait', 'api': 'DA_18_Solar', 'row': 'PercentIncreasePer3Star', 'columns': [0, 1]}, [.01, .01])]
        checked, _ = self.reviewed(candidate, notes, self.manifest(candidate, notes, lookupChanges=records))
        self.assertEqual(checked.extras['TFT18_GolemSummon']['stats']['hp'], 900)
        # a patch-note mapping still may not address the inactive column
        line = fixtures.change('Solar Bonus', '1.5%', '1%', section='TRAITS')
        candidate, notes = self.candidate([line], raw=raw)
        mapping = {'change': line, 'target': {'kind': 'trait', 'api': 'DA_18_Solar', 'row': 'PercentIncreasePer3Star', 'columns': [0]},
                   'expected': [.01], 'observedBefore': [.015], 'reason': 'x'}
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'invalid reviewed target'):
            tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, mappings=[mapping]))

    def test_four_star_attack_damage_that_is_no_longer_base_times_1_5_cubed(self):
        raw = deepcopy(self.raw)
        raw['units'][2]['stats']['damageByStar'] = [40, 60, 90, 135]
        raw['units'][2]['curveTable']['AutoAttackDamage'] = [[1, 40], [2, 60], [3, 90], [4, 135]]
        self.rebase(raw)
        changed = deepcopy(raw)
        changed['units'][2]['stats']['damageByStar'] = [40, 60, 90, 120]
        changed['units'][2]['curveTable']['AutoAttackDamage'][3][1] = 120
        candidate, notes = self.candidate(raw=changed)
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'unexplained'):
            tft_update.reconcile(candidate, self.previous, notes)
        record = self.record({'kind': 'unit', 'api': 'TFT18_Soraka', 'row': 'AutoAttackDamage', 'stars': [4]}, [120])
        self.reviewed(candidate, notes, self.manifest(candidate, notes, lookupChanges=[record]))
        changed['units'][2]['stats']['damageByStar'] = [40, 60, 90, 121]     # no longer the row's copy
        candidate, notes = self.candidate(raw=changed)
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'damageByStar'):
            tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, lookupChanges=[record]))

    def test_a_differently_cased_tooltip_copy_of_a_trait_row_follows_the_row(self):
        row = [[0, 1], [3, 1.06], [4, 1.06]]
        raw = deepcopy(self.raw)
        raw['traits'].append({'apiName': 'DA_Riftbeast18', 'name': 'Riftbeast', 'effects': [{'minUnits': 3, 'style': 1}],
                              'curveTable': {'CapstoneASPD': deepcopy(row)}, 'curveValues': {'CapstoneAspd': deepcopy(row)}})
        overrides = deepcopy(self.overrides)
        overrides['traits']['DA_Riftbeast18'] = {'curve': {'CapstoneASPD': {'3': 1.05, '4': 1.05}}}
        self.audit['checks'].append({'what': 'Riftbeast (7) Stats', 'expected': [1.05, 1.05],
                                     'target': {'kind': 'trait', 'api': 'DA_Riftbeast18', 'row': 'CapstoneASPD', 'columns': [3, 4]}})
        self.rebase(raw, overrides)
        caught_up = deepcopy(raw)
        for table, name in (('curveTable', 'CapstoneASPD'), ('curveValues', 'CapstoneAspd')):
            caught_up['traits'][-1][table][name] = [[0, 1], [3, 1.05], [4, 1.05]]
        candidate, notes = self.candidate(raw=caught_up, patch='18.1d')
        tft_update.reconcile(candidate, self.previous, notes)
        caught_up['traits'][-1]['curveValues']['CapstoneAspd'][1][1] = 1.04     # the copy alone says something else
        candidate, notes = self.candidate(raw=caught_up, patch='18.1d')
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'unexplained lookup change'):
            tft_update.reconcile(candidate, self.previous, notes)


class TestTraitCalculationCaches(Base):
    OLD, NEW = [.07, .10, .14, .18], [.07, .12, .16, .20]

    def setUp(self):
        super().setUp()
        self.api = 'DA_18_Lunar'
        self.target = {'kind': 'trait', 'api': self.api, 'row': 'AbilityPower', 'columns': [1, 2, 3, 4]}
        self.line = fixtures.change('Lunar Ability Power', '7/10/14/18%', '7/12/16/20%', section='TRAITS')
        calc = {'resolved': True, 'terms': [
            {'type': 'flat', 'op': 'add', 'row': 'AbilityPower', 'values': deepcopy(self.OLD)},
            {'type': 'flat', 'op': 'add', 'row': 'LunarMultiplier', 'values': [2, 2, 2, 2]}],
            'values': [value + 2 for value in self.OLD]}
        trait = {'apiName': self.api, 'name': 'Lunar', 'effects': [{'minUnits': 2, 'style': 1}],
                 'curveTable': {'AbilityPower': [[0, .07], *[[column, value] for column, value in enumerate(self.OLD, 1)]],
                                'LunarMultiplier': [[column, 2] for column in range(5)],
                                'UnrelatedValue': [[0, .3], [1, .3]]},
                 'attributeCalcs': {'GenericCalc1': calc},
                 'attributeValues': {'GenericCalc1': deepcopy(calc['values'])}}
        raw = deepcopy(self.raw)
        raw['traits'].append(trait)
        self.rebase(raw)

    def changed_raw(self):
        raw = deepcopy(self.raw)
        trait = raw['traits'][-1]
        trait['curveTable']['AbilityPower'] = [[0, .07], *[[column, value] for column, value in enumerate(self.NEW, 1)]]
        calc = trait['attributeCalcs']['GenericCalc1']
        term = calc['terms'][0]
        term['coefficient' if term['type'] == 'scaled' else 'values'] = deepcopy(self.NEW)
        calc['values'] = [value + 2 for value in self.NEW]
        trait['attributeValues']['GenericCalc1'] = deepcopy(calc['values'])
        return raw

    def mapping(self):
        return {'change': self.line, 'target': self.target, 'observedBefore': self.OLD, 'expected': self.NEW,
                'numericEncoding': {'scale': .01, 'offset': 0}, 'reason': 'The official AP row names these breakpoint columns.'}

    def test_flat_trait_calculation_caches_follow_their_mapped_rows_without_a_cache_disposition(self):
        candidate, notes = self.candidate([self.line], raw=self.changed_raw())
        checked, _ = self.reviewed(candidate, notes, self.manifest(candidate, notes, mappings=[self.mapping()]))
        self.assertEqual(tft_update._values(checked, self.target), self.NEW)
        self.assertEqual(checked.raw['traits'][-1]['attributeValues']['GenericCalc1'], [2.07, 2.12, 2.16, 2.20])

    def test_flat_trait_caches_reject_corrupt_old_or_new_calculation_and_attribute_values(self):
        baseline = deepcopy(self.raw)
        for when, key in (('old', 'attributeCalcs'), ('new', 'attributeCalcs'),
                          ('old', 'attributeValues'), ('new', 'attributeValues')):
            with self.subTest(when=when, key=key):
                raw = deepcopy(baseline)
                if when == 'old':
                    values = raw['traits'][-1][key]['GenericCalc1']
                    (values['values'] if key == 'attributeCalcs' else values)[1] += 1
                self.rebase(raw)
                changed = self.changed_raw()
                if when == 'new':
                    values = changed['traits'][-1][key]['GenericCalc1']
                    (values['values'] if key == 'attributeCalcs' else values)[1] += 1
                candidate, notes = self.candidate([self.line], raw=changed)
                with self.assertRaisesRegex(tft_update.ReviewRequired, 'unverified calculation cache'):
                    tft_update.reconcile(candidate, self.previous, notes,
                                         review=self.manifest(candidate, notes, mappings=[self.mapping()]))

    def test_trait_caches_never_assume_stats_for_scaled_runtime_or_unknown_terms(self):
        baseline = deepcopy(self.raw)
        for kind in ('scaled', 'runtime', 'unknown'):
            with self.subTest(kind=kind):
                raw = deepcopy(baseline)
                term = raw['traits'][-1]['attributeCalcs']['GenericCalc1']['terms'][0]
                term['type'] = kind
                if kind == 'scaled':
                    term['scaling'] = 'AbilityPower'
                    term['coefficient'] = term.pop('values')
                self.rebase(raw)
                candidate, notes = self.candidate([self.line], raw=self.changed_raw())
                with self.assertRaisesRegex(tft_update.ReviewRequired, 'trait calculation contains runtime or scaled terms'):
                    tft_update.reconcile(candidate, self.previous, notes,
                                         review=self.manifest(candidate, notes, mappings=[self.mapping()]))

    def test_trait_cache_arithmetic_does_not_approve_other_rows_or_formula_changes(self):
        for case, message in (('row', 'unexplained lookup change'), ('formula', 'unverified formula')):
            with self.subTest(case=case):
                changed = self.changed_raw()
                if case == 'row':
                    changed['traits'][-1]['curveTable']['UnrelatedValue'][1][1] = .5
                else:
                    changed['traits'][-1]['attributeCalcs']['GenericCalc1']['terms'][1]['op'] = 'multiply'
                candidate, notes = self.candidate([self.line], raw=changed)
                with self.assertRaisesRegex(tft_update.ReviewRequired, message):
                    tft_update.reconcile(candidate, self.previous, notes,
                                         review=self.manifest(candidate, notes, mappings=[self.mapping()]))


class TestDefinitionDispositions(Base):
    def disposition(self, path, before, after):
        return {'path': path, 'before': before, 'after': after, 'disposition': 'presentation-only', 'reason': 'Renamed, same numbers.'}

    def root_leaf_fixture(self):
        raw = deepcopy(self.raw)
        role = 'DA_Role_MagicSpecialist_Manaless'
        raw['roleData'][role] = {'name': 'Magic Specialist', 'roleTags': ['Role.Magic', 'Role.Specialist'],
                                 'resourceType': 'Mana.ResourceType.Mana'}
        raw['units'][2]['role'] = role
        raw['units'][2]['stats'].update(mana=0, initialMana=0)
        raw['unitAssetNames'] = {'DA_18_GnarBig': 'TFT18_Gnar'}
        self.rebase(raw)
        changed = deepcopy(raw)
        changed['roleData'][role]['resourceType'] = 'Mana.ResourceType.None'
        changed['unitAssetNames']['DA_18_GnarBig'] = 'TFT18_GnarBig'
        records = [self.disposition(f'roleData.{role}.resourceType', 'Mana.ResourceType.Mana', 'Mana.ResourceType.None'),
                   self.disposition('unitAssetNames.DA_18_GnarBig', 'TFT18_Gnar', 'TFT18_GnarBig')]
        return changed, records

    def test_known_root_leaves_need_separate_exact_records_and_keep_the_audit(self):
        raw, records = self.root_leaf_fixture()
        candidate, notes = self.candidate(raw=raw)
        for absent in ([], records[:1], records[1:]):
            with self.subTest(records=absent), self.assertRaisesRegex(tft_update.ReviewRequired, 'unverified role definitions or lookup structure'):
                tft_update.reconcile(candidate, self.previous, notes,
                                     review=self.manifest(candidate, notes, definitionDispositions=absent))
        checked, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, definitionDispositions=records))
        self.assertEqual(checked.units['TFT18_Soraka']['resource'], 'None')
        for field in ('stats', 'kind', 'roleTags'):
            self.assertEqual(checked.units['TFT18_Soraka'][field], self.previous.units['TFT18_Soraka'][field])
        for record in records:
            self.assertIn({'kind': 'reviewed-definition', 'path': record['path'],
                           'disposition': record['disposition'], 'reason': record['reason']},
                          audit['automatic']['definitionChanges'])

    def test_known_root_leaf_records_reject_wrong_values_stale_records_and_broad_parents(self):
        raw, records = self.root_leaf_fixture()
        candidate, notes = self.candidate(raw=raw)
        role = 'DA_Role_MagicSpecialist_Manaless'
        bad_records = [
            {**records[0], 'before': 'Mana.ResourceType.Energy'},
            {**records[0], 'after': 'Mana.ResourceType.Rage'},
            self.disposition('roleData', self.previous.raw['roleData'], raw['roleData']),
            self.disposition(f'roleData.{role}', self.previous.raw['roleData'][role], raw['roleData'][role]),
        ]
        for bad in bad_records:
            with self.subTest(record=bad), self.assertRaisesRegex(tft_update.ReviewRequired, 'unverified role definitions or lookup structure'):
                tft_update.reconcile(candidate, self.previous, notes,
                                     review=self.manifest(candidate, notes, definitionDispositions=[bad, records[1]]))
        candidate, notes = self.candidate()
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'does not contain'):
            tft_update.reconcile(candidate, self.previous, notes,
                                 review=self.manifest(candidate, notes, definitionDispositions=records))

    def test_known_root_leaf_approval_cannot_cover_other_tags_aliases_or_roots(self):
        base_raw, records = self.root_leaf_fixture()
        role = 'DA_Role_MagicSpecialist_Manaless'
        for case in ('role-tag', 'unit-alias', 'other-root', 'new-root', 'new-role-key'):
            with self.subTest(case=case):
                raw = deepcopy(base_raw)
                if case == 'role-tag':
                    raw['roleData'][role]['roleTags'][0] = 'Role.Attack'
                elif case == 'unit-alias':
                    raw['unitAssetNames']['DA_18_GnarBig'] = 'TFT18_Soraka'
                elif case == 'other-root':
                    raw['roles']['caster'] = 'Attack Caster'
                elif case == 'new-root':
                    raw['combatSettings'] = {'multiplier': 2}
                elif case == 'new-role-key':
                    raw['roleData'][role]['newManaMechanic'] = True
                candidate, notes = self.candidate(raw=raw)
                with self.assertRaisesRegex(tft_update.ReviewRequired, 'unverified role definitions or lookup structure'):
                    tft_update.reconcile(candidate, self.previous, notes,
                                         review=self.manifest(candidate, notes, definitionDispositions=records))

    def test_a_known_root_cannot_be_added_even_when_its_value_is_null(self):
        raw = deepcopy(self.raw)
        self.assertNotIn('unitAssetNames', raw)
        raw['unitAssetNames'] = None
        candidate, notes = self.candidate(raw=raw)
        for records in ([], [self.disposition('unitAssetNames', None, None)]):
            with self.subTest(records=records), self.assertRaisesRegex(tft_update.ReviewRequired, 'unverified role definitions or lookup structure'):
                tft_update.reconcile(candidate, self.previous, notes,
                                     review=self.manifest(candidate, notes, definitionDispositions=records))

    def calculation_member_fixture(self):
        raw = deepcopy(self.raw)
        unit = raw['units'][2]
        unit['attributeCalcs'] = deepcopy(unit['ability']['attributeCalcs'])
        unit['attributeValues'] = {name: deepcopy(calc['values']) for name, calc in unit['attributeCalcs'].items()}
        unit['ability']['attributeValues'] = deepcopy(unit['attributeValues'])
        self.rebase(raw)
        added = deepcopy(raw)
        calc = {'resolved': True, 'terms': [{'type': 'flat', 'op': 'add', 'values': [1, 1, 1, 1]}],
                'values': [1, 1, 1, 1]}
        records = []
        for block, path in ((added['units'][2], 'TFT18_Soraka'),
                            (added['units'][2]['ability'], 'TFT18_Soraka.ability')):
            for key, value in (('attributeCalcs', calc), ('attributeValues', calc['values'])):
                block[key]['GenericCalc3'] = deepcopy(value)
                records.append(self.disposition(f'{path}.{key}.GenericCalc3', None, deepcopy(value)))
        return added, records

    def test_each_added_calculation_and_value_member_requires_its_exact_record(self):
        raw, records = self.calculation_member_fixture()
        candidate, notes = self.candidate(raw=raw)
        for missing in (records, *[[record] for record in records]):
            supplied = [record for record in records if record not in missing]
            with self.subTest(missing=[r['path'] for r in missing]), \
                    self.assertRaisesRegex(tft_update.ReviewRequired, 'definition structure changed'):
                tft_update.reconcile(candidate, self.previous, notes,
                                     review=self.manifest(candidate, notes, definitionDispositions=supplied))
        checked, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, definitionDispositions=records))
        self.assertEqual(checked.units['TFT18_Soraka']['calcs']['MagicDamageCalc1'],
                         self.previous.units['TFT18_Soraka']['calcs']['MagicDamageCalc1'])
        self.assertEqual(checked.units['TFT18_Soraka']['calcs']['GenericCalc3']['values'], [1, 1, 1, 1])
        for record in records:
            self.assertIn({'kind': 'reviewed-definition', 'path': record['path'],
                           'disposition': record['disposition'], 'reason': record['reason']},
                          audit['automatic']['definitionChanges'])

    def test_added_calculation_records_reject_wrong_or_stale_full_member_values(self):
        raw, records = self.calculation_member_fixture()
        candidate, notes = self.candidate(raw=raw)
        for bad in ({**records[0], 'before': {}}, {**records[0], 'after': {'resolved': True}},
                    {**records[0], 'after': {**records[0]['after'], 'values': [2, 2, 2, 2]}},
                    {**records[1], 'after': [2, 2, 2, 2]}):
            supplied = [bad if record['path'] == bad['path'] else record for record in records]
            with self.subTest(record=bad), self.assertRaisesRegex(tft_update.ReviewRequired, 'definition structure changed'):
                tft_update.reconcile(candidate, self.previous, notes,
                                     review=self.manifest(candidate, notes, definitionDispositions=supplied))
        candidate, notes = self.candidate()
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'does not contain'):
            tft_update.reconcile(candidate, self.previous, notes,
                                 review=self.manifest(candidate, notes, definitionDispositions=records))

    def test_calculation_parent_approval_and_other_formula_edits_do_not_cover_added_members(self):
        raw, records = self.calculation_member_fixture()
        candidate, notes = self.candidate(raw=raw)
        parent_records = []
        for before, after, path in ((self.previous.raw['units'][2], raw['units'][2], 'TFT18_Soraka'),
                                    (self.previous.raw['units'][2]['ability'], raw['units'][2]['ability'], 'TFT18_Soraka.ability')):
            for key in ('attributeCalcs', 'attributeValues'):
                parent_records.append(self.disposition(f'{path}.{key}', before[key], after[key]))
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'definition structure changed'):
            tft_update.reconcile(candidate, self.previous, notes,
                                 review=self.manifest(candidate, notes, definitionDispositions=parent_records))
        for case, message in (('formula', 'unverified formula'), ('cache', 'unverified calculation cache')):
            with self.subTest(case=case):
                changed = deepcopy(raw)
                calc = changed['units'][2]['ability']['attributeCalcs']['MagicDamageCalc1']
                if case == 'formula':
                    calc['terms'][0]['op'] = 'multiply'
                else:
                    calc['values'][0] += 1
                candidate, notes = self.candidate(raw=changed)
                with self.assertRaisesRegex(tft_update.ReviewRequired, message):
                    tft_update.reconcile(candidate, self.previous, notes,
                                         review=self.manifest(candidate, notes, definitionDispositions=records))

    def orphan_row_fixture(self, *, shop=False, addressable=False, array='values'):
        old, new = [.035, .0455, .056, .056], [.045, .0059, .072, .072]
        row, name = 'HealthPercentHeal', 'TFTCalculationAttributes.HealthCalc1'
        extra = fixtures.unit('SprykinSummonMelee')
        extra['shopUnit'] = shop
        term = {'type': 'flat' if array == 'values' else 'scaled', 'op': 'add', 'row': row, array: old}
        if array == 'coefficient':
            term['scaling'] = 'AbilityPower'
        calc = {'resolved': True, 'terms': [term], 'values': old}
        extra['attributeCalcs'] = {name: deepcopy(calc)}
        extra['attributeValues'] = {name: deepcopy(old)}
        extra['ability']['attributeCalcs'] = {name: deepcopy(calc)}
        extra['ability']['attributeValues'] = {name: deepcopy(old)}
        extra['ability']['footer'] = [{'row': row, 'values': [[star, value] for star, value in enumerate(old[:3], 1)]}]
        if addressable:
            extra['curveTable'][row] = [[star, value] for star, value in enumerate(old, 1)]
        raw = deepcopy(self.raw)
        raw['units'] = [unit for unit in raw['units'] if unit['apiName'] != extra['apiName']]
        raw['units'].append(extra)
        self.rebase(raw)
        changed = deepcopy(raw)
        unit = changed['units'][-1]
        records = []
        for block, path in ((unit, extra['apiName']), (unit['ability'], extra['apiName'] + '.ability')):
            block['attributeCalcs'][name]['terms'][0][array] = deepcopy(new)
            block['attributeCalcs'][name]['values'] = deepcopy(new)
            block['attributeValues'][name] = deepcopy(new)
            records.append(self.disposition(f'{path}.attributeCalcs.{name}.terms[0].{array}', old, new))
        footer = [[1, .045], [2, .00585], [3, .072]]  # the source footer has different two-star precision
        unit['ability']['footer'][0]['values'] = footer
        records.append(self.disposition(extra['apiName'] + '.ability.footer[0].values',
                                        extra['ability']['footer'][0]['values'], footer))
        return changed, records

    def test_non_shop_orphan_arrays_are_accepted_only_by_their_exact_array_records(self):
        for array in ('values', 'coefficient'):
            with self.subTest(array=array):
                raw, records = self.orphan_row_fixture(array=array)
                candidate, notes = self.candidate(raw=raw)
                for missing in (records, *[[record] for record in records]):
                    supplied = [record for record in records if record not in missing]
                    with self.subTest(missing=[r['path'] for r in missing]), self.assertRaises(tft_update.ReviewRequired):
                        tft_update.reconcile(candidate, self.previous, notes,
                                             review=self.manifest(candidate, notes, definitionDispositions=supplied))
                checked, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, definitionDispositions=records))
                self.assertEqual(checked.extras['TFT18_SprykinSummonMelee']['curve'], {})
                self.assertEqual(checked.extras['TFT18_SprykinSummonMelee']['stats'],
                                 self.previous.extras['TFT18_SprykinSummonMelee']['stats'])
                for record in records:
                    self.assertIn({'kind': 'reviewed-definition', 'path': record['path'],
                                   'disposition': record['disposition'], 'reason': record['reason']},
                                  audit['automatic']['definitionChanges'])

    def test_orphan_array_records_reject_wrong_stale_parent_and_corrupt_cache_values(self):
        raw, records = self.orphan_row_fixture()
        candidate, notes = self.candidate(raw=raw)
        for bad in ({**records[0], 'before': [.01] * 4}, {**records[0], 'after': [.09] * 4}):
            supplied = [bad, *records[1:]]
            with self.subTest(record=bad), self.assertRaisesRegex(tft_update.ReviewRequired, 'unverified coefficient'):
                tft_update.reconcile(candidate, self.previous, notes,
                                     review=self.manifest(candidate, notes, definitionDispositions=supplied))
        parent = 'TFT18_SprykinSummonMelee.ability.footer[0]'
        broad = self.disposition(parent, self.previous.raw['units'][-1]['ability']['footer'][0], raw['units'][-1]['ability']['footer'][0])
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'unexplained lookup change'):
            tft_update.reconcile(candidate, self.previous, notes,
                                 review=self.manifest(candidate, notes, definitionDispositions=[*records[:2], broad]))
        corrupted = deepcopy(raw)
        corrupted['units'][-1]['ability']['attributeCalcs']['TFTCalculationAttributes.HealthCalc1']['values'][0] += .01
        candidate, notes = self.candidate(raw=corrupted)
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'unverified calculation cache'):
            tft_update.reconcile(candidate, self.previous, notes,
                                 review=self.manifest(candidate, notes, definitionDispositions=records))
        candidate, notes = self.candidate()
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'does not contain'):
            tft_update.reconcile(candidate, self.previous, notes,
                                 review=self.manifest(candidate, notes, definitionDispositions=records))

    def test_orphan_array_dispositions_cannot_approve_shop_units_or_addressable_rows(self):
        for case in ('shop', 'previous-shop', 'candidate-shop', 'addressable', 'previous-row', 'candidate-row'):
            with self.subTest(case=case):
                raw, records = self.orphan_row_fixture(shop=case in ('shop', 'previous-shop'),
                                                       addressable=case in ('addressable', 'previous-row'))
                unit = raw['units'][-1]
                if case in ('previous-shop', 'candidate-shop'):
                    unit['shopUnit'] = case == 'candidate-shop'
                    records.append(self.disposition(unit['apiName'] + '.shopUnit', not unit['shopUnit'], unit['shopUnit']))
                elif case == 'previous-row':
                    old = unit['curveTable'].pop('HealthPercentHeal')
                    records.append(self.disposition(unit['apiName'] + '.curveTable.HealthPercentHeal', old, None))
                elif case == 'candidate-row':
                    curve = [[star, value] for star, value in enumerate([.045, .0059, .072, .072], 1)]
                    unit['curveTable']['HealthPercentHeal'] = curve
                    records.append(self.disposition(unit['apiName'] + '.curveTable.HealthPercentHeal', None, curve))
                candidate, notes = self.candidate(raw=raw)
                with self.assertRaisesRegex(tft_update.ReviewRequired, 'unverified coefficient'):
                    tft_update.reconcile(candidate, self.previous, notes,
                                         review=self.manifest(candidate, notes, definitionDispositions=records))
        # Addressable extra rows still work through the ordinary numeric lookup review.
        raw, _ = self.orphan_row_fixture(addressable=True)
        values = [.045, .0059, .072, .072]
        raw['units'][-1]['curveTable']['HealthPercentHeal'] = [[star, value] for star, value in enumerate(values, 1)]
        raw['units'][-1]['ability']['footer'][0]['values'][1][1] = values[1]
        candidate, notes = self.candidate(raw=raw)
        target = {'kind': 'unit', 'api': 'TFT18_SprykinSummonMelee', 'row': 'HealthPercentHeal', 'stars': [1, 2, 3, 4]}
        record = TestLookupChanges.record(self, target, values)
        checked, _ = self.reviewed(candidate, notes, self.manifest(candidate, notes, lookupChanges=[record]))
        self.assertEqual([tft.curve_at(checked.extras[target['api']]['curve'][target['row']], star) for star in target['stars']], values)

    def test_a_text_change_needs_its_exact_record(self):
        raw = deepcopy(self.raw)
        raw['units'][2]['ability']['desc'] = 'A renamed spell.'
        candidate, notes = self.candidate(raw=raw)
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'unverified formula, mechanic, or value at TFT18_Soraka.ability.desc'):
            tft_update.reconcile(candidate, self.previous, notes)
        good = self.disposition('TFT18_Soraka.ability.desc', 'A spell.', 'A renamed spell.')
        _, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, definitionDispositions=[good]))
        self.assertIn({'kind': 'reviewed-definition', 'path': good['path'], 'disposition': 'presentation-only', 'reason': good['reason']},
                      audit['automatic']['definitionChanges'])
        for bad in ({**good, 'after': 'A renamed spell that also burns.'}, {**good, 'before': 'Another spell.'}):
            with self.subTest(bad=bad), self.assertRaisesRegex(tft_update.ReviewRequired, 'unverified formula'):
                tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, definitionDispositions=[bad]))

    def test_a_record_for_a_change_that_did_not_happen_is_refused(self):
        candidate, notes = self.candidate()
        stale = self.disposition('TFT18_Soraka.ability.desc', 'A spell.', 'A renamed spell.')
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'does not contain'):
            tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, definitionDispositions=[stale]))
        for bad in ({**stale, 'path': ''}, {k: v for k, v in stale.items() if k != 'after'}, {**stale, 'disposition': ''}, {**stale, 'reason': ''}):
            with self.subTest(bad=bad), self.assertRaises(tft_update.ReviewRequired):
                tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, definitionDispositions=[bad]))

    def test_added_non_shop_unit_and_unused_trait_require_exact_root_records(self):
        extra = fixtures.unit('GnarBig')
        extra['shopUnit'] = False
        trait = {'apiName': 'TFT18_Prowler', 'name': 'Prowler', 'units': [], 'effects': [], 'curveTable': {}}
        raw = deepcopy(self.raw)
        raw['units'].append(extra)
        raw['traits'].append(trait)
        candidate, notes = self.candidate(raw=raw)
        records = [self.disposition(value['apiName'], None, value) for value in (extra, trait)]
        for absent in ([], records[:1], records[1:]):
            with self.subTest(records=absent), self.assertRaisesRegex(tft_update.ReviewRequired, 'new or removed'):
                tft_update.reconcile(candidate, self.previous, notes,
                                     review=self.manifest(candidate, notes, definitionDispositions=absent))
        checked, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, definitionDispositions=records))
        self.assertEqual(set(checked.units), set(self.previous.units))
        self.assertIn(extra['apiName'], checked.extras)
        self.assertIn(trait['apiName'], checked.traits)
        for record in records:
            self.assertIn({'kind': 'reviewed-definition', 'path': record['path'],
                           'disposition': record['disposition'], 'reason': record['reason']},
                          audit['automatic']['definitionChanges'])

    def test_added_definition_record_must_match_the_full_entity_and_absent_baseline(self):
        extra = fixtures.unit('GnarBig')
        extra['shopUnit'] = False
        raw = deepcopy(self.raw)
        raw['units'].append(extra)
        candidate, notes = self.candidate(raw=raw)
        good = self.disposition(extra['apiName'], None, extra)
        incomplete = {key: value for key, value in extra.items() if key != 'ability'}
        changed = deepcopy(extra)
        changed['stats']['hp'] += 1
        for bad in ({**good, 'before': {}}, {**good, 'after': incomplete}, {**good, 'after': changed},
                    {**good, 'path': extra['apiName'] + '.stats'}):
            with self.subTest(record=bad), self.assertRaisesRegex(tft_update.ReviewRequired, 'new or removed unit'):
                tft_update.reconcile(candidate, self.previous, notes,
                                     review=self.manifest(candidate, notes, definitionDispositions=[bad]))
        candidate, notes = self.candidate()  # the reviewed addition is no longer present
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'does not contain'):
            tft_update.reconcile(candidate, self.previous, notes,
                                 review=self.manifest(candidate, notes, definitionDispositions=[good]))

    def test_new_shop_champion_is_refused_even_with_an_exact_definition_record(self):
        base_raw = deepcopy(self.raw)
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                raw = deepcopy(base_raw)
                extra = fixtures.unit('NewChampion')
                if legacy:
                    for unit in [*raw['units'], extra]:
                        unit.pop('shopUnit')
                        unit['traits'] = ['Existing']
                self.rebase(raw)
                raw['units'].append(extra)
                candidate, notes = self.candidate(raw=raw)
                record = self.disposition(extra['apiName'], None, extra)
                with self.assertRaisesRegex(tft_update.ReviewRequired, 'new or removed unit'):
                    tft_update.reconcile(candidate, self.previous, notes,
                                         review=self.manifest(candidate, notes, definitionDispositions=[record]))

    def test_legacy_shop_classification_cannot_be_changed_by_an_extra_unit(self):
        raw = deepcopy(self.raw)
        for unit in raw['units']:
            unit.pop('shopUnit')
            unit['traits'] = ['Existing']
        self.rebase(raw)
        extra = fixtures.unit('GnarBig')
        extra['shopUnit'] = False
        raw['units'].append(extra)  # the first flag would demote every legacy champion
        candidate, notes = self.candidate(raw=raw)
        record = self.disposition(extra['apiName'], None, extra)
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'new or removed unit'):
            tft_update.reconcile(candidate, self.previous, notes,
                                 review=self.manifest(candidate, notes, definitionDispositions=[record]))

    def test_added_trait_with_champion_members_or_modeled_effects_is_refused(self):
        trait = {'apiName': 'TFT18_Prowler', 'name': 'Prowler', 'units': [], 'effects': [], 'curveTable': {}}
        base_raw = deepcopy(self.raw)
        for case in ('declared-member', 'current-reference', 'previous-reference', 'modeled-effect'):
            with self.subTest(case=case):
                previous_raw = deepcopy(base_raw)
                if case == 'previous-reference':
                    previous_raw['units'][2]['traitApiNames'] = [trait['apiName']]
                self.rebase(previous_raw)
                raw = deepcopy(previous_raw)
                added = deepcopy(trait)
                if case == 'declared-member':
                    added['units'] = [{'unit': 'TFT18_Soraka'}]
                elif case == 'current-reference':
                    raw['units'][2]['traitApiNames'] = [trait['apiName']]
                elif case == 'previous-reference':
                    raw['units'][2]['traitApiNames'] = []
                raw['traits'].append(added)
                candidate, notes = self.candidate(raw=raw)
                records = [self.disposition(added['apiName'], None, added)]
                if case in ('current-reference', 'previous-reference'):
                    records.append(self.disposition('TFT18_Soraka.traitApiNames',
                                                    previous_raw['units'][2]['traitApiNames'], raw['units'][2]['traitApiNames']))
                effects = {trait['apiName']: {}} if case == 'modeled-effect' else {}
                with patch.object(tft, 'load_trait_effects', return_value=effects), \
                        self.assertRaisesRegex(tft_update.ReviewRequired, 'new or removed trait'):
                    tft_update.reconcile(candidate, self.previous, notes,
                                         review=self.manifest(candidate, notes, definitionDispositions=records))

    def test_unit_and_trait_removals_remain_refused_with_exact_records(self):
        extra = fixtures.unit('GnarBig')
        extra['shopUnit'] = False
        trait = {'apiName': 'TFT18_Prowler', 'name': 'Prowler', 'units': [], 'effects': [], 'curveTable': {}}
        previous_raw = deepcopy(self.raw)
        previous_raw['units'].append(extra)
        previous_raw['traits'].append(trait)
        self.rebase(previous_raw)
        for kind, value in (('unit', extra), ('trait', trait)):
            with self.subTest(kind=kind):
                raw = deepcopy(previous_raw)
                raw[kind + 's'] = [entity for entity in raw[kind + 's'] if entity['apiName'] != value['apiName']]
                candidate, notes = self.candidate(raw=raw)
                record = self.disposition(value['apiName'], value, None)
                with self.assertRaisesRegex(tft_update.ReviewRequired, f'new or removed {kind}'):
                    tft_update.reconcile(candidate, self.previous, notes,
                                         review=self.manifest(candidate, notes, definitionDispositions=[record]))

    def test_a_new_trait_row_is_acknowledged_by_name_and_value(self):
        raw = deepcopy(self.raw)
        raw['traits'].append({'apiName': 'DA_18_Summoner', 'name': 'Summoner', 'effects': [{'minUnits': 2, 'style': 1}],
                              'curveTable': {'DamageMult': [[0, 1], [1, 1.45]]}})
        self.rebase(raw)
        added = deepcopy(raw)
        added['traits'][-1]['curveTable']['AzirDamageMult'] = [[0, 1], [1, 1.2]]
        candidate, notes = self.candidate(raw=added)
        with self.assertRaisesRegex(tft_update.ReviewRequired, r"new or missing curve rows at DA_18_Summoner.curveTable: \['AzirDamageMult'\]"):
            tft_update.reconcile(candidate, self.previous, notes)
        record = self.disposition('DA_18_Summoner.curveTable.AzirDamageMult', None, [[0, 1], [1, 1.2]])
        self.reviewed(candidate, notes, self.manifest(candidate, notes, definitionDispositions=[record]))

    def test_dropping_a_forms_stats_block_is_checked_not_believed(self):
        def with_form(mana):
            raw = deepcopy(self.raw)
            raw['units'][2]['extraAbilities'] = {'DA_18_Soraka_AP': {
                'variant': 'AP', 'curveTable': {}, 'attributeCalcs': {}, 'stats': {**raw['units'][2]['stats'], 'mana': mana}}}
            return raw

        for mana, message in ((30, None), (25, "the AP form's effective stats change")):
            with self.subTest(mana=mana):
                raw = with_form(mana)
                self.rebase(raw)
                dropped = deepcopy(raw)
                block = dropped['units'][2]['extraAbilities']['DA_18_Soraka_AP'].pop('stats')
                candidate, notes = self.candidate(raw=dropped)
                with self.assertRaisesRegex(tft_update.ReviewRequired, 'definition structure changed'):
                    tft_update.reconcile(candidate, self.previous, notes)
                record = self.disposition('TFT18_Soraka.extraAbilities.DA_18_Soraka_AP.stats', block, None)
                review = self.manifest(candidate, notes, definitionDispositions=[record])
                if message:
                    with self.assertRaisesRegex(tft_update.ReviewRequired, message):
                        tft_update.reconcile(candidate, self.previous, notes, review=review)
                else:
                    self.reviewed(candidate, notes, review)


CDRAGON_STATS = {'hp': 850.0, 'damage': 40.0, 'attackSpeed': 0.75, 'armor': 40.0, 'magicResist': 40.0,
                 'initialMana': 0, 'mana': 30.0, 'range': 4.0}


class TestSourceCrossCheck(Base):
    """A small snapshot with a CommunityDragon export next to it."""
    def setUp(self):
        super().setUp()
        raw = deepcopy(self.raw)
        amumu, yi, soraka = raw['units']
        amumu['assetNames'], soraka['assetNames'] = ['DA_Amumu18'], ['DA_18_Soraka', 'DA_18_Soraka_Blossom']
        yi['assetNames'] = ['DA_18_MasterYi_AD', 'DA_18_MasterYi_AP']
        yi['curveTable']['AutoAttackDamageAP'] = [[1, 15], [2, 22.5], [3, 33.75], [4, 50.625]]
        self.cdragon = {'champions': [
            {'apiName': 'DA_Amumu18', 'stats': {**CDRAGON_STATS, 'initialMana': 30.0, 'mana': 125.0}},
            {'apiName': 'DA_18_Soraka', 'stats': {**CDRAGON_STATS, 'attackSpeed': 0.75000001192092896}},
            {'apiName': 'DA_18_Soraka_Blossom', 'stats': {**CDRAGON_STATS, 'armor': 45.0}},
            {'apiName': 'DA_18_MasterYi_AD', 'stats': {**CDRAGON_STATS, 'armor': 55.0, 'magicResist': 55.0}},
            {'apiName': 'DA_18_MasterYi_AP', 'stats': {**CDRAGON_STATS, 'damage': 15.0, 'armor': 55.0, 'magicResist': 55.0}},
        ], 'traits': [], 'items': []}
        self.rebase(raw)
        self.previous = self.with_cdragon(self.previous, self.cdragon)

    def with_cdragon(self, snap, cdragon):
        (Path(snap.dir) / 'communitydragon.json').write_text(json.dumps(cdragon))
        return tft.Snapshot(18, snap.patch, directory=snap.dir)

    def staged(self, cdragon, **kwargs):
        candidate, notes = self.candidate(**kwargs)
        return self.with_cdragon(candidate, cdragon), notes

    def disagreeing(self, **stats):
        cdragon = deepcopy(self.cdragon)
        cdragon['champions'][1]['stats'].update(stats)
        return cdragon

    def test_forms_variants_float_noise_and_missing_values(self):
        result = tft.source_disagreements(self.previous)
        self.assertEqual((result['compared'], result['unmodeled'], result['disagreements']), (4, ['DA_18_Soraka_Blossom'], []))
        cdragon = deepcopy(self.cdragon)
        cdragon['champions'][1]['stats'].update(hp=950.0, damage=None)
        cdragon['champions'][4]['stats']['damage'] = 60.0       # the AP form attacks with AutoAttackDamageAP (15), not stats.damage
        found = tft.source_disagreements(self.with_cdragon(self.previous, cdragon))['disagreements']
        self.assertEqual([(d['api'], d['asset'], d['form'], d['stat'], d['effective'], d['communitydragon']) for d in found], [
            ('TFT18_MasterYi', 'DA_18_MasterYi_AP', 'AP', 'ad', 15.0, 60.0),
            ('TFT18_Soraka', 'DA_18_Soraka', None, 'hp', 850, 950.0),
            ('TFT18_Soraka', 'DA_18_Soraka', None, 'ad', 40.0, None)])

    def record(self, **values):
        return {'api': 'TFT18_Soraka', 'asset': 'DA_18_Soraka', 'stat': 'hp', 'effective': 850, 'communitydragon': 950.0,
                'disposition': 'communitydragon-lags', 'evidence': ['Riot: Soraka Health 950 ⇒ 850.'], 'reason': 'CommunityDragon is behind.', **values}

    def test_a_new_disagreement_stops_an_unattended_run_and_a_review_explains_it(self):
        candidate, notes = self.staged(self.disagreeing(hp=950.0))
        with self.assertRaisesRegex(tft_update.ReviewRequired, r'unexplained lookup/CommunityDragon disagreement: Soraka hp \(DA_18_Soraka\): snapshot 850, CommunityDragon 950'):
            tft_update.reconcile(candidate, self.previous, notes)
        _, audit = tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, sourceDisagreements=[self.record()]))
        explained = audit['sourceCrossCheck']['explained']
        self.assertEqual([(e['stat'], e['disposition'], e['evidence']) for e in explained], [('hp', 'communitydragon-lags', ['Riot: Soraka Health 950 ⇒ 850.'])])
        self.assertEqual((audit['sourceCrossCheck']['comparedRecords'], audit['sourceCrossCheck']['unmodeledRecords']), (4, ['DA_18_Soraka_Blossom']))

    def test_a_review_must_describe_the_disagreement_that_exists(self):
        candidate, notes = self.staged(self.disagreeing(hp=950.0))
        for message, records in (('no longer matches the staged values', [self.record(communitydragon=900.0)]),
                                 ('the staged data does not have', [self.record(), self.record(stat='armor', effective=40, communitydragon=45.0)]),
                                 ('needs a unique api/asset/stat', [self.record(), self.record()]),
                                 ('needs evidence', [self.record(evidence=[])]), ('needs disposition', [self.record(disposition='')]),
                                 ('unexplained lookup/CommunityDragon disagreement', [])):
            with self.subTest(message=message), self.assertRaisesRegex(tft_update.ReviewRequired, message):
                tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, sourceDisagreements=records))

    def test_what_the_published_snapshot_already_held_does_not_block_an_unrelated_hotfix(self):
        self.previous = self.with_cdragon(self.previous, self.disagreeing(hp=950.0))
        candidate, notes = self.staged(self.disagreeing(hp=950.0), changes=[fixtures.change('Amumu Heal Max HP %', '2.5%', '3%')])
        overrides, audit = tft_update.reconcile(candidate, self.previous, notes)
        self.assertEqual(audit['sourceCrossCheck']['explained'], [])
        self.assertEqual([(i['stat'], i['since']) for i in audit['sourceCrossCheck']['inherited']], [('hp', '18.1d')])
        # ...but a person reviewing the transition has to deal with it
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'the review needs a sourceDisagreements record'):
            tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes))
        # a value that moved is a new disagreement even on an unattended run
        candidate, notes = self.staged(self.disagreeing(hp=975.0), changes=[fixtures.change('Amumu Heal Max HP %', '2.5%', '3%')])
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'CommunityDragon 975'):
            tft_update.reconcile(candidate, self.previous, notes)

    def test_an_explanation_is_carried_while_both_values_stand_and_the_audit_stays_stable(self):
        candidate, notes = self.staged(self.disagreeing(hp=950.0))
        overrides, audit = tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, sourceDisagreements=[self.record()]))
        published = self.with_cdragon(self.snapshot('published', candidate.raw, notes, overrides, audit), self.disagreeing(hp=950.0))
        _, again = tft_update.reconcile(published, published, notes)
        self.assertEqual(again, audit)
        self.assertEqual([d['status'] for d in tft.check_sources(published)['disagreements']], ['explained'])
        gone = self.with_cdragon(self.snapshot('caught-up', candidate.raw, notes, overrides, audit), self.cdragon)
        _, later = tft_update.reconcile(gone, published, notes)
        self.assertEqual(later['sourceCrossCheck']['explained'], [])

    def check(self, snap):
        out = io.StringIO()
        with patch.object(tft, 'load_snapshot', return_value=snap), contextlib.redirect_stdout(out):
            try:
                tft.cmd_check(SimpleNamespace(set=18, patch=None))
                return 0, out.getvalue()
            except SystemExit as stop:
                return stop.code, out.getvalue()

    def test_tft_check_reports_every_disagreement_and_fails_on_one_a_reconciled_audit_does_not_know(self):
        older = self.with_cdragon(self.previous, self.disagreeing(hp=950.0))
        code, text = self.check(older)          # an audit from before the cross-check: reported, not failed
        self.assertEqual(code, 0)
        self.assertIn('SRC   Soraka         hp           snapshot 850 ≠ CommunityDragon 950', text)
        self.assertIn('This audit predates the source cross-check', text)
        candidate, notes = self.staged(self.disagreeing(hp=950.0))
        overrides, audit = tft_update.reconcile(candidate, self.previous, notes, review=self.manifest(candidate, notes, sourceDisagreements=[self.record()]))
        published = self.with_cdragon(self.snapshot('published', candidate.raw, notes, overrides, audit), self.disagreeing(hp=950.0))
        code, text = self.check(published)
        self.assertEqual(code, 0)
        self.assertIn('ok    Soraka         hp           snapshot 850 ≠ CommunityDragon 950 (DA_18_Soraka)  communitydragon-lags', text)
        drifted = self.with_cdragon(published, self.disagreeing(hp=950.0, armor=45.0))
        code, text = self.check(drifted)
        self.assertEqual(code, 2)
        self.assertIn('SRC   Soraka         armor', text)


def _parser_entry(text, *, parent="", section="UNITS: TIER 3", major="LARGE CHANGES", update=""):
    return {"text": text, "parent": parent, "section": section, "major": major, "update": update}


class TestPatchNotesParser(unittest.TestCase):
    def test_real_18_2_nested_form_labels_do_not_absorb_numeric_values(self):
        cases = [
            ("Master Yi AP Form: Ability Damage: 140/210/335 AP ⇒ 125/190/285 AP",
             "Master Yi AP Form: Ability Damage", "140/210/335 AP", "125/190/285 AP"),
            ("Master Yi AD Form: Base AD: 65 ⇒ 60",
             "Master Yi AD Form: Base AD", "65", "60"),
            ("Nidalee AP Form: Nidalee Empowered Attack Damage: 285/425 AP ⇒ 300/450 AP",
             "Nidalee AP Form: Nidalee Empowered Attack Damage", "285/425 AP", "300/450 AP"),
        ]
        for text, what, old, new in cases:
            with self.subTest(what=what):
                parsed = tft.patch_entry_changes(_parser_entry(text))
                self.assertEqual(parsed, [{"what": what, "old": old, "new": new,
                    "section": "UNITS: TIER 3", "major": "LARGE CHANGES", "update": ""}])

    def test_html_inline_formatting_preserves_form_label_and_mana_pair(self):
        document = tft.patch_notes_document("""
            <h2>LARGE CHANGES</h2><h4>UNITS: TIER 1</h4><ul>
              <li><strong>Akali AD Form</strong> Mana: 0/30 &rArr; 0/25</li>
              <li>Leona Mana: 40/100 ⇒ 30/90</li>
            </ul><h4>UNITS: TIER 3</h4><ul>
              <li>Master Yi <em>AP Form</em>: Ability Damage:
                  140/210/335 AP ⇒ 125/190/285 AP</li>
            </ul>""", "18.2")
        changes = document["changes"]
        self.assertEqual([(c["what"], c["old"], c["new"]) for c in changes], [
            ("Akali AD Form Mana", "0/30", "0/25"),
            ("Leona Mana", "40/100", "30/90"),
            ("Master Yi AP Form: Ability Damage", "140/210/335 AP", "125/190/285 AP"),
        ])
        self.assertEqual([c["section"] for c in changes], ["UNITS: TIER 1"] * 2 + ["UNITS: TIER 3"])
        self.assertTrue(all(c["major"] == "LARGE CHANGES" and c["update"] == "" for c in changes))

    def test_compound_numeric_statements_keep_each_label_and_shared_parent(self):
        document = tft.patch_notes_document("""
            <h2>LARGE CHANGES</h2><h4>UNITS: TIER 5</h4><ul><li>Taric:
              <ul><li>Passive Shield: 175/350 + 10% max HP ⇒ 100/225 + 15% max HP.
                  Heal: 200/300 AP ⇒ 250/375 AP</li></ul>
            </li></ul>""", "18.2")
        self.assertEqual(document["changes"], [
            {"what": "Taric Passive Shield", "old": "175/350 + 10% max HP",
             "new": "100/225 + 15% max HP", "section": "UNITS: TIER 5",
             "major": "LARGE CHANGES", "update": ""},
            {"what": "Taric Heal", "old": "200/300 AP", "new": "250/375 AP",
             "section": "UNITS: TIER 5", "major": "LARGE CHANGES", "update": ""},
        ])
        compound = next(n for n in document["notes"] if n["text"].startswith("Passive Shield:"))
        self.assertEqual(compound["parent"], "Taric")
        self.assertIn(". Heal:", compound["text"])
        self.assertEqual(tft.patch_entry_changes(compound), document["changes"])

    def test_deep_three_star_headings_and_sibling_scope_are_retained(self):
        document = tft.patch_notes_document("""
            <h2>SMALL CHANGES</h2><h4>UNITS</h4><ul>
              <li>3-star 4-costs:<ul><li>Nidalee:<ul><li>AD Form:
                <ul><li>Ability Damage: 2500% AD ⇒ 3000% AD</li></ul>
              </li></ul></li></ul></li>
              <li>3-star 5-costs:<ul><li>Gnar:<ul>
                <li>Rage Per Attack: 5 ⇒ 20</li>
              </ul></li></ul></li>
            </ul>""", "18.2")
        self.assertEqual([(c["what"], c["old"], c["new"]) for c in document["changes"]], [
            ("3-star 4-costs Nidalee AD Form Ability Damage", "2500% AD", "3000% AD"),
            ("3-star 5-costs Gnar Rage Per Attack", "5", "20"),
        ])
        self.assertTrue(all(c["section"] == "UNITS" and c["major"] == "SMALL CHANGES"
                            for c in document["changes"]))
        leaf = next(n for n in document["notes"] if n["text"].startswith("Ability Damage:"))
        self.assertEqual(leaf["parent"], "3-star 4-costs Nidalee AD Form")

    def test_deep_parents_retain_mid_patch_date_without_leaking_into_main_article(self):
        document = tft.patch_notes_document("""
            <h2>Mid-Patch Updates</h2><h3>SEPTEMBER 10TH</h3><h4>UNITS</h4>
            <ul><li>3-star 4-costs:<ul><li>Nidalee AD Form:
              <ul><li>Ability Damage: 2500% AD ⇒ 3000% AD</li></ul>
            </li></ul></li></ul>
            <h2>LARGE CHANGES</h2><h4>ITEMS</h4>
            <ul><li>Bloodthirster Trigger Health: 40% ⇒ 50%</li></ul>""", "18.2")
        self.assertEqual(document["patch"], "18.2b")
        self.assertEqual(document["updates"], ["SEPTEMBER 10TH"])
        hotfix, main = document["changes"]
        self.assertEqual((hotfix["what"], hotfix["update"], hotfix["major"]),
                         ("3-star 4-costs Nidalee AD Form Ability Damage", "SEPTEMBER 10TH", "Mid-Patch Updates"))
        self.assertEqual((main["what"], main["update"], main["major"]),
                         ("Bloodthirster Trigger Health", "", "LARGE CHANGES"))

    REAL_18_2_MID_PATCH = """
        <h1>Teamfight Tactics patch 18.2</h1>
        <h2>MID-PATCH UPDATE</h2><h4>SEPTEMBER 14</h4>
        <h4>SYSTEMS</h4><ul><li>XP From Level 8-9: 64 ⇒ 68</li><li>XP From Level 9-10: 64 ⇒ 68</li></ul>
        <h4>UNITS</h4><ul>
          <li>Camille Ability Damage: 160/240/410/700 AD ⇒ 150/225/375/640 AD</li>
          <li>Brambleback: Fixed a bug where his baseline Armor Ignore was higher than intended.</li>
          <li>Draven Bounty Hunter:<ul><li>Casts for 4-costs: 5 ⇒ 6</li></ul></li>
        </ul>
        <h2>18.2 PERFORMANCE/STABILITY IMPROVEMENTS</h2><ul><li>Engine Load/Start-up: faster.</li></ul>
        <h2>SYSTEMS</h2><h4>XP PER LEVEL</h4><ul><li>Level 8 to Level 9: 68 ⇒ 64</li></ul>
        <h2>LARGE CHANGES</h2><h4>UNITS: TIER 4</h4><ul><li>Ashe Arrow Damage: 440/660 AD ⇒ 465/700 AD</li></ul>"""

    def test_real_18_2_singular_heading_with_the_date_as_h4_is_a_dated_update(self):
        # 18.1 wrote <h2>Mid-Patch Updates</h2><h3>AUGUST 27TH</h3><h4>CHAMPIONS</h4>. 18.2 wrote the
        # shape above. Unrecognised, it left the hotfix labelled 18.2 with an empty update list.
        document = tft.patch_notes_document(self.REAL_18_2_MID_PATCH, "18.2")
        self.assertEqual((document["patch"], document["updates"]), ("18.2b", ["SEPTEMBER 14"]))
        by_label = {c["what"]: c for c in document["changes"]}
        for what, section in [("XP From Level 8-9", "SYSTEMS"), ("Camille Ability Damage", "UNITS"),
                              ("Draven Bounty Hunter Casts for 4-costs", "UNITS")]:
            self.assertEqual((by_label[what]["update"], by_label[what]["section"], by_label[what]["major"]),
                             ("SEPTEMBER 14", section, "MID-PATCH UPDATE"))
        # the date is never mistaken for a section, and nothing leaks into the article below it
        self.assertNotIn("SEPTEMBER 14", {n["section"] for n in document["notes"]})
        for what in ("Level 8 to Level 9", "Ashe Arrow Damage"):
            self.assertEqual(by_label[what]["update"], "")
        performance = next(n for n in document["notes"] if n["text"].startswith("Engine Load"))
        self.assertEqual((performance["update"], performance["major"]), ("", "18.2 PERFORMANCE/STABILITY IMPROVEMENTS"))

    def test_only_a_date_promotes_a_lower_heading_and_only_inside_a_mid_patch_block(self):
        html = self.REAL_18_2_MID_PATCH.replace("<h4>SEPTEMBER 14</h4>", "<h4>MAYHEM CHANGES</h4>")
        document = tft.patch_notes_document(html, "18.2")
        self.assertEqual((document["patch"], document["updates"]), ("18.2", []))
        elsewhere = """<h2>LARGE CHANGES</h2><h4>SEPTEMBER 14</h4>
            <ul><li>Ashe Arrow Damage: 440/660 AD ⇒ 465/700 AD</li></ul>"""
        document = tft.patch_notes_document(elsewhere, "18.2")
        self.assertEqual((document["patch"], document["changes"][0]["section"], document["changes"][0]["update"]),
                         ("18.2", "SEPTEMBER 14", ""))

    def test_real_18_4_h4_ordinal_date_labels_the_b_patch_and_keeps_main_changes_separate(self):
        # Extracted from Riot's 18.4 article on 2026-10-11:
        # https://teamfighttactics.leagueoflegends.com/en-us/news/game-updates/teamfight-tactics-patch-18-4/
        # The ordinal suffix used to prevent h4 dates from matching, unlike h3 dates.
        document = tft.patch_notes_document("""
            <h2 id="patch-midpatch-updates">MID-PATCH UPDATE</h2>
            <h4 class="change-detail-title">OCTOBER 7TH</h4>
            <h4 class="change-detail-title">2 COST UNIT</h4><ul>
              <li>Alistar Spell Damage: 100/150/225 AP ⇒ 180/270/420 AP</li>
              <li>Gromp AP Form Splash Damage: 160/240/375 AP ⇒ 180/270/435 AP</li>
            </ul>
            <h2 id="patch-large-changes">LARGE CHANGES</h2>
            <h4 class="change-detail-title">UNITS: TIER 2</h4><ul><li>Gromp<ul>
              <li>Splash damage AP Form: 160/240/360/610 ⇒ 160/240/375/625</li>
            </ul></li></ul>""", "18.4")
        self.assertEqual((document["patch"], document["updates"]), ("18.4b", ["OCTOBER 7TH"]))
        self.assertEqual(document["revisionSource"], "dated mid-patch update sequence")
        self.assertEqual([(c["what"], c["update"], c["major"]) for c in document["changes"]], [
            ("Alistar Spell Damage", "OCTOBER 7TH", "MID-PATCH UPDATE"),
            ("Gromp AP Form Splash Damage", "OCTOBER 7TH", "MID-PATCH UPDATE"),
            ("Gromp Splash damage AP Form", "", "LARGE CHANGES"),
        ])
        # The article puts the hotfix first. Retain this source order and its date,
        # allowing the reconciler to apply the base 375 value before the hotfix's 435.
        self.assertEqual([(c["old"], c["new"], c["update"]) for c in document["changes"][1:]], [
            ("160/240/375 AP", "180/270/435 AP", "OCTOBER 7TH"),
            ("160/240/360/610", "160/240/375/625", ""),
        ])
        self.assertNotIn("OCTOBER 7TH", {n["section"] for n in document["notes"]})

    def test_h4_ordinal_updates_preserve_newest_first_sequence_and_reset_main_scope(self):
        document = tft.patch_notes_document("""
            <h2>MID-PATCH UPDATE</h2>
            <h4>OCTOBER 11TH</h4><h4>UNITS</h4><ul><li>Varus Mana: 20/110 ⇒ 10/100</li></ul>
            <h4>OCTOBER 7TH</h4><h4>UNITS</h4><ul><li>Varus Mana: 30/120 ⇒ 20/110</li></ul>
            <h2>LARGE CHANGES</h2><h4>UNITS: TIER 1</h4><ul><li>Varus Mana: 40/130 ⇒ 30/120</li></ul>
            """, "18.4")
        self.assertEqual((document["patch"], document["updates"]),
                         ("18.4c", ["OCTOBER 11TH", "OCTOBER 7TH"]))
        self.assertEqual([c["update"] for c in document["changes"]], ["OCTOBER 11TH", "OCTOBER 7TH", ""])
        self.assertEqual(document["changes"][-1]["section"], "UNITS: TIER 1")

    def test_h4_dates_allow_each_ordinal_suffix_without_accepting_longer_words(self):
        for date in ("OCTOBER 1ST", "OCTOBER 2ND", "OCTOBER 3RD", "october 7th"):
            with self.subTest(date=date):
                document = tft.patch_notes_document(f"""
                    <h2>MID-PATCH UPDATE</h2><h4>{date}</h4><h4>UNITS</h4>
                    <ul><li>Varus Mana: 30/120 ⇒ 20/110</li></ul>""", "18.4")
                self.assertEqual((document["patch"], document["updates"]), ("18.4b", [date]))
        document = tft.patch_notes_document("""
            <h2>MID-PATCH UPDATE</h2><h4>OCTOBER 7THING</h4>
            <ul><li>Varus Mana: 30/120 ⇒ 20/110</li></ul>""", "18.4")
        self.assertEqual((document["patch"], document["updates"]), ("18.4", []))

    def test_numeric_ratio_colon_is_kept_for_review_instead_of_becoming_a_label(self):
        parsed = tft.patch_entry_changes(_parser_entry("Damage Ratio: 1:2 AD/AP ⇒ 2:3 AD/AP", parent="Champion AP Form"))
        self.assertEqual(len(parsed), 1)
        self.assertEqual((parsed[0]["what"], parsed[0]["old"], parsed[0]["new"]),
                         ("Champion AP Form Damage Ratio", "1:2 AD/AP", "2:3 AD/AP"))

    def test_nested_label_stops_before_signed_or_decimal_numeric_expression(self):
        for old, new in (("-1:2 AD/AP", "-2:3 AD/AP"), (".5:1 AD/AP", ".75:1 AD/AP")):
            with self.subTest(old=old):
                parsed = tft.patch_entry_changes(_parser_entry(f"Champion AP Form: Ratio: {old} ⇒ {new}"))
                self.assertEqual((parsed[0]["what"], parsed[0]["old"], parsed[0]["new"]),
                                 ("Champion AP Form: Ratio", old, new))

    def test_mechanics_suffix_is_not_silently_removed_from_new_value(self):
        parsed = tft.patch_entry_changes(_parser_entry(
            "Champion AP Form: Damage: 140/210 AP ⇒ 125/190 AP and now stuns the target"))
        self.assertEqual(parsed[0]["old"], "140/210 AP")
        self.assertEqual(parsed[0]["new"], "125/190 AP and now stuns the target")

    def test_real_18_2_xp_level_changes_keep_systems_context(self):
        document = tft.patch_notes_document("""
            <h2>SYSTEMS</h2><h4>XP PER LEVEL</h4><ul>
              <li>Level 7 to Level 8: 60 ⇒ 56</li>
              <li>Level 8 to Level 9: 68 ⇒ 64</li>
              <li>Level 9 to Level 10: 68 ⇒ 64</li>
            </ul><h2>LARGE CHANGES</h2><h4>TRAITS</h4>
            <ul><li>Hunter Targeting Duration for Damage Amp: 4 seconds ⇒ 3 seconds</li></ul>""", "18.2")
        xp, hunter = document["changes"][:3], document["changes"][3]
        self.assertEqual([(c["what"], c["old"], c["new"]) for c in xp], [
            ("Level 7 to Level 8", "60", "56"),
            ("Level 8 to Level 9", "68", "64"),
            ("Level 9 to Level 10", "68", "64"),
        ])
        self.assertTrue(all(c["section"] == "XP PER LEVEL" and c["major"] == "SYSTEMS" for c in xp))
        self.assertEqual((hunter["section"], hunter["major"]), ("TRAITS", "LARGE CHANGES"))

    def test_mechanics_only_leaf_remains_available_for_review(self):
        document = tft.patch_notes_document("""
            <h2>LARGE CHANGES</h2><h4>UNITS: TIER 4</h4><ul>
              <li>Sentinel Now targets the largest line of enemies. No longer required to include his current target</li>
              <li>Sentinel Ability Shield: 400/500 AP ⇒ 350/450 AP</li>
            </ul>""", "18.2")
        self.assertEqual(len(document["changes"]), 1)
        mechanics = next(n for n in document["notes"] if n["text"].startswith("Sentinel Now"))
        self.assertEqual(tft.patch_entry_changes(mechanics), [])
        self.assertEqual((mechanics["section"], mechanics["major"]), ("UNITS: TIER 4", "LARGE CHANGES"))


class TestBoundReview(unittest.TestCase):
    setUp = fixtures.TestReconcile.setUp
    snapshot = fixtures.TestReconcile.snapshot
    candidate = fixtures.TestReconcile.candidate

    def manifest(self, candidate, notes, mappings=(), dispositions=()):
        return {'schema': 1, 'patch': candidate.patch, 'previousPatch': self.previous.patch,
                'source': notes['url'], 'bindings': {
                    'lookupHash': tft.json_hash(candidate.raw), 'binsHash': tft.json_hash(candidate.bins),
                    'patchNotesHash': tft.json_hash(notes), 'previousAuditHash': tft.json_hash(self.previous.audit)},
                'mappings': list(mappings), 'dispositions': list(dispositions)}

    def mapping(self, line, stars=(1, 2), expected=(250, 375)):
        target = {'kind': 'unit', 'api': 'TFT18_Soraka', 'row': 'DamageAP', 'stars': list(stars)}
        return {'change': line, 'target': target, 'expected': list(expected),
                'observedBefore': tft_update._values(self.previous, target),
                'numericEncoding': {'scale': 1, 'offset': 0},
                'reason': 'Reviewed exact current AP row despite stale previous source values.'}

    def reviewed(self, candidate, notes, review):
        overrides, audit = tft_update.reconcile(candidate, self.previous, notes, review=review)
        checked = self.snapshot('checked', candidate.raw, notes, overrides, audit, candidate.bins)
        findings, _ = tft.check_audit(checked, notes)
        self.assertTrue(all(f['status'] == 'current' for f in findings), findings)
        return checked, audit

    def test_stale_source_requires_bound_review_and_preserves_unmentioned_stars(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        candidate, notes = self.candidate([line])
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'continuity gap'):
            tft_update.reconcile(candidate, self.previous, notes)
        review = self.manifest(candidate, notes, [self.mapping(line)])
        untouched = deepcopy((candidate.__dict__, self.previous.__dict__, notes, review))
        checked, audit = self.reviewed(candidate, notes, review)
        self.assertEqual(tft_update._values(checked, {**self.mapping(line)['target'], 'stars': [1, 2, 3, 4]}),
                         [250, 375, 1000, 2200])
        self.assertEqual(audit['reviews'], [review])
        self.assertEqual(untouched, (candidate.__dict__, self.previous.__dict__, notes, review))

    def test_every_source_binding_is_required(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        candidate, notes = self.candidate([line])
        for binding in ('lookupHash', 'binsHash', 'patchNotesHash', 'previousAuditHash'):
            with self.subTest(binding=binding):
                review = self.manifest(candidate, notes, [self.mapping(line)])
                review['bindings'][binding] = 'wrong'
                with self.assertRaisesRegex(tft_update.ReviewRequired, 'manifest hashes'):
                    self.reviewed(candidate, notes, review)

    def test_wrong_transition_or_source_is_rejected(self):
        candidate, notes = self.candidate()
        for key, value in [('patch', '18.3'), ('previousPatch', '18.1c'), ('source', 'https://example.com')]:
            with self.subTest(key=key):
                review = self.manifest(candidate, notes)
                review[key] = value
                with self.assertRaises(tft_update.ReviewRequired):
                    self.reviewed(candidate, notes, review)

    def test_baseline_values_cannot_be_silently_replaced(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        candidate, notes = self.candidate([line])
        mapping = self.mapping(line)
        mapping['observedBefore'] = [230, 345]
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'baseline no longer matches'):
            self.reviewed(candidate, notes, self.manifest(candidate, notes, [mapping]))

    def test_exact_evidence_including_major_and_punctuation_is_required(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        candidate, notes = self.candidate([line])
        for field, value in [('major', 'Small Changes'), ('new', '250/375AP'), ('what', 'Soraka Initial Star Damage!')]:
            with self.subTest(field=field):
                mapping = deepcopy(self.mapping(line))
                mapping['change'][field] = value
                with self.assertRaisesRegex(tft_update.ReviewRequired, 'absent from the exact'):
                    self.reviewed(candidate, notes, self.manifest(candidate, notes, [mapping]))

    def test_review_does_not_hide_unrelated_raw_change(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        raw = deepcopy(self.raw)
        raw['units'][0]['stats']['hp'] = 999
        candidate, notes = self.candidate([line], raw=raw)
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'base-stat change'):
            self.reviewed(candidate, notes, self.manifest(candidate, notes, [self.mapping(line)]))

    def test_review_does_not_hide_unknown_mechanics(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        candidate, notes = self.candidate([line], bullets=[{'text': 'Soraka now burns all enemies.', 'section': 'BUG FIXES'}])
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'mechanics bullet'):
            self.reviewed(candidate, notes, self.manifest(candidate, notes, [self.mapping(line)]))

    def test_exact_mechanics_disposition_is_recorded(self):
        note = {'text': 'Soraka now selects the farthest ally.', 'section': 'BUG FIXES'}
        candidate, notes = self.candidate(bullets=[note])
        disposition = {'note': note, 'disposition': 'retained-combat-approximation',
                       'reason': 'The single-actor model counts ally shielding but does not place allies.'}
        _, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, dispositions=[disposition]))
        self.assertIn(disposition, audit['automatic']['outOfScope'])

    def test_note_disposition_does_not_skip_an_unreviewed_numeric_change(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        candidate, notes = self.candidate([line])
        disposition = {'note': fixtures.entry(line), 'disposition': 'reviewed', 'reason': 'A note is not a numeric mapping.'}
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'continuity gap'):
            self.reviewed(candidate, notes, self.manifest(candidate, notes, dispositions=[disposition]))

    def test_tooltip_correction_uses_exact_nonnumeric_note(self):
        note = {'text': 'Soraka tooltip now correctly states 250/375 damage; functionality unchanged.', 'section': 'BUG FIXES'}
        candidate, notes = self.candidate(bullets=[note])
        mapping = self.mapping({})
        del mapping['change']
        mapping['note'] = note
        checked, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, [mapping]))
        self.assertEqual(tft_update._values(checked, mapping['target']), [250, 375])
        self.assertTrue(audit['checks'][-1]['manualOnly'])

    def test_overlapping_check_preserves_unaffected_coordinates_and_history(self):
        line = fixtures.change('Soraka Two Star Damage', '345 AP', '375 AP')
        candidate, notes = self.candidate([line])
        mapping = self.mapping(line, stars=[2], expected=[375])
        checked, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, [mapping]))
        damage = [c for c in audit['checks'] if c['target'].get('row') == 'DamageAP']
        self.assertEqual([c['target']['stars'] for c in damage], [[1], [2]])
        self.assertEqual([c['expected'] for c in damage], [[225], [375]])
        self.assertEqual(damage[-1]['supersededChecks'][0]['expected'], [225, 335])

    def test_simple_review_mapping_is_reusable_for_future_numeric_update(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        candidate, notes = self.candidate([line])
        checked, _ = self.reviewed(candidate, notes, self.manifest(candidate, notes, [self.mapping(line)]))
        future_line = fixtures.change(line['what'], '250/375 AP', '260/390 AP', update='SEPTEMBER 4TH')
        future_notes = deepcopy(notes)
        future_notes['changes'].insert(0, future_line)
        future_notes['notes'].insert(0, fixtures.entry(future_line))
        future_notes['updates'].insert(0, 'SEPTEMBER 4TH')
        future = self.snapshot('future', candidate.raw, future_notes, checked.overrides, checked.audit)
        overrides, audit = tft_update.reconcile(future, checked, future_notes)
        self.assertEqual(overrides['units']['TFT18_Soraka']['curve']['DamageAP'], [260, 390, 1000, 2200])

    def test_compound_review_never_becomes_a_guessed_automatic_mapping(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP + 10% HP', '250/375 AP + 15% HP')
        candidate, notes = self.candidate([line])
        checked, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, [self.mapping(line)]))
        self.assertTrue(audit['checks'][-1]['manualOnly'])
        future_line = fixtures.change(line['what'], line['new'], '270/405 AP + 15% HP', update='SEPTEMBER 4TH')
        future_notes = deepcopy(notes)
        future_notes['changes'].insert(0, future_line)
        future_notes['notes'].insert(0, fixtures.entry(future_line))
        future_notes['updates'].insert(0, 'SEPTEMBER 4TH')
        future = self.snapshot('future', candidate.raw, future_notes, checked.overrides, checked.audit)
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'decomposed expression'):
            tft_update.reconcile(future, checked, future_notes)

    def test_future_array_extension_cannot_leave_newly_listed_stars_stale(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        candidate, notes = self.candidate([line])
        checked, _ = self.reviewed(candidate, notes, self.manifest(candidate, notes, [self.mapping(line)]))
        future_line = fixtures.change(line['what'], '250/375/1000 AP', '260/390/1100 AP', update='SEPTEMBER 4TH')
        future_notes = deepcopy(notes)
        future_notes['changes'].insert(0, future_line)
        future_notes['notes'].insert(0, fixtures.entry(future_line))
        future_notes['updates'].insert(0, 'SEPTEMBER 4TH')
        future = self.snapshot('future', candidate.raw, future_notes, checked.overrides, checked.audit)
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'outside the reviewed scope'):
            tft_update.reconcile(future, checked, future_notes)

    def test_augment_note_cannot_reuse_a_champion_mapping_with_the_same_label(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        candidate, notes = self.candidate([line])
        checked, _ = self.reviewed(candidate, notes, self.manifest(candidate, notes, [self.mapping(line)]))
        future_line = fixtures.change(line['what'], '250/375 AP', '260/390 AP', update='SEPTEMBER 4TH', section='AUGMENTS')
        future_notes = deepcopy(notes)
        future_notes['changes'].insert(0, future_line)
        future_notes['notes'].insert(0, fixtures.entry(future_line))
        future_notes['updates'].insert(0, 'SEPTEMBER 4TH')
        future = self.snapshot('future', candidate.raw, future_notes, checked.overrides, checked.audit)
        overrides, audit = tft_update.reconcile(future, checked, future_notes)
        self.assertEqual(overrides['units']['TFT18_Soraka']['curve']['DamageAP'], [250, 375, 1000, 2200])
        self.assertIn('outside', audit['automatic']['outOfScope'][0]['reason'])

    def test_successive_checks_do_not_recursively_duplicate_history(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        check = {'what': line['what'], 'target': self.mapping(line)['target'], 'expected': [250, 375]}
        checks = [check]
        for step in range(8):
            updated = deepcopy(checks[-1])
            updated['expected'] = [251 + step, 376 + step]
            tft_update._replace_check(checks, updated)
        history = checks[-1]['supersededChecks']
        self.assertEqual(len(history), 8)
        self.assertTrue(all('supersededChecks' not in old for old in history))

    def test_excluded_stage_review_retires_only_changed_field_checks(self):
        api = 'DA_Artifact_Reviewed'
        self.raw['items'].append({'apiName': api, 'name': 'Reviewed Artifact',
                                 'curveTable': {'Damage': [[1, 30], [3, 50]], 'Health': [[1, 300]]}})
        for row, column, expected in [('Damage', 1, 30), ('Damage', 3, 50), ('Health', 1, 300)]:
            self.audit['checks'].append({'what': f'Reviewed Artifact {row}',
                'target': {'kind': 'item', 'api': api, 'row': row, 'columns': [column]}, 'expected': [expected]})
        self.audit['lookupHash'] = tft.json_hash(self.raw)
        self.previous = self.snapshot('previous', self.raw, self.notes, self.overrides, self.audit)
        line = fixtures.change('Reviewed Artifact Damage', '30/50 (stages 2–4)', '25/45', section='ARTIFACTS')
        candidate, notes = self.candidate([line])
        disposition = {'change': line, 'disposition': 'excluded-item-stage-array',
                       'reason': 'The stage encoding is unresolved, and this artifact is excluded from both pools.',
                       'retireTargets': [{'kind': 'item', 'api': api, 'row': 'Damage'}]}
        _, audit = self.reviewed(candidate, notes, self.manifest(candidate, notes, dispositions=[disposition]))
        self.assertEqual([c['target']['row'] for c in audit['checks'] if c['target']['api'] == api], ['Health'])
        retired = [c for c in audit['outOfScopeChecks'] if c['target']['api'] == api]
        self.assertEqual([c['expected'] for c in retired], [[30], [50]])
        self.assertTrue(all(c['retiredAtPatch'] == candidate.patch for c in retired))

    def test_review_cannot_retire_modeled_values_without_replacements(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        candidate, notes = self.candidate([line])
        disposition = {'change': line, 'disposition': 'unsupported', 'reason': 'Retirement is not approval to skip modeled changes.',
                       'retireTargets': [self.mapping(line)['target']]}
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'only an excluded item field'):
            self.reviewed(candidate, notes, self.manifest(candidate, notes, dispositions=[disposition]))

    def test_flat_and_percentage_units_cannot_reuse_the_same_numeric_value(self):
        self.audit['checks'].append({'what': 'Soraka Health',
            'target': {'kind': 'unit', 'api': 'TFT18_Soraka', 'stat': 'hp'}, 'expected': [850],
            'numericEncoding': {'scale': 1, 'offset': 0},
            'patchLine': {'what': 'Soraka Health', 'old': '800 HP', 'new': '850 HP'}})
        self.previous = self.snapshot('previous', self.raw, self.notes, self.overrides, self.audit)
        for old, new in [('850 HP', '850% HP'), ('850% HP', '850 HP'),
                         ('850 HP', '850%'), ('850', '850%')]:
            line = fixtures.change('Soraka Health', old, new)
            candidate, notes = self.candidate([line])
            with self.subTest(old=old), self.assertRaisesRegex(tft_update.ReviewRequired, 'percentage/flat units changed'):
                tft_update.reconcile(candidate, self.previous, notes)

    def test_omitted_repeated_unit_suffix_remains_supported(self):
        line = fixtures.change('Soraka Health', '850 HP', '900')
        candidate, notes = self.candidate([line])
        overrides, _ = tft_update.reconcile(candidate, self.previous, notes)
        self.assertEqual(overrides['units']['TFT18_Soraka']['stats']['hp'], 900)

    def test_malformed_target_values_and_duplicate_review_are_rejected(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        candidate, notes = self.candidate([line])
        for value in ([250], [True, 375], [float('inf'), 375]):
            mapping = self.mapping(line)
            mapping['expected'] = value
            with self.subTest(value=value), self.assertRaisesRegex(tft_update.ReviewRequired, 'invalid reviewed expected'):
                self.reviewed(candidate, notes, self.manifest(candidate, notes, [mapping]))
        mapping = self.mapping(line)
        with self.assertRaisesRegex(tft_update.ReviewRequired, 'duplicate reviewed mapping'):
            self.reviewed(candidate, notes, self.manifest(candidate, notes, [mapping, mapping]))

    def test_manifest_file_is_loaded_only_for_a_new_transition(self):
        line = fixtures.change('Soraka Initial Star Damage', '230/345 AP', '250/375 AP')
        candidate, notes = self.candidate([line], patch='18.2', next_minor=True)
        review = self.manifest(candidate, notes, [self.mapping(line)])
        directory = Path(self.previous.dir).parent / 'patch-reviews'
        directory.mkdir()
        (directory / '18.2.json').write_text(json.dumps(review))
        overrides, audit = tft_update.reconcile(candidate, self.previous, notes)
        checked = self.snapshot('checked', candidate.raw, notes, overrides, audit)
        # An unchanged scheduled run must not reapply the old transition review.
        _, again = tft_update.reconcile(checked, checked, notes)
        self.assertEqual(again['reviews'], [review])

    def test_xp_purchase_cost_is_outside_fixed_level_combat_but_other_systems_are_not(self):
        line = {'what': 'Level 7 to Level 8', 'old': '60', 'new': '56', 'update': '',
                'section': 'XP PER LEVEL', 'major': 'SYSTEMS'}
        candidate, notes = self.candidate([line], patch='18.2', next_minor=True)
        _, audit = tft_update.reconcile(candidate, self.previous, notes)
        self.assertIn('XP purchase costs', audit['automatic']['outOfScope'][0]['reason'])
        line['what'] = 'All units attack speed'
        candidate, notes = self.candidate([line], patch='18.2', next_minor=True)
        with self.assertRaises(tft_update.ReviewRequired):
            tft_update.reconcile(candidate, self.previous, notes)

    def test_mid_patch_xp_table_row_is_outside_too(self):
        # Riot's 18.2 SEPTEMBER 14 update: <h2>MID-PATCH UPDATE</h2> ... <h4>SYSTEMS</h4>
        # "XP From Level 8-9: 64 ⇒ 68". It blocked forty scheduled refreshes.
        lines = [fixtures.change(what, '64', '68', update='SEPTEMBER 14', section='SYSTEMS')
                 for what in ('XP From Level 8-9', 'XP From Level 9-10')]
        for line in lines:
            line['major'] = 'MID-PATCH UPDATE'
        candidate, notes = self.candidate(lines)
        overrides, audit = tft_update.reconcile(candidate, self.previous, notes)
        self.assertEqual(overrides, self.overrides)
        # each line is recorded once as a numeric change and once as the bullet it came from
        recorded = audit['automatic']['outOfScope']
        self.assertEqual([r['change']['what'] for r in recorded if 'change' in r], ['XP From Level 8-9', 'XP From Level 9-10'])
        self.assertTrue(all('XP purchase costs' in r['reason'] for r in recorded))

    def test_a_line_that_only_mentions_xp_or_a_level_is_not_the_xp_table(self):
        outside = lambda what, section, major='MID-PATCH UPDATE': tft_update._outside(
            {'what': what, 'section': section, 'major': major}, set())
        self.assertIsNotNone(outside('XP From Level 8-9', 'SYSTEMS'))
        self.assertIsNotNone(outside('Level 8 to Level 9', 'XP PER LEVEL', 'SYSTEMS'))
        for what, section in [
                ('Soraka XP From Level 8-9', 'SYSTEMS'),            # a champion's line, wherever it is filed
                ('XP From Level 8-9', 'UNITS'),                     # the right words under a gameplay heading
                ('Draven Bounty Hunter Level 10 XP Reward', 'UNITS'),
                ('Level 3 Shop Odds', 'SYSTEMS'),                   # a systems line that is no XP row
                ('Level 8 to Level 9 Champion Damage', 'SYSTEMS'),
                ('XP From Level 8-9 Refund on Sell', 'SYSTEMS')]:
            with self.subTest(what=what, section=section):
                self.assertIsNone(outside(what, section))
        # and end to end: a champion line under the mid-patch SYSTEMS heading still needs a review
        line = fixtures.change('Soraka XP From Level 8-9', '64', '68', update='SEPTEMBER 14', section='SYSTEMS')
        candidate, notes = self.candidate([line])
        with self.assertRaises(tft_update.ReviewRequired):
            tft_update.reconcile(candidate, self.previous, notes)


OFFICIAL_SOURCE = (
    "https://teamfighttactics.leagueoflegends.com/en-us/news/game-updates/"
    "teamfight-tactics-patch-18-4/"
)


class TestPatch18_4Review(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        set_dir = Path(tft.TFT_DATA_DIR) / "set18"
        cls.previous = tft.Snapshot(18, "18.3", directory=str(set_dir / "18.3"))
        source = set_dir / "18.4b"
        if not source.is_dir():
            source = set_dir / ".pending" / "18.4b"
        cls.manifest = json.loads(
            (set_dir / "patch-reviews" / "18.4b.json").read_text()
        )
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.staging = Path(cls.temporary.name) / "candidate"
        cls.staging.mkdir()
        cls.input_names = (
            "metatft.json", "meta.json", "bins.json",
            "patchnotes.json", "communitydragon.json",
        )
        for name in cls.input_names:
            shutil.copyfile(source / name, cls.staging / name)
        # A publication's reviewed overrides must not be mistaken for candidate
        # approval: replay using only the previous generation's corrections.
        shutil.copyfile(
            Path(cls.previous.dir) / "overrides.json",
            cls.staging / "overrides.json",
        )
        cls.candidate = tft.Snapshot(18, "18.4b", directory=str(cls.staging))
        cls.notes = json.loads((cls.staging / "patchnotes.json").read_text())
        cls._verified = None

    def reconcile(self, manifest):
        # The retained Ivern approximation can warn during source cross-checks;
        # it is explicitly reviewed and is not relevant to these number oracles.
        with contextlib.redirect_stderr(io.StringIO()):
            return tft_update.reconcile(
                self.candidate, self.previous, self.notes, review=manifest
            )

    def verified(self):
        cls = type(self)
        if cls._verified is None:
            overrides, audit = self.reconcile(self.manifest)
            checked_dir = Path(self.temporary.name) / "checked"
            checked_dir.mkdir()
            for name in self.input_names:
                shutil.copyfile(self.staging / name, checked_dir / name)
            (checked_dir / "overrides.json").write_text(json.dumps(overrides))
            (checked_dir / "audit.json").write_text(json.dumps(audit))
            cls._verified = tft.Snapshot(18, "18.4b", directory=str(checked_dir))
        return cls._verified

    @staticmethod
    def values(entity, row, positions):
        return [tft.curve_at(entity["curve"][row], p) for p in positions]

    def test_review_is_bound_to_the_official_b_update(self):
        self.assertEqual(self.manifest["source"], OFFICIAL_SOURCE)
        self.assertEqual(self.notes["url"], OFFICIAL_SOURCE)
        self.assertEqual(self.notes["patch"], "18.4b")
        self.assertIn("OCTOBER 7TH", self.notes["updates"])
        checked = self.verified()
        self.assertEqual(
            checked.audit["automatic"]["reviewManifestHash"],
            tft.json_hash(self.manifest),
        )
        self.assertEqual(checked.audit["reviews"][-1], self.manifest)

    def test_alistar_damage_reaches_the_ability_calculation(self):
        unit = self.verified().unit("Alistar")
        self.assertEqual(self.values(unit, "Damage", (1, 2, 3)), [180, 270, 420])
        self.assertEqual(self.values(unit, "HealBaseValue", (1, 2, 3)), [230, 300, 400])
        for star, damage in ((1, 180), (2, 270), (3, 420)):
            with self.subTest(star=star):
                terms = tft.kit_spec(unit, star)["calcs"]["MagicDamageCalc1"]["terms"]
                self.assertEqual(len(terms), 1)
                self.assertEqual(terms[0]["scaling"], "AbilityPower")
                self.assertEqual(terms[0]["coef"], damage)

    def test_gromp_b_update_wins_without_overwriting_the_fourth_star(self):
        unit = self.verified().unit("Gromp")
        self.assertEqual(
            self.values(unit, "SecondaryDamageAP", (1, 2, 3, 4)),
            [180, 270, 435, 625],
        )
        for star, damage in ((1, 180), (2, 270), (3, 435)):
            with self.subTest(star=star):
                kit = tft.kit_spec(unit, star, "AP")
                self.assertEqual(kit["calcs"]["MagicDamageCalc2"]["terms"][0]["coef"], damage)
        self.assertEqual(
            self.values(unit["forms"]["AD"], "AutoAttackDamage", (1, 2, 3, 4)),
            [50, 75, 112.5, 168.75],
        )
        for star, ad in ((1, 50), (2, 75), (3, 112.5), (4, 168.75)):
            with self.subTest(ad_star=star):
                kit = tft.kit_spec(unit, star, "AD")
                self.assertEqual(kit["baseAd"], ad)
                self.assertEqual(kit["stats"]["ad"], 50)
                self.assertEqual(kit["stats"]["as"], 0.75)
                self.assertEqual((kit["stats"]["initialMana"], kit["stats"]["mana"]), (0, 70))
        # The AD buff must not turn the AP form's ordinary attacks into AD attacks.
        ap = tft.kit_spec(unit, 1, "AP")
        self.assertEqual(ap["baseAd"], 30)
        self.assertEqual((ap["stats"]["initialMana"], ap["stats"]["mana"]), (0, 45))

    def test_mama_beak_base_attack_and_summon_damage_are_distinct(self):
        unit = self.verified().unit("Mama Beak")
        kit = tft.kit_spec(unit, 2)
        self.assertEqual(unit["stats"]["ad"], 60)
        self.assertEqual(kit["baseAd"], 90)
        self.assertEqual(kit["rows"]["AutoAttackDamage"], 90)
        self.assertEqual(kit["rows"]["MiniDamagePerAttack"], 30)
        self.assertEqual(kit["calcs"]["PhysicalDamageCalc1"]["terms"][0]["coef"], 30)

    def test_nidalee_maps_small_spears_and_swipes_to_their_own_forms(self):
        unit = self.verified().unit("Nidalee")
        self.assertEqual(self.values(unit, "EmpoweredDamage", (1, 2)), [185, 275])
        self.assertEqual(self.values(unit["forms"]["AD"], "SwipeDamage", (1, 2)), [200, 300])
        for star, spear, swipe in ((1, 185, 200), (2, 275, 300)):
            with self.subTest(star=star):
                ap = tft.kit_spec(unit, star, "AP")
                ad = tft.kit_spec(unit, star, "AD")
                self.assertEqual(ap["calcs"]["MagicDamageCalc1"]["terms"][0]["coef"], spear)
                self.assertEqual(ad["calcs"]["PhysicalDamageCalc1"]["terms"][0]["coef"], swipe)
        self.assertEqual(self.values(unit, "ThirdAttackEmpoweredDamage", (1, 2)), [330, 500])
        self.assertEqual(self.values(unit, "EmpoweredDamage", (3, 4)), [2000, 3000])
        self.assertEqual(self.values(unit["forms"]["AD"], "SwipeDamage", (3, 4)), [3000, 5000])

    def test_trait_numeric_encodings_follow_the_official_values(self):
        checked = self.verified()
        self.assertEqual(self.values(checked.traits["DA_18_Fae"], "ADAP", (1, 2)), [4.5, 7])
        self.assertEqual(
            self.values(checked.traits["DA_18_Executioner"], "BonusBleedPercent", (1, 2, 3)),
            [0, 0.25, 0.35],
        )

    def test_unannounced_higher_star_source_changes_do_not_get_accepted(self):
        checked = self.verified()
        self.assertEqual(self.values(checked.unit("Rengar"), "StabDamage", (3, 4)), [650, 1050])
        self.assertEqual(self.values(checked.unit("Tristana"), "AOEDamageBase", (3, 4)), [420, 680])
        self.assertEqual(self.values(checked.unit("Brambleback"), "FrenzyADPercent", (1, 2, 3, 4)), [1, 1, 3, 4])

    def test_all_final_audit_checks_and_source_disagreements_are_reviewed(self):
        checked = self.verified()
        findings, _ = tft.check_audit(checked, self.notes)
        self.assertTrue(findings, "A real archived transition must produce audit checks.")
        self.assertEqual([f for f in findings if f["status"] != "current"], [])
        sources = tft.check_sources(checked)
        self.assertTrue(sources["audited"])
        self.assertEqual(
            [d for d in sources["disagreements"] if d["status"] != "explained"], []
        )
        for disagreement in sources["disagreements"]:
            with self.subTest(api=disagreement["api"], stat=disagreement["stat"]):
                self.assertTrue(disagreement["reason"])
                self.assertTrue(disagreement["disposition"])

    def test_removing_only_alistar_damage_mapping_restores_the_review_blocker(self):
        incomplete = deepcopy(self.manifest)
        removed = [
            mapping for mapping in incomplete["mappings"]
            if mapping.get("change", {}).get("what") == "Alistar Spell Damage"
        ]
        self.assertEqual(len(removed), 1)
        incomplete["mappings"] = [
            mapping for mapping in incomplete["mappings"] if mapping not in removed
        ]
        with self.assertRaisesRegex(
            tft_update.ReviewRequired,
            r"Alistar Spell Damage: ability numbers match zero or multiple curve rows",
        ):
            self.reconcile(incomplete)

    def test_changed_source_binding_is_rejected_before_audit_outputs(self):
        wrong = deepcopy(self.manifest)
        wrong["bindings"]["lookupHash"] = "0" * 64
        with self.assertRaisesRegex(tft_update.ReviewRequired, "manifest hashes do not match"):
            self.reconcile(wrong)
        # Reconciliation returns data for the publication workflow; these
        # negative cases must not write an approved candidate audit on disk.
        self.assertFalse((self.staging / "audit.json").exists())


class TestRefreshPublication(unittest.TestCase):
    def setUp(self):
        original = tft.load_snapshot(18, "18.1d")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.active = root / "data" / "set18" / "18.1d"
        self.cache = root / "cache"
        self.cache.mkdir()
        shutil.copytree(original.dir, self.active)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(tft, "TFT_DATA_DIR", str(root / "data")))
        self.stack.enter_context(patch.object(tft, "CACHE_DIR", str(self.cache)))
        self.stack.enter_context(patch.object(tft, "REFRESH_STATE_FILE", str(root / "refresh.json")))
        self.stack.enter_context(patch.object(tft, "_SNAP", {}))
        self.snap = tft.load_snapshot(18, "18.1d")
        self.args = SimpleNamespace(set=18, patch="18.1d", force=True)
        self.marker = self.cache / ".dashboard-ready"
        self.site_generation = "prepared-active"
        self.site_prepare = self.stack.enter_context(patch.object(tft_site, "prepare", side_effect=self.fake_site))
        self.site_load = self.stack.enter_context(patch.object(tft_site, "load"))

    def fake_site(self, snap):
        return {"siteGeneration": self.site_generation,
                "baselineRevision": tft.snapshot_revision(snap),
                "manifestHash": "test"}

    def mock_downloads(self, notes=None):
        notes = notes or json.loads((self.active / "patchnotes.json").read_text())
        source = {"url": "https://example.invalid", "lastModified": None, "sha256": "test"}
        cd = self.snap.communitydragon
        exported = {"setData": [{"number": 18, "champions": cd["champions"],
                                "traits": cd["traits"], "items": [x["apiName"] for x in cd["items"]]}],
                    "items": cd["items"]}
        bins = {k.lower(): v for k, v in self.snap.bins.items()}
        self.stack.enter_context(patch.object(tft, "fetch_bytes", return_value=b"notes"))
        self.stack.enter_context(patch.object(tft, "latest_patch_slug", return_value="18-1"))
        self.stack.enter_context(patch.object(tft, "patch_notes_document", return_value=notes))
        self.stack.enter_context(patch.object(tft, "fetch_source", side_effect=[(self.snap.raw, source), (exported, source)]))
        self.stack.enter_context(patch.object(tft, "fetch_json", side_effect=lambda url: bins[url.rsplit("/", 1)[-1].removesuffix(".cdtb.bin.json")]))
        self.stack.enter_context(patch.object(tft, "distill_bin", side_effect=lambda data: data))

    def test_warming_failure_keeps_active_snapshot(self):
        self.mock_downloads()
        before = {p.name: p.read_bytes() for p in self.active.iterdir() if p.is_file()}

        def prepare(candidate):
            self.assertNotEqual(candidate.dir, str(self.active))
            self.assertEqual(before["meta.json"], (self.active / "meta.json").read_bytes())
            raise RuntimeError("simulation failed")

        with self.assertRaisesRegex(RuntimeError, "simulation failed"):
            tft.cmd_fetch(self.args, prepare=prepare)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.active.iterdir() if p.is_file()})
        self.assertFalse(self.marker.exists())

    def test_publication_happens_after_prepare(self):
        self.mock_downloads()
        before = (self.active / "meta.json").read_bytes()
        (self.active / "review.txt").write_text("Keep these review notes.")
        calls = []

        def prepare(candidate):
            self.assertEqual((self.active / "meta.json").read_bytes(), before)
            self.assertEqual(candidate.meta["verifiedAt"], candidate.meta["fetchedAt"])
            calls.append(candidate.patch)

        result = tft.cmd_fetch(self.args, prepare=prepare)
        self.assertEqual(calls, ["18.1d"])
        self.assertEqual(result.dir, str(self.active))
        self.assertNotEqual((self.active / "meta.json").read_bytes(), before)
        self.assertEqual((self.active / "review.txt").read_text(), "Keep these review notes.")

    def test_publication_failure_leaves_every_active_file_unchanged(self):
        self.mock_downloads()
        before = {p.name: p.read_bytes() for p in self.active.iterdir() if p.is_file()}
        with patch.object(tft, "_exchange_directories", side_effect=OSError("publication interrupted")):
            with self.assertRaisesRegex(OSError, "publication interrupted"):
                tft.cmd_fetch(self.args, prepare=lambda candidate: None)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.active.iterdir() if p.is_file()})
        self.assertTrue(all(f["status"] == "current" for f in tft.check_patch_notes(tft.Snapshot(18, "18.1d", str(self.active)))[0]))

    def test_patch_only_change_has_distinct_cache_keys(self):
        newer = copy.deepcopy(self.snap)
        newer.patch = "18.1e"
        self.assertTrue(set(tft.cell_paths(self.snap).values()).isdisjoint(tft.cell_paths(newer).values()))
        self.assertNotEqual(tft.snapshot_revision(self.snap), tft.snapshot_revision(newer))

    def test_loaded_snapshot_keeps_its_cache_when_same_patch_files_change(self):
        old_hash = self.snap.hash_inputs()
        old_paths = tft.cell_paths(self.snap)
        old_revision = tft.snapshot_revision(self.snap)
        old_mana = self.snap.unit("Soraka")["stats"]["mana"]
        overrides = copy.deepcopy(self.snap.overrides)
        overrides["units"].setdefault(self.snap.unit("Soraka")["api"], {}).setdefault("stats", {})["mana"] = old_mana + 10
        (self.active / "overrides.json").write_text(json.dumps(overrides))
        newer = tft.Snapshot(18, "18.1d", str(self.active))
        self.assertEqual(newer.unit("Soraka")["stats"]["mana"], old_mana + 10)
        self.assertEqual(self.snap.unit("Soraka")["stats"]["mana"], old_mana)
        self.assertEqual(self.snap.hash_inputs(), old_hash)
        self.assertEqual(tft.cell_paths(self.snap), old_paths)
        self.assertEqual(tft.snapshot_revision(self.snap), old_revision)
        self.assertTrue(set(old_paths.values()).isdisjoint(tft.cell_paths(newer).values()))

    def test_stale_index_cannot_activate_an_older_audited_patch(self):
        newer_dir = self.active.with_name("18.1e")
        shutil.copytree(self.active, newer_dir)
        self.mock_downloads()
        self.assertEqual(tft.load_snapshot().patch, "18.1e")
        before = (self.active / "meta.json").read_bytes()
        with patch.object(tft, "fetch_source") as fetch, patch.object(tft, "warm") as warm:
            with self.assertRaisesRegex(ReviewRequired, "older patch 18.1d"):
                tft.cmd_fetch(SimpleNamespace(set=18, patch=None, force=True), automatic=True)
        fetch.assert_not_called()
        warm.assert_not_called()
        self.assertEqual(tft.load_snapshot().patch, "18.1e")
        self.assertEqual((self.active / "meta.json").read_bytes(), before)

    def test_known_hotfix_is_reconciled_before_publication(self):
        notes = json.loads((self.active / "patchnotes.json").read_text())
        update = "SEPTEMBER 5TH"
        change = {"what": "Amumu Heal Max HP %", "old": "2.5%", "new": "3%",
                  "section": "CHAMPIONS", "update": update, "major": "Mid-Patch Updates"}
        notes["patch"] = "18.1e"
        notes["updates"].insert(0, update)
        notes["changes"].insert(0, change)
        notes["notes"].insert(0, {"text": "Amumu Heal Max HP %: 2.5% ⇒ 3%", "parent": "",
                                  "section": "CHAMPIONS", "update": update, "major": "Mid-Patch Updates"})
        self.mock_downloads(notes)
        before = (self.active / "overrides.json").read_bytes()
        prepared = []

        def prepare(candidate):
            self.assertEqual(tft.load_snapshot().patch, "18.1d")
            self.assertEqual(tft.curve_at(candidate.unit("Amumu")["curve"]["PassiveHealPercent"], 2), .03)
            prepared.append(candidate.patch)

        result = tft.cmd_fetch(SimpleNamespace(set=18, patch=None, force=True), automatic=True, prepare=prepare)
        self.assertEqual(prepared, ["18.1e"])
        self.assertEqual(result.patch, "18.1e")
        self.assertEqual((self.active / "overrides.json").read_bytes(), before)
        self.assertEqual(tft.load_snapshot().patch, "18.1e")
        self.assertTrue(all(f["status"] == "current" for f in tft.check_patch_notes(result)[0]))

    def test_communitydragon_only_change_reaches_the_reconciler(self):
        # The lookup, the bins and the notes stand still; only the freshly downloaded export moves.
        # check_audit binds none of it, so the fetch itself has to notice.
        audit = json.loads((self.active / "audit.json").read_text())
        known = tft.source_disagreements(self.snap)["disagreements"]
        self.assertTrue(known)      # the archived 18.1d snapshot already differs from its export somewhere
        audit["sourceCrossCheck"] = {"explained": [dict(d, disposition="fixture", reason="Known.", evidence=["fixture"])
                                                   for d in known], "inherited": []}
        (self.active / "audit.json").write_text(json.dumps(audit))
        tft._SNAP.clear()
        self.snap = tft.load_snapshot(18, "18.1d")
        args = SimpleNamespace(set=18, patch="18.1d", force=True)
        calls = []
        self.mock_downloads()
        tft.cmd_fetch(args, automatic=True, prepare=lambda candidate: calls.append(candidate.patch))
        self.assertEqual(calls, ["18.1d"])          # everything explained: published without a review

        tft._SNAP.clear()
        self.snap = tft.load_snapshot(18, "18.1d")
        moved = copy.deepcopy(self.snap.communitydragon)
        soraka = next(c for c in moved["champions"] if c["apiName"] == "DA_18_Soraka")
        soraka["stats"]["hp"] += 50
        # Only the download moves. mock_downloads copies the export it is given, so the
        # archived one is put back: the reconciler reads it as the previous snapshot's,
        # and a disagreement that snapshot already had is inherited rather than raised.
        archived, self.snap.communitydragon = self.snap.communitydragon, moved
        self.mock_downloads()
        self.snap.communitydragon = archived
        before = (self.active / "audit.json").read_bytes()
        with self.assertRaisesRegex(ReviewRequired, "unexplained lookup/CommunityDragon disagreement: Soraka hp"):
            tft.cmd_fetch(args, automatic=True, prepare=lambda candidate: calls.append("published"))
        self.assertEqual(calls, ["18.1d"])
        self.assertEqual((self.active / "audit.json").read_bytes(), before)

    def test_staged_cell_keeps_previous_build_cache(self):
        unit = self.snap.unit("Akali")
        key = "s2-clump-bare"
        old = self.cache / f"akali-{key}-{'0' * 16}.json"
        old.write_text("previous build")
        path = self.cache / f"akali-{key}-{'1' * 16}.json"
        with patch.object(tft, "enumerate_builds", return_value=([], 0, [])):
            tft.compute_cell(self.snap, unit, key, {("akali", key): str(path)}, prune=False)
        self.assertEqual(old.read_text(), "previous build")
        self.assertTrue(path.exists())

    def fake_fetch(self, args, *, automatic, prepare, progress):
        self.assertTrue(automatic)
        progress(phase="fetching", targetPatch="18.1d")
        prepare(self.snap)
        return self.snap

    def test_success_records_state_and_unchanged_run_does_not_reload(self):
        with patch.object(tft, "cmd_fetch", side_effect=self.fake_fetch), \
             patch.object(tft, "warm", return_value=0) as warm, \
             patch.object(tft, "cell_ready", return_value={"akali/s2-clump-bare": True}):
            tft.cmd_refresh(self.args)
            stamp = self.marker.stat().st_mtime_ns
            tft.cmd_refresh(self.args)
        self.assertEqual(self.marker.stat().st_mtime_ns, stamp)
        self.assertFalse(warm.call_args.kwargs["prune"])
        state = tft.refresh_state()
        self.assertEqual((state["status"], state["activePatch"], state["exit"]), ("ok", "18.1d", 0))
        self.assertEqual(state["computedCells"], 0)
        self.assertFalse(any("composition" in key.lower() for key in state))

    def test_prepares_active_analysis_and_responses_before_activation(self):
        events = []
        def warm_builds(**kwargs):
            self.assertIs(kwargs["snap"], self.snap)
            self.assertFalse(self.marker.exists())
            events.append("champions")
            return 3
        def prepare_site(snap):
            self.assertFalse(self.marker.exists())
            events.append("responses")
            return self.fake_site(snap)
        def fetch(args, **kwargs):
            kwargs["prepare"](self.snap)
            self.assertEqual(events, ["champions", "responses"])
            events.append("activate")
            return self.snap
        self.site_prepare.side_effect = prepare_site
        with patch.object(tft, "cmd_fetch", side_effect=fetch), \
             patch.object(tft, "warm", side_effect=warm_builds), \
             patch.object(tft, "cell_ready", return_value={"cell": True}):
            tft.cmd_refresh(self.args)
        self.assertEqual(events, ["champions", "responses", "activate"])
        state = tft.refresh_state()
        self.assertEqual(state["computedCells"], 3)
        self.assertEqual(json.loads(self.marker.read_text())["site"], self.fake_site(self.snap))

    def test_champion_failure_preserves_active_archive_and_ready_marker(self):
        self.mock_downloads()
        before = {p.name: p.read_bytes() for p in self.active.iterdir() if p.is_file()}
        self.marker.write_text('{"previous":true}')
        with patch.object(tft, "warm", side_effect=RuntimeError("champion worker failed")), \
             patch.object(tft, "cell_ready", return_value={"cell": True}):
            with self.assertRaises(SystemExit) as error:
                tft.cmd_refresh(self.args)
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.active.iterdir() if p.is_file()})
        self.assertEqual(json.loads(self.marker.read_text()), {"previous": True})
        self.site_prepare.assert_not_called()

    def test_incomplete_champion_calculations_cannot_publish(self):
        with patch.object(tft, "cmd_fetch", side_effect=self.fake_fetch), \
             patch.object(tft, "warm", return_value=0), \
             patch.object(tft, "cell_ready", return_value={"cell": False}):
            with self.assertRaises(SystemExit):
                tft.cmd_refresh(self.args)
        self.assertFalse(self.marker.exists())
        self.site_prepare.assert_not_called()
        self.assertIn("Some champion builds did not finish", tft.refresh_state()["message"])

    def test_response_failure_preserves_active_archive(self):
        self.mock_downloads()
        before = {p.name: p.read_bytes() for p in self.active.iterdir() if p.is_file()}
        self.site_prepare.side_effect = RuntimeError("compression failed")
        with patch.object(tft, "warm", return_value=0), \
             patch.object(tft, "cell_ready", return_value={"cell": True}):
            with self.assertRaises(SystemExit):
                tft.cmd_refresh(self.args)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.active.iterdir() if p.is_file()})
        self.assertFalse(self.marker.exists())

    def test_saved_active_response_change_signals_a_new_publication(self):
        with patch.object(tft, "cmd_fetch", side_effect=self.fake_fetch), \
             patch.object(tft, "warm", return_value=0), \
             patch.object(tft, "cell_ready", return_value={"cell": True}):
            tft.cmd_refresh(self.args)
            before = json.loads(self.marker.read_text())
            self.site_generation = "prepared-new-response"
            tft.cmd_refresh(self.args)
        after = json.loads(self.marker.read_text())
        self.assertEqual(before["revision"], after["revision"])
        self.assertNotIn("compositionRevision", after)
        self.assertNotEqual(before["site"], after["site"])

    def test_waits_for_an_existing_champion_calculation(self):
        with patch.object(tft, "cmd_fetch", side_effect=self.fake_fetch), \
             patch.object(tft, "warm", side_effect=[None, 0]) as warm, \
             patch.object(tft, "cell_ready", return_value={"cell": True}), \
             patch.object(tft.time, "sleep") as sleep:
            tft.cmd_refresh(self.args)
        self.assertEqual(warm.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_dashboard_ready_rejects_wrong_prepared_revision(self):
        with patch.object(tft, "cell_ready", return_value={"cell": True}):
            with self.assertRaisesRegex(RuntimeError, "do not match"):
                tft.dashboard_ready(self.snap, prepared_site={**self.fake_site(self.snap), "baselineRevision": "wrong"})
        self.assertFalse(self.marker.exists())

    def test_malformed_state_does_not_prevent_the_next_refresh(self):
        Path(tft.REFRESH_STATE_FILE).write_text("[]")
        self.marker.write_text("[]")
        self.assertEqual(tft.refresh_state(), {})
        with patch.object(tft, "cmd_fetch", side_effect=self.fake_fetch), \
             patch.object(tft, "warm", return_value=0), \
             patch.object(tft, "cell_ready", return_value={"akali/s2-clump-bare": True}):
            tft.cmd_refresh(self.args)
        self.assertEqual(tft.refresh_state()["status"], "ok")
        self.assertEqual(json.loads(self.marker.read_text())["patch"], "18.1d")

    def test_warm_failure_records_error_without_activation(self):
        with patch.object(tft, "cmd_fetch", side_effect=self.fake_fetch), \
             patch.object(tft, "warm", side_effect=RuntimeError("simulation failed")):
            with self.assertRaises(SystemExit) as error:
                tft.cmd_refresh(self.args)
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(tft.refresh_state()["status"], "failed")
        self.assertEqual(tft.refresh_state()["activePatch"], "18.1d")
        self.assertFalse(self.marker.exists())

    def test_unknown_change_records_review_status(self):
        with patch.object(tft, "cmd_fetch", side_effect=ReviewRequired("New targeting behavior needs review.")), \
             patch.object(tft, "warm") as warm:
            with self.assertRaises(SystemExit) as error:
                tft.cmd_refresh(self.args)
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(tft.refresh_state()["status"], "needs-review")
        warm.assert_not_called()
        self.assertFalse(self.marker.exists())

    def test_concurrent_refresh_does_not_overwrite_running_state(self):
        tft.write_json_atomic(tft.REFRESH_STATE_FILE, {"status": "running", "phase": "warming"})
        with open(self.cache / "refresh.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(tft, "cmd_fetch") as fetch:
                tft.cmd_refresh(self.args)
        fetch.assert_not_called()
        self.assertEqual(tft.refresh_state(), {"status": "running", "phase": "warming"})

    def failing_download(self, args, *, automatic, prepare, progress):
        progress(phase="fetching", targetPatch="18.2")
        tft.fetch_bytes("https://user:password@example.invalid/source?token=secret#private")

    def test_exhausted_transient_download_exits_75_with_safe_transport_context(self):
        with patch.object(tft, "cmd_fetch", side_effect=self.failing_download), \
             patch.object(tft.urllib.request, "urlopen", side_effect=URLError(
                 socket.gaierror(socket.EAI_NONAME, "DNS failure with token=secret"))) as opener, \
             patch.object(tft_update.time, "sleep"), patch.object(tft, "warm") as warm:
            with self.assertRaises(SystemExit) as caught:
                tft.cmd_refresh(self.args)
        self.assertEqual(caught.exception.code, 75)
        self.assertEqual(opener.call_count, 4)
        warm.assert_not_called()
        state = tft.refresh_state()
        self.assertEqual((state["status"], state["phase"], state["failedPhase"]), ("failed", "stopped", "fetching"))
        self.assertEqual(state["transport"]["attempts"], 4)
        self.assertTrue(state["transport"]["automaticRetry"])
        self.assertEqual(state["transport"]["url"], "https://example.invalid/source")
        self.assertEqual(state["history"][-1]["exit"], 75)
        for secret in ("password", "token=secret", "#private"):
            self.assertNotIn(secret, json.dumps(state))
        self.assertIsNone(tft._FETCH_PROGRESS.get())

    def test_permanent_http_error_does_not_trigger_service_retry(self):
        with patch.object(tft, "cmd_fetch", side_effect=self.failing_download), \
             patch.object(tft.urllib.request, "urlopen", side_effect=HTTPError(
                 "https://example.invalid/source?token=secret", 403, "secret", {}, None)) as opener:
            with self.assertRaises(SystemExit) as caught:
                tft.cmd_refresh(self.args)
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(opener.call_count, 1)
        self.assertFalse(tft.refresh_state()["transport"]["automaticRetry"])
        self.assertEqual(tft.refresh_state()["transport"]["httpStatus"], 403)

    def test_non_fetch_timeouts_and_arbitrary_exit_75_are_permanent_failures(self):
        for failure, expected in ((TimeoutError("warm timeout"), 1), (SystemExit(75), 1), (SystemExit(143), 143)):
            with self.subTest(failure=type(failure).__name__), \
                 patch.object(tft, "cmd_fetch", side_effect=failure):
                with self.assertRaises(SystemExit) as caught:
                    tft.cmd_refresh(self.args)
            self.assertEqual(caught.exception.code, expected)
            self.assertEqual(tft.refresh_state()["failedPhase"], "checking")

    def test_review_blocker_and_terminal_history_survive_later_network_failure(self):

        def review(args, **kwargs):
            kwargs["progress"](phase="validating", targetPatch="18.2")
            raise ReviewRequired("A new mechanic needs review.")

        with patch.object(tft, "cmd_fetch", side_effect=review):
            with self.assertRaises(SystemExit) as caught:
                tft.cmd_refresh(self.args)
        self.assertEqual(caught.exception.code, 2)
        blocker = tft.refresh_state()["reviewBlocker"]
        self.assertEqual((blocker["targetPatch"], blocker["failedPhase"]), ("18.2", "validating"))
        with patch.object(tft, "cmd_fetch", side_effect=self.failing_download), \
             patch.object(tft.urllib.request, "urlopen", side_effect=URLError(TimeoutError("network"))), \
             patch.object(tft_update.time, "sleep"):
            with self.assertRaises(SystemExit):
                tft.cmd_refresh(self.args)
        state = tft.refresh_state()
        self.assertEqual(state["reviewBlocker"], blocker)
        self.assertEqual([entry["exit"] for entry in state["history"]], [2, 75])
        self.assertEqual(state["history"][0]["message"], "A new mechanic needs review.")

    def test_legacy_review_state_migrates_to_blocker_and_history(self):
        old = {"status": "needs-review", "phase": "stopped", "message": "Review existing blocker.",
               "targetPatch": "18.2", "activePatch": "18.1d", "finishedAt": "2026-09-10T10:00:00+00:00", "exit": 2}
        tft.write_json_atomic(tft.REFRESH_STATE_FILE, old)
        with patch.object(tft, "cmd_fetch", side_effect=RuntimeError("later failure")):
            with self.assertRaises(SystemExit):
                tft.cmd_refresh(self.args)
        state = tft.refresh_state()
        self.assertEqual(state["reviewBlocker"]["message"], old["message"])
        self.assertEqual(state["history"][0]["message"], old["message"])
        self.assertEqual(state["reviewBlocker"]["detectedAt"], old["finishedAt"])

    def test_validated_preparation_clears_old_blocker_even_if_warming_fails(self):
        tft.write_json_atomic(tft.REFRESH_STATE_FILE, {"status": "needs-review", "message": "Old review.", "exit": 2})
        with patch.object(tft, "cmd_fetch", side_effect=self.fake_fetch), \
             patch.object(tft, "warm", side_effect=RuntimeError("new warm failure")):
            with self.assertRaises(SystemExit):
                tft.cmd_refresh(self.args)
        state = tft.refresh_state()
        self.assertIsNone(state["reviewBlocker"])
        self.assertEqual(state["failedPhase"], "warming-builds")
        self.assertEqual(state["history"][0]["message"], "Old review.")

    def test_success_clears_blocker_and_preserves_history(self):
        tft.write_json_atomic(tft.REFRESH_STATE_FILE, {"status": "needs-review", "message": "Old review.", "exit": 2})
        with patch.object(tft, "cmd_fetch", side_effect=self.fake_fetch), \
             patch.object(tft, "warm", return_value=0), \
             patch.object(tft, "cell_ready", return_value={"cell": True}):
            tft.cmd_refresh(self.args)
        state = tft.refresh_state()
        self.assertIsNone(state["reviewBlocker"])
        self.assertEqual([entry["status"] for entry in state["history"]], ["needs-review", "ok"])

    def test_terminal_history_is_bounded_and_does_not_duplicate_previous_run(self):
        history = [{"status": "failed", "message": f"old {i}", "exit": 1} for i in range(15)]
        tft.write_json_atomic(tft.REFRESH_STATE_FILE, {**history[-1], "history": history})
        with patch.object(tft, "cmd_fetch", side_effect=RuntimeError("new failure")):
            with self.assertRaises(SystemExit):
                tft.cmd_refresh(self.args)
        history = tft.refresh_state()["history"]
        self.assertEqual(len(history), tft.REFRESH_HISTORY_LIMIT)
        self.assertEqual([entry["message"] for entry in history], [f"old {i}" for i in range(6, 15)] + ["new failure"])

    def test_long_retry_after_persists_guard_without_immediate_service_retry(self):
        with patch.object(tft, "cmd_fetch", side_effect=self.failing_download), \
             patch.object(tft.urllib.request, "urlopen", side_effect=HTTPError(
                 "https://example.invalid/source", 429, "limited", {"Retry-After": "600"}, None)) as opener, \
             patch.object(tft_update.time, "sleep") as sleep:
            with self.assertRaises(SystemExit) as caught:
                tft.cmd_refresh(self.args)
        state = tft.refresh_state()
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(opener.call_count, 1)
        sleep.assert_not_called()
        self.assertFalse(state["transport"]["automaticRetry"])
        self.assertEqual(state["transport"]["retryAfterSeconds"], 600)
        self.assertGreater(datetime.fromisoformat(state["retryNotBefore"]), datetime.now(timezone.utc) + timedelta(seconds=590))

    def test_retry_not_before_skips_fetch_and_expired_guard_allows_recovery(self):
        deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        blocker = {"message": "Still needs review.", "targetPatch": "18.2"}
        tft.write_json_atomic(tft.REFRESH_STATE_FILE, {"status": "failed", "exit": 1,
            "activePatch": "18.1d", "lastSuccessAt": "old success", "retryNotBefore": deadline,
            "reviewBlocker": blocker, "transport": {"url": "https://example.invalid/source"}})
        with patch.object(tft, "cmd_fetch") as fetch:
            with self.assertRaises(SystemExit) as caught:
                tft.cmd_refresh(self.args)
        fetch.assert_not_called()
        state = tft.refresh_state()
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual((state["status"], state["phase"]), ("waiting-not-before", "waiting-not-before"))
        self.assertEqual(state["lastSuccessAt"], "old success")
        self.assertEqual(state["reviewBlocker"], blocker)
        state["retryNotBefore"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        tft.write_json_atomic(tft.REFRESH_STATE_FILE, state)
        with patch.object(tft, "cmd_fetch", side_effect=self.fake_fetch) as fetch, \
             patch.object(tft, "warm", return_value=0), \
             patch.object(tft, "cell_ready", return_value={"cell": True}):
            tft.cmd_refresh(self.args)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(tft.refresh_state()["status"], "ok")
        self.assertIsNone(tft.refresh_state()["retryNotBefore"])
        self.assertIsNone(tft.refresh_state()["reviewBlocker"])

    def test_optional_bin_404_preserves_historical_evidence_literal(self):
        missing = next(api for api, value in self.snap.bins.items() if "error" in value)
        self.mock_downloads()
        original = tft.fetch_json

        def fetched(url):
            if url.rsplit("/", 1)[-1].removesuffix(".cdtb.bin.json") == missing.lower():
                raise HTTPError(url, 404, "Not Found; GET context; attempt 1/4", {}, None)
            return original(url)

        with patch.object(tft, "fetch_json", side_effect=fetched):
            result = tft.cmd_fetch(self.args)
        self.assertEqual(result.bins[missing], {"error": "HTTP Error 404: Not Found"})


if __name__ == "__main__":
    unittest.main()
