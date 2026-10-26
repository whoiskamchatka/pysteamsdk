import ctypes
import errno
import os
import platform
import struct
import sys
import sysconfig
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import BinaryIO

from pysteamsdk.errors import (
    IncompatibleLibraryError,
    LibraryLoadError,
    LibraryNotFoundError,
    MissingExportsError,
    UnsupportedPlatformError,
)

ENV_VAR = "PYSTEAMSDK_LIB"

# What init, shutdown and manual dispatch will call. SteamAPI_InitFlat appeared in SDK 1.59,
# so finding these proves the file is a recent steam_api, not that it matches SDK 1.65.
REQUIRED_EXPORTS = (
    "SteamAPI_InitFlat",
    "SteamAPI_Shutdown",
    "SteamAPI_GetHSteamPipe",
    "SteamAPI_ManualDispatch_Init",
    "SteamAPI_ManualDispatch_RunFrame",
    "SteamAPI_ManualDispatch_GetNextCallback",
    "SteamAPI_ManualDispatch_FreeLastCallback",
    "SteamAPI_ManualDispatch_GetAPICallResult",
)

# File name, and the folder under the SDK's redistributable_bin that ships it.
_LIBRARIES = {
    ("win32", "x86_64"): ("steam_api64.dll", "win64"),
    ("linux", "x86_64"): ("libsteam_api.so", "linux64"),
    ("darwin", "x86_64"): ("libsteam_api.dylib", "osx"),
    ("darwin", "arm64"): ("libsteam_api.dylib", "osx"),
}
_MACHINE_ALIASES = {
    "amd64": "x86_64",
    "aarch64": "arm64",
    "win32": "x86",
    "i386": "x86",
    "i686": "x86",
}
# Windows reports any wrong binary as "not a valid Win32 application" and glibc reports a wrong
# architecture as "No such file or directory", so a failed load reads the header to say why.
# macOS keeps dyld's own message.
_FORMATS = {"win32": "PE", "linux": "ELF"}
_FORMAT_NAMES = {"PE": "a Windows DLL", "ELF": "a Linux shared library"}
_ARCH_NAMES = {"x86_64": "64-bit x86-64", "x86": "32-bit x86", "arm64": "64-bit arm64"}
# PE IMAGE_FILE_MACHINE_* and ELF e_machine values.
_ARCHES = {
    ("PE", 0x8664): "x86_64",
    ("PE", 0x14C): "x86",
    ("PE", 0xAA64): "arm64",
    ("ELF", 62): "x86_64",
    ("ELF", 3): "x86",
    ("ELF", 183): "arm64",
}


def find_library(path: str | os.PathLike[str] | None = None) -> Path:
    """Return the absolute path of the Steamworks library this process would load."""
    name, folder = _target(*_host())
    try:
        cwd: Path | None = Path.cwd()
    except FileNotFoundError:  # the working directory was deleted
        cwd = None
    return _find(path, os.environ.get(ENV_VAR), _entry_dir(), cwd, name, folder)


def load_library(
    path: str | os.PathLike[str] | None = None,
    *,
    opener: Callable[[str], ctypes.CDLL] = ctypes.CDLL,
) -> ctypes.CDLL:
    """Find and load the Steamworks library without initializing Steam; it is never unloaded."""
    found = find_library(path)
    system, machine, _ = _host()
    name, folder = _LIBRARIES[system, machine]
    use = f"Use redistributable_bin/{folder}/{name} from the Steamworks SDK."
    try:
        # Exports are extern "C" __cdecl (steamtypes.h, steam_api_common.h), the only convention
        # on 64-bit targets, so CDLL is right everywhere. Given an absolute path, ctypes on Windows
        # takes dependencies from the DLL's folder and system folders, never the cwd or PATH.
        library = opener(str(found))
    except OSError as exc:
        mismatch = _mismatch(found, system, machine)
        if mismatch:
            raise IncompatibleLibraryError(f"{found} {mismatch}. {use}", path=found) from exc
        raise LibraryLoadError(_load_failure(found, exc), path=found) from exc
    missing = tuple(export for export in REQUIRED_EXPORTS if not hasattr(library, export))
    if missing:
        raise MissingExportsError(
            f"{found} does not export {', '.join(missing)}. It is not the Steamworks API "
            f"library, or it is older than SDK 1.59. {use}",
            path=found,
            missing=missing,
        )
    return library


def _host() -> tuple[str, str, bool]:
    if sys.platform == "win32":
        # platform.machine() reports the OS, which differs from this process under x64
        # emulation on Windows on Arm; sysconfig reports the interpreter build.
        machine = sysconfig.get_platform().removeprefix("win-")
    else:
        machine = platform.machine()
    machine = machine.lower()
    return sys.platform, _MACHINE_ALIASES.get(machine, machine), sys.maxsize > 2**32


