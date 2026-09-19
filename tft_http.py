"""Bounded GET retries for downloaded TFT source bytes.

Parsing belongs to callers, outside the retry boundary. A daemon worker bounds
each attempt including DNS and the complete body read: urllib's socket timeout
alone cannot enforce an elapsed deadline for those operations. Python cannot
cancel an already blocked resolver call; an abandoned worker closes its response
when it returns and never starts another attempt. A process-wide worker bound
also prevents repeated blocked calls from accumulating unbounded threads.
"""
from dataclasses import dataclass
from email.message import Message
from email.utils import parsedate_to_datetime
import errno
import http.client
from http import HTTPStatus
import math
import queue
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


RETRY_STATUSES = frozenset((429, 500, 502, 503, 504))
MAX_ACTIVE_ATTEMPTS = 4
_worker_slots = threading.BoundedSemaphore(MAX_ACTIVE_ATTEMPTS)
_RETRY_ERRNOS = frozenset(getattr(errno, name) for name in (
    "ECONNABORTED", "ECONNREFUSED", "ECONNRESET", "EHOSTUNREACH", "ENETDOWN",
    "ENETUNREACH", "EPIPE", "ETIMEDOUT", "EAGAIN", "EINTR"))


@dataclass(frozen=True)
class Response:
    body: bytes
    headers: Message
    status: int | None
    attempts: int


class FetchError(urllib.error.URLError):
    """A terminal transport error with sanitized request/attempt context."""


