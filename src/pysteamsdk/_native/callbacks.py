"""Deliver Steam's callbacks and asynchronous call results, through manual dispatch.

steam_api.h's manual dispatch loop hands over one message at a time, which stays valid only until
SteamAPI_ManualDispatch_FreeLastCallback. So run() copies each message's record, and the result
of each finished call, inside that loop, and calls the handlers only once the loop is done:
outside any call into Steam and any lock, where a handler may call Steam, register or cancel
handlers, dispatch again or shut Steam down. Handlers get their own copies, which stay valid.

A session has one dispatcher, which callbacks() makes on first use; nothing else may take
messages from the pipe it reads.
"""

import ctypes
import threading
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from pysteamsdk._native.bindings import BindingError
from pysteamsdk._native.lifecycle import Session, inside_callback
from pysteamsdk.errors import LifecycleError, NotInitializedError

_making = threading.Lock()


class Registration:
    """A handler waiting for callbacks or a call result, until cancelled or delivered."""

    __slots__ = ("_owner", "active", "handler", "key", "record")

    def __init__(
        self, owner: "Callbacks", key: tuple[str, int], record: type, handler: Callable[..., object]
    ) -> None:
        self._owner = owner
        self.key = key
        self.record = record
        self.handler = handler
        self.active = True

    def cancel(self) -> None:
        """Stop delivering to the handler; it is not called again, even later in a run."""
        self._owner._forget(self)


@dataclass(frozen=True)
class _Message:
    """One message as Steam sent it, copied: the callback record's bytes, and for a finished
    call its handle, the ID of the record it completed with, and that record's bytes, or None
    when Steam had none."""

    callback: int
    data: bytes | None
    call: int | None = None
    result_id: int = 0
    result: bytes | None = None
    failed: bool = False


