import shutil
import subprocess
import sys
import tarfile
import zipfile
from email.message import Message
from email.parser import Parser
from importlib.metadata import metadata
from importlib.resources import files
from pathlib import Path

import pytest

import pysteamsdk
from check_sdk_files import main

ROOT = Path(__file__).resolve().parents[2]
VERSION = pysteamsdk.__version__
DIST_INFO = f"pysteamsdk-{VERSION}.dist-info"
DECOYS = {
    "sdk/public/steam/steam_api.h": b"".join([b"//====== Copy", b"right Valve Corporation\n"]),
    "sdk/redistributable_bin/win64/steam_api64.dll": b"MZ",
    "src/pysteamsdk/steam_api64.dll": b"MZ",
    "src/pysteamsdk/libsteam_api.so": b"\x7fELF",
    "src/pysteamsdk/libsteam_api.dylib": b"\xcf\xfa\xed\xfe",
    "src/pysteamsdk/steam_appid.txt": b"480",
    "steam_appid.txt": b"480",
}


@pytest.fixture(scope="module")
def dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    project = tmp_path_factory.mktemp("project")
    # .gitignore is left out on purpose: the packaging config alone must keep SDK files out.
    for name in ("pyproject.toml", "README.md", "LICENSE", "CHANGELOG.md"):
        shutil.copy2(ROOT / name, project / name)
    for name in ("src", "tests", "tools"):
        shutil.copytree(ROOT / name, project / name, ignore=shutil.ignore_patterns("__pycache__"))
    for name, data in DECOYS.items():
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_bytes(data)

    out = tmp_path_factory.mktemp("dist")
    subprocess.run(
        [sys.executable, "-m", "hatchling", "build", "-t", "wheel", "-t", "sdist", "-d", str(out)],
        cwd=project,
        capture_output=True,
        check=True,
        timeout=300,
    )
    return out


def wheel_path(dist: Path) -> Path:
    (path,) = dist.glob("*.whl")
    return path


def sdist_names(dist: Path) -> set[str]:
    (path,) = dist.glob("*.tar.gz")
    assert path.name == f"pysteamsdk-{VERSION}.tar.gz"
    with tarfile.open(path) as sdist:
        return {m.name.partition("/")[2] for m in sdist.getmembers() if m.isfile()}


def read_message(wheel: zipfile.ZipFile, name: str) -> Message:
    return Parser().parsestr(wheel.read(f"{DIST_INFO}/{name}").decode())


def test_wheel_is_pure_and_contains_only_the_package(dist: Path) -> None:
    path = wheel_path(dist)
    assert path.name == f"pysteamsdk-{VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(path) as wheel:
        names = set(wheel.namelist())
        tags = read_message(wheel, "WHEEL")
    required = {"pysteamsdk/__init__.py", "pysteamsdk/py.typed", f"{DIST_INFO}/licenses/LICENSE"}
    assert {name.split("/")[0] for name in names} == {"pysteamsdk", DIST_INFO}
    assert required <= names
    assert tags.get_all("Tag") == ["py3-none-any"]
    assert tags["Root-Is-Purelib"] == "true"


def test_wheel_metadata(dist: Path) -> None:
    with zipfile.ZipFile(wheel_path(dist)) as wheel:
        meta = read_message(wheel, "METADATA")
    assert meta["Name"] == "pysteamsdk"
    assert meta["Version"] == VERSION
    assert meta["Requires-Python"] == ">=3.11"
    assert meta["License-Expression"] == "Apache-2.0"
    assert meta["License-File"] == "LICENSE"
    assert meta.get_all("Requires-Dist") is None
    assert "Programming Language :: Python :: 3.11" in meta.get_all("Classifier", [])
    assert meta["Description-Content-Type"] == "text/markdown"


def test_sdist_contains_only_sources(dist: Path) -> None:
    names = sdist_names(dist)
    allowed = {"PKG-INFO", "pyproject.toml", "README.md", "LICENSE", "CHANGELOG.md"}
    assert {n.split("/")[0] for n in names} <= allowed | {"src", "tests", "tools"}
    assert {"src/pysteamsdk/__init__.py", "src/pysteamsdk/py.typed", "LICENSE"} <= names


def test_distributions_contain_no_sdk_files(dist: Path) -> None:
    with zipfile.ZipFile(wheel_path(dist)) as wheel:
        names = set(wheel.namelist()) | sdist_names(dist)
    leaked = {Path(n).name for n in names} & {Path(n).name for n in DECOYS}
    assert not leaked
    assert not any(name.startswith("sdk/") for name in names)
    assert main([str(dist)]) == 0


def test_installed_metadata_matches_package() -> None:
    meta = metadata("pysteamsdk")
    assert meta["Version"] == VERSION
    assert meta.get_all("Requires-Dist") is None
    assert files("pysteamsdk").joinpath("py.typed").is_file()
