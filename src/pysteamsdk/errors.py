from pathlib import Path


class SteamError(Exception):
    """Base class for all pysteamsdk errors."""


class UnsupportedPlatformError(SteamError):
    """The running Python build or operating system is not supported."""


class NativeLibraryError(SteamError):
    """The Steamworks native library cannot be used."""

    def __init__(self, message: str, *, path: Path | None = None) -> None:
        super().__init__(message)
        self.path = path


class LibraryNotFoundError(NativeLibraryError):
    """No Steamworks library exists at the configured or searched locations."""

    def __init__(
        self, message: str, *, path: Path | None = None, searched: tuple[Path, ...] = ()
    ) -> None:
        super().__init__(message, path=path)
        self.searched = searched


class IncompatibleLibraryError(NativeLibraryError):
    """The file is not a Steamworks library this process can load."""


class MissingExportsError(IncompatibleLibraryError):
    """The library loaded but lacks functions pysteamsdk requires."""

    def __init__(
        self, message: str, *, path: Path | None = None, missing: tuple[str, ...] = ()
    ) -> None:
        super().__init__(message, path=path)
        self.missing = missing


class LibraryLoadError(NativeLibraryError):
    """The operating system failed to load the library."""


class InitError(SteamError):
    """SteamAPI_InitFlat reported a failure."""

    def __init__(self, message: str, *, result: int | None = None, reason: str = "") -> None:
        super().__init__(message)
        self.result = result
        self.reason = reason


class NoSteamClientError(InitError):
    """InitFlat returned NoSteamClient: Steam could not be reached, probably not running."""


class VersionMismatchError(InitError):
    """InitFlat returned VersionMismatch: the Steam client appears to be out of date."""


class LifecycleError(SteamError):
    """Steam was initialized or shut down in an unsupported order."""


class NotInitializedError(LifecycleError):
    """Steam is not initialized, or has been shut down."""
