import ctypes
import functools
import os
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import pysteamsdk
from pysteamsdk._native import lifecycle, loader
from pysteamsdk._native.lifecycle import ERR_MSG_SIZE, Lifecycle
from pysteamsdk._native.loader import REQUIRED_EXPORTS, load_library
from pysteamsdk.errors import (
    InitError,
    LibraryNotFoundError,
    LifecycleError,
    MissingExportsError,
    NoSteamClientError,
    NotInitializedError,
    VersionMismatchError,
)


class FakeSteam:
    def __init__(self, result: int = 0, message: bytes = b"", pipe: int = 7, user: int = 9) -> None:
        self.calls: list[str] = []
        self.buffers: list[bytes] = []
        self.result = result
        self.message = message
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        # Run inside a native call, after it is recorded: to fail it or observe state mid-call.
        self.hooks: dict[str, Callable[[], None]] = {}

        def init_flat(buffer: Any) -> int:
            self.calls.append("InitFlat")
            self.buffers.append(bytes(buffer))
            self.started.set()
            self.release.wait(5)
            self.hooks.get("InitFlat", lambda: None)()
            buffer.value = self.message
            return self.result

        def record(name: str, value: int | None) -> Any:
            def call() -> int | None:
                self.calls.append(name)
                self.hooks.get(name, lambda: None)()
                return value

            return call

        self.library: Any = SimpleNamespace(
            SteamAPI_InitFlat=init_flat,
            SteamAPI_Shutdown=record("Shutdown", None),
            SteamAPI_GetHSteamPipe=record("GetHSteamPipe", pipe),
            SteamAPI_GetHSteamUser=record("GetHSteamUser", user),
        )
        self.loads: list[Path] = []

    def load(self, path: Path) -> ctypes.CDLL:
        self.loads.append(path)
        return cast(ctypes.CDLL, self.library)


@pytest.fixture
def library(tmp_path: Path) -> Path:
    name, _ = loader._target(*loader._host())
    path = tmp_path / name
    path.write_bytes(b"")
    return path


def make(library: Path, **kwargs: Any) -> tuple[Lifecycle, FakeSteam]:
    steam = FakeSteam(**kwargs)
    return Lifecycle(load=steam.load), steam


def test_init_binds_declared_signatures_and_opens_a_session(library: Path) -> None:
    life, steam = make(library)
    session = life.init(library)
    assert steam.calls == ["InitFlat", "GetHSteamPipe", "GetHSteamUser"]
    assert steam.buffers == [bytes(ERR_MSG_SIZE)]
    assert (session.pipe, session.user, session.library) == (7, 9, steam.library)
    assert life.is_initialized()
    assert life.session() is session
    native = steam.library
    assert native.SteamAPI_InitFlat.argtypes == [ctypes.POINTER(ctypes.c_char * ERR_MSG_SIZE)]
    assert native.SteamAPI_InitFlat.restype is ctypes.c_int
    assert (native.SteamAPI_Shutdown.argtypes, native.SteamAPI_Shutdown.restype) == ([], None)
    for name in ("SteamAPI_GetHSteamPipe", "SteamAPI_GetHSteamUser"):
        function = getattr(native, name)
        assert (function.argtypes, function.restype) == ([], ctypes.c_int32)


@pytest.mark.parametrize(
    ("result", "error", "name"),
    [
        (1, InitError, "FailedGeneric"),
        (2, NoSteamClientError, "NoSteamClient"),
        (3, VersionMismatchError, "VersionMismatch"),
        (42, InitError, "unknown result"),
    ],
)
def test_failed_init_raises_and_leaves_no_session(
    library: Path, result: int, error: type[InitError], name: str
) -> None:
    life, steam = make(library, result=result, message=b"Steam said no")
    with pytest.raises(InitError) as raised:
        life.init(library)
    assert type(raised.value) is error
    assert (raised.value.result, raised.value.reason) == (result, "Steam said no")
    assert f"{name} ({result}): Steam said no" in str(raised.value)
    assert steam.calls == ["InitFlat"]
    assert not life.is_initialized()
    with pytest.raises(NotInitializedError):
        life.session()
    life.shutdown()
    assert steam.calls == ["InitFlat"]


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        (b"", ""),
        (b"  padded\n", "padded"),
        (b"caf\xc3\xa9 \xff", "café �"),
        (b"x" * (ERR_MSG_SIZE - 1), "x" * (ERR_MSG_SIZE - 1)),
    ],
)
def test_error_message_is_decoded(library: Path, message: bytes, reason: str) -> None:
    life, _ = make(library, result=1, message=message)
    with pytest.raises(InitError) as raised:
        life.init(library)
    assert raised.value.reason == reason
    assert (": " + reason + ". " in str(raised.value)) == bool(reason)


