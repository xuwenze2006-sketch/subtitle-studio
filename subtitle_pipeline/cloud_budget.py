"""Durable, conservative budget reservations for paid HTTP requests.

The caller must provide a stable request ID covering the complete request input.
``CloudCancelled`` from ``send`` means *definitely not sent*: adapters must never
raise it after an upload may have begun. Other send errors retain the reservation.
The default stop line is strict: a new reservation must keep committed cost <18.
This estimates local spending; it is not a provider-side billing limit.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
from typing import Callable

from .atomic_io import cleanup_temporary, replace_with_retry


class CloudRequestError(RuntimeError):
    """A request or its durable local state cannot safely be used."""


class BudgetExceeded(CloudRequestError):
    """The next reservation would reach the configured stop line."""


class SubmissionUnknown(CloudRequestError):
    """A request may have been billed; do not automatically submit it again."""


class CloudCancelled(CloudRequestError):
    """Cancellation before transmission, or between explicitly rejected attempts."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict
    body: bytes


_THREAD_LOCKS = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_HELD_STATES = frozenset(("reserved", "submitting", "retry_wait", "received", "unknown"))
_STATES = _HELD_STATES | {"success", "rejected", "cancelled"}


def _money(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("Cost must be a finite nonnegative number.")
    try:
        amount = Decimal(str(value))
    except (ValueError, InvalidOperation):
        raise ValueError("Cost must be a finite nonnegative number.") from None
    if not amount.is_finite() or amount < 0 or not math.isfinite(float(amount)):
        raise ValueError("Cost must be a finite nonnegative number.")
    return amount


@contextmanager
def _file_lock(path, blocking=True):
    """An OS lock plus an in-process lock; never delete the lock's inode."""
    key = os.path.normcase(str(path.resolve()))
    with _THREAD_LOCKS_GUARD:
        local_lock = _THREAD_LOCKS.setdefault(key, threading.Lock())
    if not local_lock.acquire(blocking=blocking):
        yield False
        return
    handle = None
    acquired = False
    body_error = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        while True:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (BlockingIOError, PermissionError):
                if not blocking:
                    break
                time.sleep(0.02)
        yield acquired
    except BaseException as error:
        # Only an exception escaping this context is primary; a caller may
        # already be handling an unrelated exception when it enters the lock.
        body_error = error
        raise
    finally:
        cleanup_errors = []
        if handle is not None:
            try:
                if acquired:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except BaseException as error:
                cleanup_errors.append(('native unlock', error))
            try:
                handle.close()
            except BaseException as error:
                cleanup_errors.append(('handle close', error))
        try:
            local_lock.release()
        except BaseException as error:
            cleanup_errors.append(('local mutex release', error))
        if cleanup_errors:
            primary = body_error
            # A newly raised interrupt takes priority, but only after every
            # remaining owned resource has received its cleanup attempt.
            for _, error in cleanup_errors:
                if not isinstance(error, Exception):
                    primary = error
            if primary is None:
                primary = cleanup_errors[0][1]
            for stage, error in cleanup_errors:
                label = f'文件锁 {stage} 失败：{path}'
                if error is primary:
                    primary.add_note(label)
                else:
                    details = tuple(getattr(error, '__notes__', ()))
                    primary.add_note(f'{label}: {type(error).__name__}: {error}')
                    for detail in details:
                        primary.add_note(f'{label}: {detail}')
            if primary is not body_error:
                raise primary


def _write_bytes(path, content, replace=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix="." + path.name + ".", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if replace:
            replace_with_retry(temporary, path)
        else:
            # Atomic no-clobber publication also protects existing user files.
            os.link(temporary, path)
        if os.name != "nt":
            descriptor = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary is not None:
            cleanup_temporary(temporary)


def _json_object(body):
    def invalid_constant(_):
        raise ValueError("Invalid JSON constant.")
    def unique_members(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("Duplicate JSON member.")
            result[name] = value
        return result
    data = json.loads(body, parse_constant=invalid_constant, object_pairs_hook=unique_members)
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object.")
    return data


def _require_unused_response_path(path):
    """Inspect entries without treating inaccessible or dangling paths as free."""
    try:
        path.lstat()
    except FileNotFoundError:
        # Windows also reports FileNotFoundError for file/child. Check the
        # nearest existing parent before considering this a creatable path.
        parent = path.parent
        while True:
            try:
                parent.lstat()
            except FileNotFoundError:
                if parent == parent.parent:
                    raise
                parent = parent.parent
            else:
                if not stat.S_ISDIR(parent.stat().st_mode):
                    raise CloudRequestError("The response parent is not a directory; no new transmission was started.")
                return
    raise CloudRequestError("The response path is already in use; no new transmission was started.")


def _retry_delay(headers, attempt):
    value = next((v for k, v in headers.items() if str(k).lower() == "retry-after"), None)
    if value is not None:
        try:
            seconds = float(value)
            if math.isfinite(seconds) and seconds >= 0:
                return seconds
        except (ValueError, TypeError, OverflowError):
            pass
        try:
            date = parsedate_to_datetime(str(value))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            pass
    return float(2 ** attempt)


def _wait_retry(delay, stop_event):
    """Wait the whole delay in platform-safe slices, without shortening it.

    Event.wait returns false only when its timeout expires. Subtracting each
    completed slice preserves the full delay, including deadlines too large for
    the platform's native timed-lock API. The persisted deadline is untouched.
    """
    remaining = delay
    while True:
        interval = min(remaining, 60.0)
        if stop_event is not None:
            if stop_event.wait(interval):
                return True
        else:
            time.sleep(interval)
        if remaining <= interval:
            return False
        remaining -= interval


class BudgetLedger:
    def __init__(self, path: Path, budget_cny=20.0, stop_cny=18.0):
        self.path = Path(path).resolve()
        self.budget = _money(budget_cny)
        self.stop = _money(stop_cny)
        if not 0 < self.stop <= self.budget:
            raise ValueError("Stop line must be positive and no greater than the budget.")
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def _request_lock(self, request_id):
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        legacy = self.path.parent / ("." + self.path.name + ".request-" + digest + ".lock")
        # Keep existing lock identities until an OFFLINE migration. Moving or
        # deleting a lock while another process can open it splits ownership.
        # A dangling legacy symlink must not silently select a different lock.
        if legacy.exists() or legacy.is_symlink():
            return legacy
        return self.path.parent / "运行数据" / "请求锁" / legacy.name

    def _load(self):
        if not self.path.exists():
            return {"version": 1, "budget_cny": float(self.budget), "stop_cny": float(self.stop), "requests": {}}
        try:
            data = _json_object(self.path.read_bytes())
            if data["version"] != 1 or _money(data["budget_cny"]) != self.budget or _money(data["stop_cny"]) != self.stop:
                raise ValueError()
            if not isinstance(data["requests"], dict):
                raise ValueError()
            for record in data["requests"].values():
                if record["status"] not in _STATES or not isinstance(record["attempts"], list):
                    raise ValueError()
                _money(record["reserved_cny"])
                _money(record.get("actual_cny", 0))
                if record["status"] == "success":
                    _money(record["actual_cny"])
                if record["status"] in ("submitting", "received", "success") and not record["attempts"]:
                    raise ValueError()
                if record["status"] in ("received", "success") and not isinstance(record["raw_sha256"], str):
                    raise ValueError()
                if any(not isinstance(attempt, dict) or not isinstance(attempt.get("status"), str) for attempt in record["attempts"]):
                    raise ValueError()
                attempts = record["attempts"]
                if len(attempts) > 3:
                    raise ValueError()
                attempt_states = {"submitting", "received", "unknown", "success", "rejected", "cancelled_before_send"}
                for number, attempt in enumerate(attempts, 1):
                    if type(attempt.get("number")) is not int or attempt["number"] != number or attempt["status"] not in attempt_states:
                        raise ValueError()
                    if attempt["status"] == "rejected" and attempt.get("http_status") not in (400, 401, 403, 422, 429):
                        raise ValueError()
                    if attempt["status"] in ("received", "success") and attempt.get("http_status") != 200:
                        raise ValueError()
                    if number < len(attempts) and not (attempt["status"] == "cancelled_before_send" or
                            (attempt["status"] == "rejected" and attempt.get("http_status") == 429)):
                        raise ValueError()
                status = record["status"]
                last = attempts[-1] if attempts else {}
                if "next_attempt_at" in record:
                    deadline = record["next_attempt_at"]
                    try:
                        valid_deadline = type(deadline) in (int, float) and math.isfinite(deadline) and deadline >= 0
                    except OverflowError:
                        valid_deadline = False
                    if not valid_deadline:
                        raise ValueError()
                elif (status == "retry_wait" or status in ("cancelled", "reserved")
                      and last.get("status") == "rejected" and last.get("http_status") == 429):
                    # A missing deadline must not turn a persisted rate limit
                    # into permission to send immediately.
                    raise ValueError()
                if status in ("submitting", "received", "success", "unknown") and last.get("status") != status:
                    raise ValueError()
                if status == "retry_wait" and not (0 < len(attempts) < 3 and last.get("status") == "rejected" and last.get("http_status") == 429):
                    raise ValueError()
                if status == "reserved" and len(attempts) >= 3:
                    raise ValueError()
                if status in ("reserved", "cancelled") and attempts and not (
                        last.get("status") == "cancelled_before_send" or
                        (last.get("status") == "rejected" and last.get("http_status") == 429)):
                    raise ValueError()
                if status == "rejected" and last.get("status") not in ("rejected", "cancelled_before_send"):
                    raise ValueError()
                if not isinstance(record["raw_path"], str) or not isinstance(record["provider"], str):
                    raise ValueError()
            return data
        except (OSError, ValueError, TypeError, KeyError, UnicodeError):
            raise CloudRequestError("The budget ledger is invalid or its configuration differs.") from None

    def _save(self, data):
        try:
            _write_bytes(self.path, json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8"))
        except (OSError, ValueError, TypeError):
            raise CloudRequestError("The budget ledger could not be persisted; automatic resubmission is unsafe.") from None

    @staticmethod
    def _totals(data):
        spent = sum((_money(r["actual_cny"]) for r in data["requests"].values() if r["status"] == "success"), Decimal(0))
        reserved = sum((_money(r["reserved_cny"]) for r in data["requests"].values() if r["status"] in _HELD_STATES), Decimal(0))
        return spent, reserved

    def summary(self) -> dict:
        with _file_lock(self.lock_path):
            data = self._load()
            changed = False
            for request_id, record in data["requests"].items():
                if record["status"] == "submitting":
                    # Nonblocking only: an active owner may be waiting for this
                    # short ledger lock before it records a received response.
                    with _file_lock(self._request_lock(request_id), blocking=False) as stale:
                        if stale:
                            record.update(status="unknown", reason="interrupted_submission")
                            record["attempts"][-1]["status"] = "unknown"
                            changed = True
            if changed:
                self._save(data)
            spent, reserved = self._totals(data)
            return {"budget_cny": float(self.budget), "stop_cny": float(self.stop),
                    "spent_cny": float(spent), "reserved_cny": float(reserved),
                    "committed_cny": float(spent + reserved),
                    "remaining_cny": float(max(Decimal(0), self.budget - spent - reserved)),
                    "requests": data["requests"]}

    def _mutate(self, request_id, callback):
        with _file_lock(self.lock_path):
            data = self._load()
            record = data["requests"][request_id]
            callback(record)
            record["updated_at"] = time.time()
            self._save(data)
            return record

    def _cancel(self, request_id):
        def update(record):
            record["status"] = "cancelled"
            if record["attempts"] and record["attempts"][-1]["status"] == "submitting":
                record["attempts"][-1]["status"] = "cancelled_before_send"
                record["attempts"][-1]["actual_cny"] = 0.0
        self._mutate(request_id, update)
        raise CloudCancelled("The request was cancelled before transmission.") from None

    def _unknown(self, request_id, reason):
        def update(record):
            record["status"] = "unknown"
            record["reason"] = reason
            if record["attempts"]:
                record["attempts"][-1]["status"] = "unknown"
        self._mutate(request_id, update)

    def _finish_local(self, request_id, record, actual_cost):
        try:
            body = Path(record["raw_path"]).read_bytes()
            if hashlib.sha256(body).hexdigest() != record["raw_sha256"]:
                raise ValueError()
            payload = _json_object(body)
        except (OSError, KeyError, ValueError, UnicodeError):
            if record["status"] == "received":
                self._unknown(request_id, "invalid_success_response")
                raise SubmissionUnknown("The paid response is saved but cannot be parsed or verified; it will not be resubmitted.") from None
            raise CloudRequestError("The saved response is missing or fails integrity verification; it will not be resubmitted.") from None
        if record["status"] == "success":
            return payload
        actual = _money(record["reserved_cny"])
        source = "reservation"
        if actual_cost is not None:
            try:
                actual = _money(actual_cost(payload))
                source = "usage"
            except Exception:
                pass
        def update(current):
            current["status"] = "success"
            current["actual_cny"] = float(actual)
            current["cost_source"] = source
            current["attempts"][-1]["status"] = "success"
            current["attempts"][-1]["actual_cny"] = float(actual)
        self._mutate(request_id, update)
        return payload

    def execute(self, request_id: str, provider: str, reserved_cny: float, raw_path: Path,
                send: Callable[[], HttpResponse], stop_event=None,
                actual_cost: Callable[[dict], float] | None = None,
                before_submit: Callable[[], None] | None = None) -> dict:
        """Admit new transmissions without holding the shared budget lock.

        The optional hook runs under the exact request lock before reserving
        new funds or submitting a restored/retried request. Hook failures are
        local failures, not evidence of transmission, and leave existing
        reservations untouched. Saved successful responses never need the hook.
        """
        amount = _money(reserved_cny)
        if not isinstance(request_id, str) or not request_id or not isinstance(provider, str) or not provider:
            raise ValueError("A stable request ID and provider are required.")
        requested_raw_path = Path(raw_path)
        raw_path = requested_raw_path.resolve()
        def require_unused_response():
            # Keep the lexical entry: resolve() follows a dangling symlink,
            # whose nonexistent target would otherwise appear unoccupied.
            _require_unused_response_path(requested_raw_path)
            if requested_raw_path != raw_path:
                _require_unused_response_path(raw_path)
        if raw_path == self.path or raw_path == self.lock_path:
            raise CloudRequestError("The response path conflicts with the budget ledger.")
        with _file_lock(self._request_lock(request_id), blocking=False) as owns_request:
            if not owns_request:
                raise SubmissionUnknown("This request is already in progress; a duplicate submission was blocked.")
            admitted = before_submit is None
            needs_admission = False
            while True:
                with _file_lock(self.lock_path):
                    # Reload after admission: another request may have spent or
                    # reserved money while the shared lock was released.
                    data = self._load()
                    record = data["requests"].get(request_id)
                    if record is not None:
                        if record["provider"] != provider or _money(record["reserved_cny"]) != amount or Path(record["raw_path"]) != raw_path:
                            raise CloudRequestError("The request ID already belongs to a different request configuration.")
                        if record["status"] == "submitting":
                            record["status"] = "unknown"
                            record["reason"] = "interrupted_submission"
                            record["attempts"][-1]["status"] = "unknown"
                            self._save(data)
                        if record["status"] == "unknown":
                            raise SubmissionUnknown("The earlier submission may have been billed; automatic resubmission is blocked.")
                        if record["status"] == "rejected":
                            raise CloudRequestError("The earlier request was rejected and has no automatic attempts remaining.")
                        if record["status"] == "cancelled" and len(record["attempts"]) >= 3:
                            record["status"] = "rejected"
                            self._save(data)
                            raise CloudRequestError("The request has no automatic attempts remaining.")
                        if record["status"] in ("received", "success"):
                            break
                        # A definitely unsent reservation can outlive a file
                        # arriving at its response path. Refuse before changing
                        # attempts or reacquiring cancelled funds.
                        require_unused_response()
                    if not admitted:
                        if stop_event is not None and stop_event.is_set():
                            raise CloudCancelled("The request was cancelled before transmission.")
                        if record is not None and record["status"] in ("reserved", "retry_wait"):
                            # Funds are already held. Validate these inputs only
                            # after any persisted rate-limit delay has elapsed.
                            needs_admission = True
                            break
                    else:
                        if record is None or record["status"] == "cancelled":
                            if stop_event is not None and stop_event.is_set():
                                raise CloudCancelled("The request was cancelled before transmission.")
                            spent, reserved = self._totals(data)
                            if spent + reserved + amount >= self.stop or spent + reserved + amount > self.budget:
                                raise BudgetExceeded("The next request would reach the configured spending stop line.")
                            if record is None:
                                require_unused_response()
                                if any(Path(r["raw_path"]) == raw_path for r in data["requests"].values()):
                                    raise CloudRequestError("The response path is already in use.")
                                record = {"provider": provider, "reserved_cny": float(amount), "raw_path": str(raw_path),
                                          "status": "reserved", "attempts": [], "created_at": time.time()}
                                data["requests"][request_id] = record
                            else:
                                record["status"] = "reserved"
                            self._save(data)
                        break
                if record is not None and record["status"] == "cancelled":
                    # Cancellation may retain a known 429 deadline. Wait before
                    # admission and reacquiring funds, keeping both locks and
                    # input checks in the same order as other restored retries.
                    delay = max(0.0, record.get("next_attempt_at", 0) - time.time())
                    if delay and _wait_retry(delay, stop_event):
                        raise CloudCancelled("The request was cancelled before transmission.")
                before_submit()
                if stop_event is not None and stop_event.is_set():
                    raise CloudCancelled("The request was cancelled before transmission.")
                admitted = True
            if record["status"] in ("received", "success"):
                return self._finish_local(request_id, record, actual_cost)
            while True:
                if stop_event is not None and stop_event.is_set():
                    self._cancel(request_id)
                next_time = record.get("next_attempt_at", 0)
                delay = max(0.0, next_time - time.time())
                if delay:
                    cancelled = _wait_retry(delay, stop_event)
                    if cancelled:
                        self._cancel(request_id)
                if needs_admission and before_submit is not None:
                    # A 429 proves the previous attempt was rejected, but does
                    # not prove the input is still admissible for another send.
                    # Do not catch this with the transport-error handler below.
                    before_submit()
                    if stop_event is not None and stop_event.is_set():
                        raise CloudCancelled("The request was cancelled before transmission.")
                def begin(current):
                    # Admission and rate-limit waits may take time. Recheck
                    # before the durable send marker, retaining the unsent or
                    # rejected record if a local file appeared meanwhile.
                    require_unused_response()
                    if before_submit is not None:
                        if (current["provider"] != provider or _money(current["reserved_cny"]) != amount
                                or Path(current["raw_path"]) != raw_path
                                or current["status"] not in ("reserved", "retry_wait")):
                            raise CloudRequestError("The request changed before transmission.")
                    # Recheck immediately before every send, including resumed
                    # retries. Persistent state must never bypass this cap.
                    if len(current["attempts"]) >= 3:
                        raise CloudRequestError("The request has no automatic attempts remaining.")
                    current["status"] = "submitting"
                    current.pop("next_attempt_at", None)
                    current["attempts"].append({"number": len(current["attempts"]) + 1, "status": "submitting",
                                                "reserved_cny": current["reserved_cny"], "started_at": time.time()})
                record = self._mutate(request_id, begin)
                try:
                    response = send()
                except CloudCancelled:
                    self._cancel(request_id)
                except Exception:
                    self._unknown(request_id, "transport_error")
                    raise SubmissionUnknown("Transmission or response retrieval failed; the request may have been billed.") from None
                if not isinstance(response, HttpResponse) or not isinstance(response.body, bytes) or not isinstance(response.headers, dict) or not isinstance(response.status, int):
                    self._unknown(request_id, "invalid_transport_response")
                    raise SubmissionUnknown("The transport returned an invalid response; automatic resubmission is blocked.")
                number = len(record["attempts"])
                response_path = raw_path if response.status == 200 else raw_path.with_name(raw_path.name + f".attempt-{number:02d}.http-{response.status}.body")
                try:
                    _write_bytes(response_path, response.body, replace=False)
                except OSError:
                    self._unknown(request_id, "response_persistence_failed")
                    raise SubmissionUnknown("The response could not be saved safely; automatic resubmission is blocked.") from None
                digest = hashlib.sha256(response.body).hexdigest()
                delay = _retry_delay(response.headers, number) if response.status == 429 else 0
                def received(current):
                    attempt = current["attempts"][-1]
                    attempt.update(http_status=response.status, raw_path=str(response_path), raw_sha256=digest, completed_at=time.time())
                    if response.status == 200:
                        current.update(status="received", raw_sha256=digest)
                        attempt["status"] = "received"
                    elif response.status == 429 and number < 3:
                        current.update(status="retry_wait", next_attempt_at=time.time() + delay)
                        attempt.update(status="rejected", actual_cny=0.0)
                    elif response.status in (400, 401, 403, 422, 429):
                        current["status"] = "rejected"
                        attempt.update(status="rejected", actual_cny=0.0)
                    else:
                        current.update(status="unknown", reason="ambiguous_http_response")
                        attempt["status"] = "unknown"
                record = self._mutate(request_id, received)
                if record["status"] == "received":
                    return self._finish_local(request_id, record, actual_cost)
                if record["status"] == "unknown":
                    raise SubmissionUnknown("The HTTP response does not prove the request was unbilled; automatic resubmission is blocked.")
                if record["status"] == "rejected":
                    raise CloudRequestError(f"The service rejected the request (HTTP {response.status}); no automatic attempts remain.")
                # Wait using the original delay so event-based cancellation can
                # interrupt immediately; persisted deadline also covers restart.
                cancelled = _wait_retry(delay, stop_event)
                if cancelled:
                    self._cancel(request_id)
                record["next_attempt_at"] = 0
                needs_admission = True