def _safe_url(url):
    """Retain the source path, omitting userinfo, query and fragment."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or "unknown-host"
        if ":" in host:
            host = f"[{host}]"
        if parts.port is not None:
            host += f":{parts.port}"
        path = urllib.parse.quote(parts.path, safe="/%:@-._~!$&'()*+,;=")
        return urllib.parse.urlunsplit((parts.scheme, host, path, "", ""))
    except (TypeError, ValueError):
        return "<invalid URL>"


def _headers(value):
    copied = Message()
    if value is not None:
        for key, item in value.items():
            copied[key] = item
    return copied


def _attempt(request, opener, timeout, monotonic):
    """One opener/read operation with an elapsed-time watchdog."""
    deadline = monotonic() + timeout
    slots = _worker_slots
    if not slots.acquire(timeout=timeout):
        raise TimeoutError("HTTP worker capacity deadline exceeded")
    remaining = deadline - monotonic()
    if remaining <= 0:
        slots.release()
        raise TimeoutError("HTTP attempt deadline exceeded")
    outcome = queue.Queue(maxsize=1)

    def download():
        try:
            with opener(request, timeout=remaining) as response:
                headers = _headers(response.headers)
                status = getattr(response, "status", None)
                if status is not None and status >= 400:
                    raise urllib.error.HTTPError(request.full_url, status, "HTTP response", headers, None)
                value = (response.read(), headers, status)
            outcome.put((True, value))
        except BaseException as error:
            # urlopen raises HTTPError before entering the response context.
            # Close it here even if the caller's watchdog has already expired.
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            outcome.put((False, error))
        finally:
            slots.release()

    worker = threading.Thread(target=download, name="tft-http-get", daemon=True)
    try:
        worker.start()
    except BaseException:
        slots.release()
        raise
    try:
        succeeded, value = outcome.get(timeout=max(0.0, deadline - monotonic()))
    except queue.Empty:
        raise TimeoutError("HTTP attempt deadline exceeded") from None
    if not succeeded:
        raise value
    return value


def _reason(error):
    return error.reason if isinstance(error, urllib.error.URLError) and not isinstance(
        error, urllib.error.HTTPError) else error


def is_transient(error):
    """Whether the original or sanitized terminal error can recover on retry.

    EAI_NONAME can also be an intermittent resolver answer for the fixed source
    hosts. A truly nonexistent host still stops at the same retry budget.
    """
    if isinstance(error, urllib.error.HTTPError):
        return error.code in RETRY_STATUSES
    if isinstance(error, FetchError):
        return bool(getattr(error, "retryable", False))
    reason = _reason(error)
    if isinstance(reason, socket.gaierror):
        return reason.errno in (socket.EAI_AGAIN, socket.EAI_NONAME)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return False
    if isinstance(reason, (TimeoutError, ConnectionError, ssl.SSLEOFError,
                           http.client.IncompleteRead)):
        return True
    return isinstance(reason, OSError) and reason.errno in _RETRY_ERRNOS


def _retry_after(headers, now):
    value = headers.get("Retry-After") if headers is not None else None
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        # Avoid parsing an unbounded attacker-controlled decimal integer.
        return math.inf if len(value) > 10 else float(int(value))
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            from datetime import timezone
            date = date.replace(tzinfo=timezone.utc)
        return max(0.0, date.timestamp() - now)
    except (TypeError, ValueError, OverflowError):
        return None


def _terminal(error, url, attempts, maximum, elapsed, stop):
    safe_url = _safe_url(url)
    context = f"GET {safe_url}; attempt {attempts}/{maximum}; elapsed {elapsed:.3f}s; {stop}"
    if isinstance(error, urllib.error.HTTPError):
        try:
            phrase = HTTPStatus(error.code).phrase
        except ValueError:
            phrase = "HTTP failure"
        result = urllib.error.HTTPError(safe_url, error.code, f"{phrase}; {context}",
                                       error.headers, None)
    else:
        reason = _reason(error)
        category = type(reason).__name__
        code = getattr(reason, "errno", None)
        if isinstance(code, int):
            category += f" errno={code}"
        result = FetchError(f"{category}; {context}")
    result.attempts = attempts
    result.max_attempts = maximum
    result.elapsed = elapsed
    result.source_url = safe_url
    result.retryable = is_transient(error)
    return result


def get(url, *, headers=None, opener=None, max_attempts=4, total_timeout=90.0,
        attempt_timeout=30.0, backoff=1.0, max_retry_after=15.0,
        sleep=None, monotonic=None, wall_time=None, on_retry=None):
    """GET raw bytes plus copied case-insensitive headers, with bounded retries.

    ``opener`` accepts a urllib Request and ``timeout=seconds``. Pass the caller's
    ``urllib.request.urlopen`` explicitly when its existing patch point matters.
    Clock/sleep injection supports deterministic tests without networking/sleeps.

    Retry-After accepts delta seconds or an HTTP date. Valid server delays are
    honored within the configured wait/deadline bounds; a longer required delay
    terminates visibly rather than retrying earlier than the server requested.
    HTTP errors remain HTTPError (including permanent 404); other failures are
    FetchError/URLError. Error messages omit credentials, query and fragment and
    suppress the original exception text, which can contain those values.
    ``on_retry(event)`` receives a sanitized URL, failed/next attempt, elapsed
    time, delay and status before backoff. The callback should not block.
    """
    if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
        raise ValueError("max_attempts must be an integer from 1 to 10")
    values = {"total_timeout": total_timeout, "attempt_timeout": attempt_timeout,
              "backoff": backoff, "max_retry_after": max_retry_after}
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{key} must be finite")
        if value < 0 or key in ("total_timeout", "attempt_timeout") and value == 0:
            raise ValueError(f"{key} is outside its supported range")
    opener = urllib.request.urlopen if opener is None else opener
    sleep = time.sleep if sleep is None else sleep
    monotonic = time.monotonic if monotonic is None else monotonic
    wall_time = time.time if wall_time is None else wall_time
    started = monotonic()
    deadline = started + total_timeout
    attempts = 0
    last = TimeoutError("HTTP total deadline exceeded")
    while attempts < max_attempts:
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        attempts += 1
        try:
            # An abandoned opener may still be using its Request; retries must
            # not share that mutable object (urllib writes Request.timeout).
            request = urllib.request.Request(url, headers=dict(headers or {}), method="GET")
            body, response_headers, status = _attempt(request, opener,
                min(attempt_timeout, remaining), monotonic)
            if monotonic() >= deadline:
                last = TimeoutError("HTTP total deadline exceeded")
                break
            return Response(body, response_headers, status, attempts)
        except Exception as error:
            last = error
            retry_after = None
            if isinstance(error, urllib.error.HTTPError):
                retry_after = _retry_after(_headers(error.headers), wall_time())
                error.close()
            retryable = is_transient(error)
            stop = "attempt limit reached" if retryable else "permanent failure"
            if not retryable or attempts == max_attempts:
                raise _terminal(error, url, attempts, max_attempts, monotonic() - started, stop) from None
            if retry_after is not None and retry_after > max_retry_after:
                raise _terminal(error, url, attempts, max_attempts, monotonic() - started,
                                "Retry-After exceeds configured wait bound") from None
            delay = max(backoff * 2 ** (attempts - 1), retry_after or 0.0)
            remaining = deadline - monotonic()
            if remaining <= 0 or delay >= remaining:
                break
            if on_retry is not None:
                on_retry({"url": _safe_url(url), "attempt": attempts,
                          "nextAttempt": attempts + 1, "maxAttempts": max_attempts,
                          "delay": delay, "elapsed": monotonic() - started,
                          "errorType": type(_reason(error)).__name__,
                          "status": error.code if isinstance(error, urllib.error.HTTPError) else None})
                if delay >= deadline - monotonic():
                    break
            if delay:
                sleep(delay)
    raise _terminal(last, url, attempts, max_attempts, monotonic() - started,
                    "total retry deadline exhausted") from None