def test_missing_native_function_stops_before_init(library: Path) -> None:
    calls: list[str] = []
    native = SimpleNamespace(**dict.fromkeys(REQUIRED_EXPORTS))
    del native.SteamAPI_GetHSteamUser
    native.SteamAPI_InitFlat = lambda buffer: calls.append("InitFlat")

    def opener(path: str) -> ctypes.CDLL:
        return cast(ctypes.CDLL, native)

    life = Lifecycle(load=functools.partial(load_library, opener=opener))
    with pytest.raises(MissingExportsError, match="SteamAPI_GetHSteamUser"):
        life.init(library)
    assert calls == []
    assert not life.is_initialized()


def test_second_init_is_rejected_without_native_calls(library: Path) -> None:
    life, steam = make(library)
    life.init(library)
    with pytest.raises(LifecycleError, match="already initialized"):
        life.init(library)
    assert steam.calls == ["InitFlat", "GetHSteamPipe", "GetHSteamUser"]


def test_failed_init_can_be_retried_with_the_same_library(library: Path) -> None:
    life, steam = make(library, result=2)
    with pytest.raises(NoSteamClientError):
        life.init(library)
    steam.result = 0
    life.init()
    assert life.is_initialized()
    assert steam.loads == [library]
    assert steam.calls == ["InitFlat", "InitFlat", "GetHSteamPipe", "GetHSteamUser"]


def test_failed_load_can_be_retried(library: Path) -> None:
    life, steam = make(library)
    with pytest.raises(LibraryNotFoundError):
        life.init(library.parent / "missing" / library.name)
    life.init(library)
    assert steam.loads == [library]


def test_a_process_uses_only_one_library(library: Path, tmp_path: Path) -> None:
    other = tmp_path / "other" / library.name
    other.parent.mkdir()
    other.write_bytes(b"")
    life, steam = make(library, result=2)
    with pytest.raises(NoSteamClientError):
        life.init(library)
    with pytest.raises(LifecycleError, match="already loaded from"):
        life.init(other)
    steam.result = 0
    life.init(tmp_path / "other" / ".." / library.name)
    assert steam.loads == [library]
    assert steam.calls.count("InitFlat") == 2


def test_shutdown_without_init_does_nothing(library: Path) -> None:
    life, steam = make(library)
    life.shutdown()
    life.shutdown()
    assert steam.calls == []
    assert steam.loads == []


def test_shutdown_runs_once_and_is_final(library: Path) -> None:
    life, steam = make(library)
    life.init(library)
    life.shutdown()
    life.shutdown()
    assert steam.calls == ["InitFlat", "GetHSteamPipe", "GetHSteamUser", "Shutdown"]
    assert not life.is_initialized()
    with pytest.raises(LifecycleError, match="cannot be initialized again"):
        life.init(library)
    assert steam.calls.count("InitFlat") == 1


def test_session_is_unusable_after_shutdown(library: Path) -> None:
    life, steam = make(library)
    session = life.init(library)
    life.shutdown()
    assert session.closed
    for attribute in ("library", "pipe", "user"):
        with pytest.raises(NotInitializedError, match="shut down"):
            getattr(session, attribute)
    with pytest.raises(NotInitializedError):
        life.session()
    assert steam.calls[-1] == "Shutdown"


def test_concurrent_init_initializes_once(library: Path) -> None:
    life, steam = make(library)
    steam.release.clear()
    outcomes: list[str] = []

    def attempt() -> None:
        try:
            life.init(library)
            outcomes.append("initialized")
        except LifecycleError:
            outcomes.append("rejected")

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    assert steam.started.wait(5)
    steam.release.set()
    for thread in threads:
        thread.join(5)
    assert sorted(outcomes) == ["initialized"] + ["rejected"] * 7
    assert steam.calls.count("InitFlat") == 1