class Callbacks:
    """The callbacks and call results of one session, delivered by run()."""

    def __init__(self, declarations: ModuleType, session: Session) -> None:
        names = ("CallbackMsg_t", "SteamAPICallCompleted_t", "CALLBACK_IDS")
        missing = [name for name in names if not hasattr(declarations, name)]
        if missing:
            raise BindingError(
                f"{declarations.__name__} has no {' or '.join(missing)}, which dispatching "
                "callbacks needs; their layouts may not be verified on this target."
            )
        self._session = session
        self._pipe = session.pipe
        self._ids: dict[type, int] = dict(declarations.CALLBACK_IDS)
        self._message = declarations.CallbackMsg_t
        self._completed = declarations.SteamAPICallCompleted_t
        self._completed_id = self._ids[self._completed]
        library = session.library
        pipe = ctypes.c_int32
        # steam_api.h declares these, and HSteamPipe is an int32 and SteamAPICall_t a uint64.
        self._init = ctypes.CFUNCTYPE(None)(("SteamAPI_ManualDispatch_Init", library))
        self._run_frame = ctypes.CFUNCTYPE(None, pipe)(
            ("SteamAPI_ManualDispatch_RunFrame", library)
        )
        self._next = ctypes.CFUNCTYPE(ctypes.c_bool, pipe, ctypes.POINTER(self._message))(
            ("SteamAPI_ManualDispatch_GetNextCallback", library)
        )
        self._free = ctypes.CFUNCTYPE(None, pipe)(
            ("SteamAPI_ManualDispatch_FreeLastCallback", library)
        )
        self._result = ctypes.CFUNCTYPE(
            ctypes.c_bool,
            pipe,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_bool),
        )(("SteamAPI_ManualDispatch_GetAPICallResult", library))
        self._lock = threading.Lock()
        # Held while messages are taken from the pipe, which Steam allows one reader at a time.
        self._reading = threading.Lock()
        self._handlers: dict[int, list[Registration]] = {}
        self._expected: dict[int, Registration] = {}
        session.call("SteamAPI_ManualDispatch_Init", self._init)

    def register(self, record: type, handler: Callable[[Any], object]) -> Registration:
        """Call ``handler`` with a copy of each ``record`` callback Steam sends."""
        key = ("callback", self._callback_id(record))
        registration = Registration(self, key, record, handler)
        self._require_open()
        with self._lock:
            self._handlers.setdefault(key[1], []).append(registration)
        return registration

    def expect(self, call: int, record: type, handler: Callable[[Any], object]) -> Registration:
        """Call ``handler`` once with the ``record`` the asynchronous call ``call`` completes with,
        or with None when the call failed or completed with another record. Register before the
        next run() can see the call finish."""
        if not isinstance(call, int) or not 0 < call < 1 << 64:
            raise ValueError(f"{call!r} is not a call handle; a call that did not start gives 0")
        self._callback_id(record)
        registration = Registration(self, ("call", call), record, handler)
        self._require_open()
        with self._lock:
            if call in self._expected:
                raise ValueError(f"call {call:#x} already has a handler")
            self._expected[call] = registration
        return registration

    def run(self) -> int:
        """Deliver what Steam has sent since the last run, and return how many handlers ran.

        Every handler runs even if one raises; then their exceptions, and any message these
        declarations do not match, are raised together as an ExceptionGroup.
        """
        if inside_callback():
            raise LifecycleError("Callbacks cannot be dispatched from a function Steam is calling.")
        with self._reading:
            try:
                messages = self._session.call("callback dispatch", self._read)
            except NotInitializedError:
                self._clear()
                raise
        return self._deliver(messages)

    def _read(self) -> list[_Message]:
        self._run_frame(self._pipe)
        message = self._message()
        found = []
        while self._next(self._pipe, ctypes.byref(message)):
            try:
                found.append(self._copy(message))
            finally:
                self._free(self._pipe)
        return found

    def _copy(self, message: Any) -> _Message:
        callback, size = int(message.m_iCallback), int(message.m_cubParam)
        with self._lock:
            wanted = callback in self._handlers or callback == self._completed_id
        if not wanted:
            return _Message(callback, None)
        data = (
            ctypes.string_at(message.m_pubParam, size) if message.m_pubParam and size > 0 else b""
        )
        if callback != self._completed_id or len(data) != ctypes.sizeof(self._completed):
            return _Message(callback, data)
        completed = self._completed.from_buffer_copy(data)
        # As in steam_api.h's loop, a finished call's result is fetched whether or not a handler
        # waits for it.
        result = (ctypes.c_uint8 * completed.m_cubParam)()
        failed = ctypes.c_bool()
        found = self._result(
            self._pipe,
            completed.m_hAsyncCall,
            result,
            completed.m_cubParam,
            completed.m_iCallback,
            ctypes.byref(failed),
        )
        return _Message(
            callback,
            data,
            call=int(completed.m_hAsyncCall),
            result_id=int(completed.m_iCallback),
            result=bytes(result) if found else None,
            failed=failed.value,
        )

    def _deliver(self, messages: list[_Message]) -> int:
        errors: list[Exception] = []
        ran = 0
        for message in messages:
            if self._session.closed:
                self._clear()
                break
            if message.call is not None:
                with self._lock:
                    waiting = self._expected.pop(message.call, None)
                if waiting is not None and waiting.active:
                    waiting.active = False
                    value = None
                    if message.result is not None and not message.failed:
                        value = self._decode(waiting.record, message.result_id, message.result)
                        if isinstance(value, BindingError):
                            errors.append(value)
                            value = None
                    ran += self._call(waiting.handler, value, errors)
            with self._lock:
                handlers = list(self._handlers.get(message.callback, ()))
            # Each record is decoded once a message, and each handler gets its own copy.
            decoded: dict[type, Any] = {}
            for registration in handlers:
                # A handler may have shut Steam down, or cancelled a later one.
                if self._session.closed or not registration.active or message.data is None:
                    continue
                record = registration.record
                if record not in decoded:
                    decoded[record] = self._decode(record, message.callback, message.data)
                    if isinstance(decoded[record], BindingError):
                        errors.append(decoded[record])
                if isinstance(decoded[record], BindingError):
                    continue
                value = record.from_buffer_copy(message.data)  # type: ignore[attr-defined]
                ran += self._call(registration.handler, value, errors)
        if errors:
            raise ExceptionGroup("callback handlers or messages failed", errors)
        return ran

    def _decode(self, record: type, callback: int, data: bytes) -> Any:
        """A copy of ``record`` from Steam's bytes, or the BindingError saying why they differ."""
        expected = self._ids[record]
        if callback != expected or len(data) != ctypes.sizeof(record):
            return BindingError(
                f"Steam sent callback {callback} with {len(data)} bytes where {record.__name__}, "
                f"callback {expected}, has {ctypes.sizeof(record)}; the declarations may not "
                "match this Steam client"
            )
        return record.from_buffer_copy(data)  # type: ignore[attr-defined]

    @staticmethod
    def _call(handler: Callable[[Any], object], value: Any, errors: list[Exception]) -> int:
        try:
            handler(value)
        except Exception as exc:
            errors.append(exc)
        return 1

    def _callback_id(self, record: type) -> int:
        callback = self._ids.get(record)
        if callback is None:
            name = getattr(record, "__name__", repr(record))
            raise TypeError(f"{name} is not a callback record of these declarations")
        return callback

    def _require_open(self) -> None:
        if self._session.closed:
            self._clear()
            raise NotInitializedError("Steam has been shut down; no callback can arrive.")

    def _forget(self, registration: Registration) -> None:
        with self._lock:
            registration.active = False
            kind, value = registration.key
            if kind == "call":
                if self._expected.get(value) is registration:
                    del self._expected[value]
            else:
                handlers = self._handlers.get(value, [])
                if registration in handlers:
                    handlers.remove(registration)

    def _clear(self) -> None:
        # Nothing arrives after shutdown, so the handlers need not be kept.
        with self._lock:
            for registration in self._expected.values():
                registration.active = False
            for handlers in self._handlers.values():
                for registration in handlers:
                    registration.active = False
            self._expected.clear()
            self._handlers.clear()


def callbacks(declarations: ModuleType, session: Session) -> Callbacks:
    """The dispatcher of ``session``, which the first call makes and switches Steam over to."""
    with _making:
        found = session.dispatcher
        if found is None:
            found = session.dispatcher = Callbacks(declarations, session)
    assert isinstance(found, Callbacks)
    return found
