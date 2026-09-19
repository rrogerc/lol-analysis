"""GET transport retries without network access or real backoff sleeps."""
from email.message import Message
from email.utils import formatdate
import errno
import hashlib
import http.client
import io
import json
import queue
import socket
import ssl
import threading
import traceback
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import tft_http
import tft


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
        return tft_http.get(self.URL, opener=opener, sleep=self.clock.sleep,
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
                with self.assertRaises(tft_http.FetchError) as caught:
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

        with self.assertRaises(tft_http.FetchError) as caught:
            self.get(fail, max_attempts=4, total_timeout=5, attempt_timeout=4, backoff=0.25)
        self.assertEqual(timeouts, [4, 0.75])
        self.assertEqual(self.clock.sleeps, [0.25])
        self.assertEqual(caught.exception.elapsed, 5)
        self.assertIn("total retry deadline exhausted", str(caught.exception))

    def test_does_not_accept_success_that_arrives_after_total_deadline(self):
        def late(request, *, timeout):
            self.clock.now += 6
            return Reply()

        with self.assertRaises(tft_http.FetchError) as caught:
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
            with patch.object(tft_http.queue.Queue, "get", side_effect=deadline):
                with self.assertRaises(tft_http.FetchError) as caught:
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
                    tft_http.get(url, opener=Mock(side_effect=error), max_attempts=1,
                                 monotonic=self.clock.monotonic, sleep=self.clock.sleep)
                except (HTTPError, tft_http.FetchError) as terminal:
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
        with patch.object(tft_http.urllib.request, "urlopen", return_value=Reply()) as opener:
            self.assertEqual(tft_http.get(self.URL).body, b"ok")
        self.assertEqual(opener.call_count, 1)

    def test_transient_classification_survives_terminal_error_wrapping(self):
        for error, retryable in ((URLError(socket.gaierror(socket.EAI_NONAME, "DNS")), True),
                                 (HTTPError(self.URL, 503, "unavailable", {}, None), True),
                                 (HTTPError(self.URL, 404, "missing", {}, None), False),
                                 (ssl.SSLCertVerificationError(1, "certificate"), False)):
            with self.subTest(error=type(error).__name__, retryable=retryable):
                self.assertEqual(tft_http.is_transient(error), retryable)
                with self.assertRaises(URLError) as caught:
                    self.get(Mock(side_effect=error), max_attempts=1)
                self.assertEqual(tft_http.is_transient(caught.exception), retryable)

    def test_retry_callback_has_sanitized_context_and_excludes_permanent_errors(self):
        url = "https://user:password@example.invalid/source?token=secret#private"
        callback = Mock()
        opener = Mock(side_effect=[HTTPError(url, 503, "secret", {"Retry-After": "2"}, None), Reply()])
        tft_http.get(url, opener=opener, monotonic=self.clock.monotonic,
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
            with patch.object(tft_http.queue.Queue, "get", new=wait):
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
            with patch.object(tft_http, "_worker_slots", NonblockingSlots()), \
                 patch.object(tft_http.queue.Queue, "get", side_effect=deadline):
                for _ in range(5):
                    with self.assertRaises(tft_http.FetchError):
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
                 patch.object(tft_http.time, "sleep") as sleep, patch("builtins.print") as logged:
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
             patch.object(tft_http.time, "sleep") as sleep:
            with self.assertRaises(json.JSONDecodeError):
                tft.fetch_source("https://example.invalid/source")
        self.assertEqual(opener.call_count, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