def test_concurrent_shutdown_shuts_down_once(library: Path) -> None:
    life, steam = make(library)
    life.init(library)
    barrier = threading.Barrier(8)

    def stop() -> None:
        barrier.wait(5)
        life.shutdown()

    threads = [threading.Thread(target=stop) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert steam.calls.count("Shutdown") == 1


def test_init_sets_no_app_id_and_touches_no_files(
    library: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    life, _ = make(library)
    before = dict(os.environ), sorted(tmp_path.iterdir())
    life.init(library)
    life.shutdown()
    assert (dict(os.environ), sorted(tmp_path.iterdir())) == before


def test_public_functions_share_one_process_lifecycle(
    library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    life, steam = make(library)
    monkeypatch.setattr(lifecycle, "_lifecycle", life)
    assert not pysteamsdk.is_initialized()
    pysteamsdk.init(library)
    assert pysteamsdk.is_initialized()
    with pytest.raises(LifecycleError):
        pysteamsdk.init()
    pysteamsdk.shutdown()
    assert not pysteamsdk.is_initialized()
    assert steam.calls == ["InitFlat", "GetHSteamPipe", "GetHSteamUser", "Shutdown"]


@pytest.mark.parametrize("failing", ["GetHSteamPipe", "GetHSteamUser"])
@pytest.mark.parametrize("error", [RuntimeError("boom"), KeyboardInterrupt()])
def test_failure_after_native_init_shuts_steam_down(
    library: Path, failing: str, error: BaseException
) -> None:
    life, steam = make(library)

    def fail() -> None:
        raise error

    steam.hooks[failing] = fail
    with pytest.raises(type(error)):
        life.init(library)
    assert steam.calls[-2:] == [failing, "Shutdown"]
    assert steam.calls.count("Shutdown") == 1
    assert not life.is_initialized()
    life.shutdown()
    with pytest.raises(LifecycleError, match="cannot be initialized again"):
        life.init(library)
    assert steam.calls.count("InitFlat") == 1
    assert steam.calls.count("Shutdown") == 1


def test_interrupted_native_init_failure_is_not_shut_down(library: Path) -> None:
    life, steam = make(library, result=2)

    def interrupt() -> None:
        raise KeyboardInterrupt

    steam.hooks["InitFlat"] = interrupt
    with pytest.raises(KeyboardInterrupt):
        life.init(library)
    assert steam.calls == ["InitFlat"]
    steam.hooks.clear()
    steam.result = 0
    life.init(library)
    assert life.is_initialized()


def test_session_is_closed_before_native_shutdown_runs(library: Path) -> None:
    life, steam = make(library)
    session = life.init(library)
    seen: list[tuple[bool, bool]] = []
    steam.hooks["Shutdown"] = lambda: seen.append((session.closed, life.is_initialized()))
    life.shutdown()
    assert seen == [(True, False)]


def test_failing_native_shutdown_still_ends_the_session(library: Path) -> None:
    life, steam = make(library)
    session = life.init(library)

    def fail() -> None:
        raise RuntimeError("shutdown failed")

    steam.hooks["Shutdown"] = fail
    with pytest.raises(RuntimeError, match="shutdown failed"):
        life.shutdown()
    assert session.closed
    assert not life.is_initialized()
    life.shutdown()
    with pytest.raises(LifecycleError):
        life.init(library)
    assert steam.calls == ["InitFlat", "GetHSteamPipe", "GetHSteamUser", "Shutdown"]


def test_shutdown_waits_for_an_init_in_progress(library: Path) -> None:
    life, steam = make(library)
    steam.release.clear()
    initializing = threading.Thread(target=life.init, args=(library,))
    initializing.start()
    assert steam.started.wait(5)
    seen_during_init: list[bool] = []
    steam.hooks["InitFlat"] = lambda: seen_during_init.append(life.is_initialized())
    stopping = threading.Thread(target=life.shutdown)
    stopping.start()
    stopping.join(0.2)
    assert stopping.is_alive()
    steam.release.set()
    initializing.join(5)
    stopping.join(5)
    assert seen_during_init == [False]
    assert steam.calls == ["InitFlat", "GetHSteamPipe", "GetHSteamUser", "Shutdown"]
    assert not life.is_initialized()
