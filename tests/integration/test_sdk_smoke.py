import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pysteamsdk._native import loader
from pysteamsdk._native.loader import ENV_VAR

SDK = os.environ.get("STEAMWORKS_SDK", "")
CHILD = Path(__file__).with_name("native_child.py")
THIRTY_TWO_BIT = {"win32": ("steam_api.dll",), "linux": ("linux32", "libsteam_api.so")}

pytestmark = [
    pytest.mark.sdk,
    pytest.mark.skipif(not SDK, reason="set STEAMWORKS_SDK to a Steamworks SDK folder"),
]


def sdk_file(*parts: str) -> Path:
    path = Path(SDK, "redistributable_bin", *parts).resolve()
    assert path.is_file(), f"{path} is missing from the SDK"
    return path


def host_library() -> Path:
    name, folder = loader._target(*loader._host())
    return sdk_file(folder, name)


def run_child(cwd: Path, *args: str, env: dict[str, str] | None = None) -> dict[str, object]:
    # Without SteamAppId and friends the child looks like a process Steam did not launch.
    inherited = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("STEAM") and key != ENV_VAR
    }
    result = subprocess.run(
        [sys.executable, str(CHILD), *args],
        cwd=cwd,
        env=inherited | (env or {}),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report: dict[str, object] = json.loads(result.stdout)
    return report


def test_sdk_library_loads_without_initializing_steam(tmp_path: Path) -> None:
    library = host_library()
    report = run_child(tmp_path, str(library))
    assert report["path"] == str(library)
    assert report["missing_exports"] == []
    assert isinstance(report["steam_running"], bool)
    assert report["mapped_after_release"] is True
    assert report["steamclient_loaded"] is False
    assert report["environment_changed"] is False
    assert list(tmp_path.iterdir()) == []


def test_sdk_library_loads_from_environment_variable(tmp_path: Path) -> None:
    library = host_library()
    report = run_child(tmp_path, env={ENV_VAR: str(library)})
    assert report["path"] == str(library)
    assert report["missing_exports"] == []


@pytest.mark.skipif(sys.platform not in THIRTY_TWO_BIT, reason="no 32-bit SDK library here")
def test_32_bit_sdk_library_is_explained(tmp_path: Path) -> None:
    report = run_child(tmp_path, str(sdk_file(*THIRTY_TWO_BIT[sys.platform])))
    assert report["error"] == "IncompatibleLibraryError"
    assert "is built for 32-bit x86, but this Python is 64-bit x86-64" in str(report["message"])
    assert report["cause"] == "OSError"


@pytest.mark.parametrize(
    ("parts", "system"),
    [
        (("win64", "steam_api64.dll"), "win32"),
        (("linux64", "libsteam_api.so"), "linux"),
        (("osx", "libsteam_api.dylib"), "darwin"),
    ],
)
def test_sdk_binaries_match_their_platform(parts: tuple[str, ...], system: str) -> None:
    assert loader._mismatch(sdk_file(*parts), system, "x86_64") is None


@pytest.mark.parametrize(
    ("parts", "system", "problem"),
    [
        (("steam_api.dll",), "win32", "is built for 32-bit x86"),
        (("linux32", "libsteam_api.so"), "linux", "is built for 32-bit x86"),
        (("linuxarm64", "libsteam_api.so"), "linux", "is built for 64-bit arm64"),
        (("androidarm64", "libsteam_api.so"), "linux", "is built for 64-bit arm64"),
        (("win64", "steam_api64.dll"), "linux", "is a Windows DLL, not"),
        (("osx", "libsteam_api.dylib"), "win32", "is not a Windows DLL"),
        (("osx", "libsteam_api.dylib"), "linux", "is not a Linux shared library"),
    ],
)
def test_sdk_binaries_for_other_platforms_are_explained(
    parts: tuple[str, ...], system: str, problem: str
) -> None:
    assert str(loader._mismatch(sdk_file(*parts), system, "x86_64")).startswith(problem)
