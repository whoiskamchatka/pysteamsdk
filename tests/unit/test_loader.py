import ctypes
import errno
import os
import struct
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from pysteamsdk._native import loader
from pysteamsdk._native.loader import ENV_VAR, REQUIRED_EXPORTS, find_library, load_library
from pysteamsdk.errors import (
    IncompatibleLibraryError,
    LibraryLoadError,
    LibraryNotFoundError,
    MissingExportsError,
    NativeLibraryError,
    SteamError,
    UnsupportedPlatformError,
)

NAME = "steam_api64.dll"
PE_X64, PE_X86, PE_ARM = 0x8664, 0x14C, 0x1C0
ELF_X64, ELF_X86, ELF_ARM64 = 62, 3, 183


def pe(machine: int) -> bytes:
    return b"MZ" + bytes(0x3A) + struct.pack("<I", 0x40) + b"PE\0\0" + struct.pack("<H", machine)


def elf(machine: int, bits: int = 64) -> bytes:
    ident = b"\x7fELF" + bytes([bits // 32, 1, 1]) + bytes(9)
    return ident + struct.pack("<HH", 3, machine)


# Headers only: real loaders reject these files, which some tests rely on.
HOST_HEADERS = {
    "win32": pe(PE_X64),
    "linux": elf(ELF_X64),
    "darwin": b"\xcf\xfa\xed\xfe" + bytes(28),
}
WRONG_ARCH = {"win32": (pe(PE_X86), "32-bit x86"), "linux": (elf(ELF_ARM64), "64-bit arm64")}


def write(path: Path, data: bytes = b"") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as exc:
        pytest.skip(f"cannot create symlinks here: {exc}")


class FakeOpener:
    def __init__(
        self, exports: tuple[str, ...] = REQUIRED_EXPORTS, error: OSError | None = None
    ) -> None:
        self.library = cast(ctypes.CDLL, SimpleNamespace(**dict.fromkeys(exports)))
        self.error = error
        self.calls: list[str] = []

    def __call__(self, path: str) -> ctypes.CDLL:
        self.calls.append(path)
        if self.error:
            raise self.error
        return self.library


@pytest.fixture
def host_library(tmp_path: Path) -> Path:
    name, _ = loader._target(*loader._host())
    return write(tmp_path / "lib" / name, HOST_HEADERS[sys.platform])


def find(
    tmp_path: Path,
    path: str | None = None,
    env: str | None = None,
    entry: str | None = "app",
) -> Path:
    entry_dir = tmp_path / entry if entry else None
    return loader._find(path, env, entry_dir, tmp_path / "cwd", NAME, "win64")


@pytest.mark.parametrize(
    ("system", "machine", "name"),
    [
        ("win32", "x86_64", "steam_api64.dll"),
        ("linux", "x86_64", "libsteam_api.so"),
        ("darwin", "x86_64", "libsteam_api.dylib"),
        ("darwin", "arm64", "libsteam_api.dylib"),
    ],
)
def test_library_name_per_platform(system: str, machine: str, name: str) -> None:
    assert loader._target(system, machine, True)[0] == name


@pytest.mark.parametrize(
    ("system", "machine"),
    [("win32", "arm64"), ("win32", "x86"), ("linux", "arm64"), ("freebsd14", "x86_64")],
)
def test_unsupported_platform(system: str, machine: str) -> None:
    with pytest.raises(UnsupportedPlatformError, match=f"does not support {machine} on {system}"):
        loader._target(system, machine, True)


def test_32_bit_python_is_unsupported() -> None:
    with pytest.raises(UnsupportedPlatformError, match="needs 64-bit Python"):
        loader._target("win32", "x86_64", False)


def test_explicit_path_wins(tmp_path: Path) -> None:
    explicit = write(tmp_path / "custom" / "renamed.dll")
    write(tmp_path / "env" / NAME)
    write(tmp_path / "app" / NAME)
    write(tmp_path / "cwd" / NAME)
    assert find(tmp_path, path=str(explicit), env=str(tmp_path / "env" / NAME)) == explicit


def test_environment_variable_wins_over_search(tmp_path: Path) -> None:
    configured = write(tmp_path / "env" / NAME)
    write(tmp_path / "app" / NAME)
    write(tmp_path / "cwd" / NAME)
    assert find(tmp_path, env=str(configured)) == configured


def test_entry_point_folder_wins_over_working_directory(tmp_path: Path) -> None:
    beside_app = write(tmp_path / "app" / NAME)
    write(tmp_path / "cwd" / NAME)
    assert find(tmp_path) == beside_app


def test_working_directory_is_searched_last(tmp_path: Path) -> None:
    in_cwd = write(tmp_path / "cwd" / NAME)
    assert find(tmp_path) == in_cwd
    assert find(tmp_path, entry=None) == in_cwd


def test_relative_paths_resolve_against_working_directory(tmp_path: Path) -> None:
    library = write(tmp_path / "cwd" / "bin" / NAME)
    assert find(tmp_path, path=f"bin/../bin/{NAME}") == library
    assert find(tmp_path, env=f"bin/{NAME}") == library
    assert find(tmp_path, path=f"bin/{NAME}").is_absolute()


def test_empty_environment_variable_is_ignored(tmp_path: Path) -> None:
    in_cwd = write(tmp_path / "cwd" / NAME)
    assert find(tmp_path, env="") == in_cwd


@pytest.mark.parametrize("missing", ["nope.dll", "nope/steam_api64.dll"])
def test_missing_explicit_path_does_not_fall_back(tmp_path: Path, missing: str) -> None:
    write(tmp_path / "cwd" / NAME)
    expected = tmp_path / "cwd" / missing
    with pytest.raises(LibraryNotFoundError, match="does not exist") as error:
        find(tmp_path, path=missing)
    assert error.value.path == expected
    assert error.value.searched == (expected,)
    assert str(expected) in str(error.value)


def test_explicit_directory_is_rejected(tmp_path: Path) -> None:
    write(tmp_path / "cwd" / NAME)
    with pytest.raises(LibraryNotFoundError, match=f"is a directory, not {NAME}"):
        find(tmp_path, path=str(tmp_path / "cwd"))


def test_missing_environment_path_does_not_fall_back(tmp_path: Path) -> None:
    write(tmp_path / "cwd" / NAME)
    configured = tmp_path / "gone" / NAME
    with pytest.raises(LibraryNotFoundError, match=f"{ENV_VAR} points to .* does not exist"):
        find(tmp_path, env=str(configured))


def test_not_found_lists_every_path_tried(tmp_path: Path) -> None:
    with pytest.raises(LibraryNotFoundError) as error:
        find(tmp_path)
    expected = (tmp_path / "app" / NAME, tmp_path / "cwd" / NAME)
    assert error.value.searched == expected
    message = str(error.value)
    assert all(str(path) in message for path in expected)
    assert ENV_VAR in message
    assert f"redistributable_bin/win64/{NAME}" in message


@pytest.mark.parametrize("spelling", ["cwd", "cwd/sub/.."])
def test_equivalent_folders_are_searched_once(tmp_path: Path, spelling: str) -> None:
    (tmp_path / "cwd" / "sub").mkdir(parents=True)
    with pytest.raises(LibraryNotFoundError) as error:
        find(tmp_path, entry=spelling)
    assert len(error.value.searched) == 1


def test_symlinked_folder_is_searched_once(tmp_path: Path) -> None:
    (tmp_path / "cwd").mkdir()
    symlink(tmp_path / "app", tmp_path / "cwd")
    with pytest.raises(LibraryNotFoundError) as error:
        find(tmp_path)
    assert len(error.value.searched) == 1


def test_without_working_directory_only_absolute_paths_work(tmp_path: Path) -> None:
    library = write(tmp_path / "lib" / NAME)
    assert loader._find(library, None, None, None, NAME, "win64") == library
    with pytest.raises(LibraryNotFoundError, match="does not exist"):
        loader._find(f"lib/{NAME}", None, None, None, NAME, "win64")
    with pytest.raises(LibraryNotFoundError) as error:
        loader._find(None, None, None, None, NAME, "win64")
    assert error.value.searched == ()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot remove the working directory")
def test_deleted_working_directory_does_not_block_absolute_paths(
    host_library: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    assert find_library(host_library) == host_library


def test_entry_folder_of_frozen_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = write(tmp_path / "game" / "game.exe")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    assert loader._entry_dir() == (tmp_path / "game").resolve()


def test_entry_folder_of_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = write(tmp_path / "game" / "main.py")
    monkeypatch.setitem(sys.modules, "__main__", SimpleNamespace(__file__=str(script)))
    assert loader._entry_dir() == (tmp_path / "game").resolve()


def test_entry_folder_follows_symlinked_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = write(tmp_path / "opt" / "game" / "main.py")
    launcher = tmp_path / "bin" / "game"
    launcher.parent.mkdir()
    symlink(launcher, script)
    monkeypatch.setitem(sys.modules, "__main__", SimpleNamespace(__file__=str(launcher)))
    assert loader._entry_dir() == (tmp_path / "opt" / "game").resolve()


def test_entry_folder_of_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = write(tmp_path / "game" / "game.pyz")
    main = SimpleNamespace(__file__=str(archive / "__main__.py"))
    monkeypatch.setitem(sys.modules, "__main__", main)
    assert loader._entry_dir() == (tmp_path / "game").resolve()


@pytest.mark.parametrize("main", [SimpleNamespace(), SimpleNamespace(__file__="main.py")])
def test_interpreter_is_not_an_entry_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, main: SimpleNamespace
) -> None:
    monkeypatch.setattr(sys, "executable", str(write(tmp_path / "python" / "python.exe")))
    monkeypatch.setitem(sys.modules, "__main__", main)
    assert loader._entry_dir() is None


def test_find_library_reads_environment_variable(
    host_library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_VAR, str(host_library))
    assert find_library() == host_library


def test_find_library_searches_working_directory(
    host_library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setitem(sys.modules, "__main__", SimpleNamespace())
    monkeypatch.chdir(host_library.parent)
    found = find_library()
    assert found.is_absolute()
    assert found.samefile(host_library)


@pytest.mark.parametrize(
    ("system", "data"),
    [("win32", pe(PE_X64)), ("linux", elf(ELF_X64)), ("darwin", b"anything")],
)
def test_matching_header_explains_nothing(tmp_path: Path, system: str, data: bytes) -> None:
    machine = "arm64" if system == "darwin" else "x86_64"
    assert loader._mismatch(write(tmp_path / "lib", data), system, machine) is None


@pytest.mark.parametrize(
    ("system", "data", "problem"),
    [
        ("win32", pe(PE_X86), "is built for 32-bit x86, but this Python is 64-bit x86-64"),
        ("win32", pe(PE_ARM), "is built for unknown CPU type 0x1c0"),
        ("linux", elf(ELF_X86, bits=32), "is built for 32-bit x86"),
        ("linux", elf(ELF_ARM64), "is built for 64-bit arm64, but this Python is 64-bit x86-64"),
        ("win32", elf(ELF_X64), "is a Linux shared library, not a Windows DLL"),
        ("linux", pe(PE_X64), "is a Windows DLL, not a Linux shared library"),
        ("win32", b"", "is not a Windows DLL"),
        ("win32", b"MZ", "is not a Windows DLL"),
        ("linux", b"\xcf\xfa\xed\xfe" + bytes(28), "is not a Linux shared library"),
    ],
)
def test_mismatched_header_is_explained(
    tmp_path: Path, system: str, data: bytes, problem: str
) -> None:
    mismatch = loader._mismatch(write(tmp_path / "lib", data), system, "x86_64")
    assert mismatch is not None
    assert mismatch.startswith(problem)


def test_unreadable_file_explains_nothing(tmp_path: Path) -> None:
    assert loader._mismatch(tmp_path, "win32", "x86_64") is None
    assert loader._mismatch(tmp_path / "missing", "linux", "x86_64") is None


def test_load_returns_the_opened_library(host_library: Path) -> None:
    opener = FakeOpener()
    assert load_library(host_library, opener=opener) is opener.library
    assert opener.calls == [str(host_library)]


def test_header_is_ignored_when_the_os_loads_the_file(host_library: Path) -> None:
    host_library.write_bytes(b"not a header any loader check knows")
    opener = FakeOpener()
    assert load_library(host_library, opener=opener) is opener.library


def test_load_requires_baseline_exports(host_library: Path) -> None:
    opener = FakeOpener(exports=REQUIRED_EXPORTS[2:])
    with pytest.raises(MissingExportsError, match=r"older than SDK 1\.59") as error:
        load_library(host_library, opener=opener)
    assert error.value.missing == REQUIRED_EXPORTS[:2]
    assert error.value.path == host_library
    assert isinstance(error.value, IncompatibleLibraryError)


def test_load_does_not_open_a_missing_file(tmp_path: Path) -> None:
    opener = FakeOpener()
    with pytest.raises(LibraryNotFoundError):
        load_library(tmp_path / "missing.dll", opener=opener)
    assert opener.calls == []


@pytest.mark.parametrize(
    ("error", "hint"),
    [
        (OSError("libfoo.so.1: cannot open shared object file"), "libfoo.so.1: cannot open"),
        (FileNotFoundError("Could not find module"), "a library it depends on was not found"),
        (OSError(errno.ENOEXEC, "%1 is not a valid Win32 application"), "damaged or incomplete"),
    ],
)
def test_os_loader_errors_keep_their_diagnostics(
    host_library: Path, error: OSError, hint: str
) -> None:
    with pytest.raises(LibraryLoadError, match="Could not load") as raised:
        load_library(host_library, opener=FakeOpener(error=error))
    assert hint in str(raised.value)
    assert str(error) in str(raised.value)
    assert raised.value.__cause__ is error
    assert raised.value.path == host_library


@pytest.mark.skipif(sys.platform not in WRONG_ARCH, reason="dyld names the architecture itself")
def test_failed_load_of_wrong_architecture_is_explained(host_library: Path) -> None:
    data, built_for = WRONG_ARCH[sys.platform]
    host_library.write_bytes(data)
    error = OSError("%1 is not a valid Win32 application")
    with pytest.raises(IncompatibleLibraryError, match=f"is built for {built_for}") as raised:
        load_library(host_library, opener=FakeOpener(error=error))
    assert raised.value.__cause__ is error


def test_real_os_loader_rejects_a_truncated_library(host_library: Path) -> None:
    with pytest.raises(LibraryLoadError, match="Could not load") as raised:
        load_library(host_library)
    assert isinstance(raised.value.__cause__, OSError)


@pytest.mark.skipif(sys.platform not in WRONG_ARCH, reason="dyld names the architecture itself")
def test_real_os_failure_on_wrong_architecture_is_explained(host_library: Path) -> None:
    data, built_for = WRONG_ARCH[sys.platform]
    host_library.write_bytes(data)
    with pytest.raises(IncompatibleLibraryError, match=f"is built for {built_for}") as raised:
        load_library(host_library)
    assert isinstance(raised.value.__cause__, OSError)


def test_loader_leaves_environment_and_working_directory_alone(
    host_library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    before = dict(os.environ), os.getcwd()
    load_library(host_library, opener=FakeOpener())
    with pytest.raises(MissingExportsError):
        load_library(host_library, opener=FakeOpener(exports=()))
    with pytest.raises(LibraryNotFoundError):
        load_library(host_library.parent / "missing", opener=FakeOpener())
    assert (dict(os.environ), os.getcwd()) == before


def test_error_hierarchy() -> None:
    assert issubclass(NativeLibraryError, SteamError)
    assert issubclass(UnsupportedPlatformError, SteamError)
    assert not issubclass(UnsupportedPlatformError, NativeLibraryError)
    for error in (LibraryNotFoundError, IncompatibleLibraryError, LibraryLoadError):
        assert issubclass(error, NativeLibraryError)
    assert issubclass(MissingExportsError, IncompatibleLibraryError)
