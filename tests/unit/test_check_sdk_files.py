import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from check_sdk_files import main, problem

# Built at runtime so this file does not trip the guard itself.
BANNER = b"//====== Copy" + b"right Valve Corporation, All rights reserved. ======\n"


@pytest.mark.parametrize(
    "name",
    [
        "sdk/Readme.txt",
        "sdk/public/steam/steam_api_flat.h",
        "public/steam/steam_api.json",
        "include/isteamuser.h",
        "vendor/steamclientpublic.h",
        "matchmakingtypes.h",
        "steam_appid.txt",
        "src/pysteamsdk/steam_api64.dll",
        "src/pysteamsdk/libsteam_api.so",
        "redistributable_bin/linux64/libsteam_api.so.1",
        "redistributable_bin/osx/libsteam_api.dylib",
        "redistributable_bin/win64/steam_api64.lib",
    ],
)
def test_rejects_sdk_files(name: str) -> None:
    assert problem(name, b"") is not None


@pytest.mark.parametrize(
    "name",
    [
        "README.md",
        "src/pysteamsdk/__init__.py",
        "src/pysteamsdk/sdk.py",
        "src/pysteamsdk/raw/structs.py",
        "ir/steamworks-1.65.json",
        "tests/abi/byval.c",
        "tests/abi/byval.h",
        "docs/sdk/setup.md",
    ],
)
def test_allows_project_files(name: str) -> None:
    assert problem(name, b"Python bindings for the Steamworks SDK.\n") is None


def test_rejects_renamed_sdk_header_by_banner() -> None:
    assert problem("tests/abi/types.h", BANNER + b"#pragma once\n") == "Valve copyright banner"


def test_scans_wheels_and_sdists(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with zipfile.ZipFile(tmp_path / "demo-1.0-py3-none-any.whl", "w") as wheel:
        wheel.writestr("demo/__init__.py", "")
        wheel.writestr("demo/steam_api64.dll", b"MZ")
    header = tmp_path / "steam_api.h"
    header.write_bytes(BANNER)
    with tarfile.open(tmp_path / "demo-1.0.tar.gz", "w:gz") as sdist:
        sdist.add(header, arcname="demo-1.0/sdk/public/steam/steam_api.h")
    header.unlink()

    assert main([str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "demo-1.0-py3-none-any.whl!demo/steam_api64.dll: native binary" in err
    assert "demo-1.0.tar.gz!demo-1.0/sdk/public/steam/steam_api.h: inside sdk/" in err


def test_passes_clean_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "module.py").write_text("x = 1\n")
    assert main([str(tmp_path)]) == 0
    assert "1 files checked" in capsys.readouterr().out


def test_fails_when_nothing_was_checked(tmp_path: Path) -> None:
    assert main([str(tmp_path)]) == 1


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_checks_files_tracked_by_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "sdk" / "public").mkdir(parents=True)
    (tmp_path / "sdk" / "public" / "steam_api.json").write_text("{}")
    (tmp_path / "module.py").write_text("x = 1\n")
    (tmp_path / "untracked.dll").write_bytes(b"MZ")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "sdk", "module.py"], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)

    assert main([]) == 1
    err = capsys.readouterr().err
    assert "sdk/public/steam_api.json: inside sdk/" in err
    assert "untracked.dll" not in err
