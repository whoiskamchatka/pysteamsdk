import ctypes
import os
import threading
from collections.abc import Callable
from pathlib import Path

from pysteamsdk._native.loader import find_library, load_library
from pysteamsdk.errors import (
    InitError,
    LifecycleError,
    NoSteamClientError,
    NotInitializedError,
    VersionMismatchError,
)

# steam_api_common.h: typedef char SteamErrMsg[ k_cchMaxSteamErrMsg ], with the constant 1024.
ERR_MSG_SIZE = 1024
ErrMsg = ctypes.c_char * ERR_MSG_SIZE

# ESteamAPIInitResult in steam_api.h. Its values fit in a C int on every target. FailedGeneric
# covers more than its comment says: with no Steam installed, Linux reports it, not NoSteamClient.
_INIT_FAILURES: dict[int, tuple[type[InitError], str, str]] = {
    1: (
        InitError,
        "FailedGeneric",
        "Check that Steam is installed, running and logged in, and that the app ID is known: "
        "launch through Steam or put steam_appid.txt in the working directory.",
    ),
    2: (
        NoSteamClientError,
        "NoSteamClient",
        "Steam could not be reached; it is probably not running or not logged in.",
    ),
    3: (
        VersionMismatchError,
        "VersionMismatch",
        "The Steam client appears to be out of date; updating it may help.",
    ),
}


class _Api:
    def __init__(self, library: ctypes.CDLL, path: Path) -> None:
        self.library = library
        self.path = path
        # steam_api.h: S_API ESteamAPIInitResult S_CALLTYPE SteamAPI_InitFlat( SteamErrMsg * );
        # The enum is returned as a 32-bit int on every target; all values are small and positive.
        self.init_flat = library.SteamAPI_InitFlat
        self.init_flat.argtypes = [ctypes.POINTER(ErrMsg)]
        self.init_flat.restype = ctypes.c_int
        # steam_api.h: S_API void S_CALLTYPE SteamAPI_Shutdown();
        self.shutdown = library.SteamAPI_Shutdown
        self.shutdown.argtypes = []
        self.shutdown.restype = None
        # steam_api_internal.h: HSteamPipe / HSteamUser, both typedefs of int32.
        self.get_pipe = library.SteamAPI_GetHSteamPipe
        self.get_pipe.argtypes = []
        self.get_pipe.restype = ctypes.c_int32
        self.get_user = library.SteamAPI_GetHSteamUser
        self.get_user.argtypes = []
        self.get_user.restype = ctypes.c_int32


class Session:
    """The process's initialized Steamworks API; unusable once Steam is shut down."""

    def __init__(self, api: _Api, pipe: int, user: int) -> None:
        self._api = api
        self._pipe = pipe
        self._user = user
        self._open = True

    @property
    def closed(self) -> bool:
        """Whether Steam has been shut down since this session began."""
        return not self._open

    @property
    def library(self) -> ctypes.CDLL:
        """The loaded steam_api library."""
        self._require_open()
        return self._api.library

    @property
    def pipe(self) -> int:
        """The HSteamPipe returned by SteamAPI_GetHSteamPipe after init."""
        self._require_open()
        return self._pipe

    @property
    def user(self) -> int:
        """The HSteamUser returned by SteamAPI_GetHSteamUser after init."""
        self._require_open()
        return self._user

    def _require_open(self) -> None:
        if not self._open:
            raise NotInitializedError(
                "Steam has been shut down; this session can no longer be used."
            )


class Lifecycle:
    """Process-wide Steamworks initialization state: one library, at most one session."""

    def __init__(self, load: Callable[[Path], ctypes.CDLL] = load_library) -> None:
        self._load = load
        self._lock = threading.Lock()
        self._api: _Api | None = None
        self._session: Session | None = None
        self._shut_down = False

    def init(self, library: str | os.PathLike[str] | None = None) -> Session:
        """Load the library if needed and initialize Steam."""
        with self._lock:
            if self._session is not None:
                raise LifecycleError("Steam is already initialized in this process.")
            if self._shut_down:
                # Valve does not document initializing again after SteamAPI_Shutdown.
                raise LifecycleError(
                    "Steam was shut down in this process and cannot be initialized again."
                )
            api = self._bind(library)
            message = ErrMsg()
            result = None
            try:
                result = api.init_flat(message)
                if result != 0:
                    raise _init_error(result, message.value)
                self._session = Session(api, api.get_pipe(), api.get_user())
                return self._session
            except BaseException:
                if result == 0:
                    # Steam is up natively but untracked; shut it down rather than leak it.
                    # An interrupt landing before `result` is assigned cannot be caught.
                    self._session = None
                    self._shut_down = True
                    api.shutdown()
                raise

    def shutdown(self) -> None:
        """Shut Steam down if it is initialized; otherwise do nothing."""
        with self._lock:
            session, self._session = self._session, None
            if session is None:
                return
            self._shut_down = True
            session._open = False
            session._api.shutdown()

    def is_initialized(self) -> bool:
        """Whether a session is active."""
        return self._session is not None

    def session(self) -> Session:
        """Return the active session."""
        session = self._session
        if session is None:
            raise NotInitializedError("Steam is not initialized; call pysteamsdk.init() first.")
        return session

    def _bind(self, library: str | os.PathLike[str] | None) -> _Api:
        if self._api is None:
            path = find_library(library)
            self._api = _Api(self._load(path), path)
        elif library is not None and not _same_file(find_library(library), self._api.path):
            # A loaded steam_api is never unloaded, and a second copy would hold its own state.
            raise LifecycleError(
                f"The Steamworks library is already loaded from {self._api.path}; "
                "a process can use only one."
            )
        return self._api


def _same_file(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _init_error(result: int, raw: bytes) -> InitError:
    reason = raw.decode("utf-8", "replace").strip()
    error, name, hint = _INIT_FAILURES.get(
        result, (InitError, "unknown result", "This SDK does not document this result.")
    )
    detail = f": {reason}" if reason else ""
    return error(
        f"SteamAPI_InitFlat failed with {name} ({result}){detail}. {hint}",
        result=result,
        reason=reason,
    )


_lifecycle = Lifecycle()


def init(library: str | os.PathLike[str] | None = None) -> None:
    """Initialize Steam for this process, loading the Steamworks library on first use."""
    _lifecycle.init(library)


def shutdown() -> None:
    """Shut Steam down for the rest of the process; does nothing if Steam is not initialized."""
    _lifecycle.shutdown()


def is_initialized() -> bool:
    """Whether Steam is initialized in this process."""
    return _lifecycle.is_initialized()