def _target(system: str, machine: str, is_64bit: bool) -> tuple[str, str]:
    if not is_64bit:
        raise UnsupportedPlatformError(
            "pysteamsdk needs 64-bit Python; this interpreter is 32-bit."
        )
    try:
        return _LIBRARIES[system, machine]
    except KeyError:
        raise UnsupportedPlatformError(
            f"pysteamsdk does not support {machine} on {system}. "
            "Supported: Windows x86-64, Linux x86-64, macOS x86-64 and arm64."
        ) from None


def _entry_dir() -> Path | None:
    entry: object
    if getattr(sys, "frozen", False):
        # Set by PyInstaller, cx_Freeze and py2exe; sys.executable is then the application.
        entry = sys.executable
    else:
        # Otherwise sys.executable is the interpreter, so only a __main__ file counts.
        entry = getattr(sys.modules.get("__main__"), "__file__", None)
    if not isinstance(entry, str) or not os.path.isabs(entry):
        return None
    # Like sys.path[0], follow symlinks to where the application really lives. Zipapps and pip's
    # Windows launchers run __main__.py from inside an archive file, hence the walk up.
    return next(
        (parent for parent in Path(os.path.realpath(entry)).parents if parent.is_dir()), None
    )


def _find(
    path: str | os.PathLike[str] | None,
    env_value: str | None,
    entry_dir: Path | None,
    cwd: Path | None,
    name: str,
    folder: str,
) -> Path:
    if path is not None:
        return _require(_absolute(path, cwd), name, from_env=False)
    if env_value:
        return _require(_absolute(env_value, cwd), name, from_env=True)
    searched = _unique(directory / name for directory in (entry_dir, cwd) if directory is not None)
    for candidate in searched:
        if candidate.is_file():
            return candidate
    listing = "".join(f"\n  {candidate}" for candidate in searched)
    raise LibraryNotFoundError(
        f"Could not find {name}. Looked for:{listing}\nCopy redistributable_bin/{folder}/{name} "
        f"from the Steamworks SDK next to your application, or set {ENV_VAR} to its path.",
        searched=tuple(searched),
    )


def _absolute(value: str | os.PathLike[str], cwd: Path | None) -> Path:
    # Without a working directory only an absolute path still names a file.
    return Path(os.path.abspath(cwd / value)) if cwd is not None else Path(value)


def _require(path: Path, name: str, *, from_env: bool) -> Path:
    if path.is_file():
        return path
    problem = f"is a directory, not {name}" if path.is_dir() else "does not exist"
    where = f"{ENV_VAR} points to {path}, which" if from_env else str(path)
    raise LibraryNotFoundError(
        f"Steamworks library not found: {where} {problem}.", path=path, searched=(path,)
    )


def _unique(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    unique = []
    for path in paths:
        key = os.path.normcase(os.path.realpath(path))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _mismatch(path: Path, system: str, machine: str) -> str | None:
    expected = _FORMATS.get(system)
    if expected is None:
        return None
    try:
        with path.open("rb") as file:
            found = _identify(file)
    except OSError:
        return None
    if found is None:
        return f"is not {_FORMAT_NAMES[expected]}"
    kind, arch = found
    if kind != expected:
        return f"is {_FORMAT_NAMES[kind]}, not {_FORMAT_NAMES[expected]}"
    if arch != machine:
        return (
            f"is built for {_ARCH_NAMES.get(arch, arch)}, but this Python is {_ARCH_NAMES[machine]}"
        )
    return None


def _identify(file: BinaryIO) -> tuple[str, str] | None:
    head = file.read(64)
    if head[:2] == b"MZ" and len(head) == 64:
        file.seek(struct.unpack_from("<I", head, 0x3C)[0])
        pe = file.read(6)
        if len(pe) == 6 and pe[:4] == b"PE\0\0":
            return "PE", _arch("PE", struct.unpack_from("<H", pe, 4)[0])
    elif head[:4] == b"\x7fELF" and len(head) >= 20:
        order = "<" if head[5] == 1 else ">"
        return "ELF", _arch("ELF", struct.unpack_from(order + "H", head, 18)[0])
    return None


def _arch(kind: str, code: int) -> str:
    return _ARCHES.get((kind, code), f"unknown CPU type {code:#x}")


def _load_failure(path: Path, exc: OSError) -> str:
    if isinstance(exc, FileNotFoundError):
        # ctypes on Windows raises this for ERROR_MOD_NOT_FOUND; the file exists, so a
        # library it depends on is missing.
        return f"Could not load {path}: a library it depends on was not found ({exc})."
    if exc.errno == errno.ENOEXEC:
        # WinError 193 for a file whose header matches this process: a truncated or corrupt DLL.
        return f"Could not load {path}: the file is damaged or incomplete ({exc})."
    return f"Could not load {path}: {exc}"
