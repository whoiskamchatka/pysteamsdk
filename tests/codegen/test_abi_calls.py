"""Which records ctypes passes by value as the native compiler does.

fixtures/calls.h holds invented records shaped like those the flat API passes by value, and like
its C++ ID wrappers; the SDK test checks that the shapes still match the SDK's. Each call runs in
a child process, because a convention ctypes gets wrong can crash it. These tests build with the
host's C++ compiler, so they run where the probe tests do.
"""

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from codegen.abi import probe

FIXTURES = Path(__file__).parent / "fixtures"
CHILD = Path(__file__).parent / "call_child.py"
TARGET = probe.host_target()
SYSV = {"linux-x86_64", "macos-x86_64"}
EVERY = {"windows-x86_64", "linux-x86_64", "macos-x86_64", "macos-arm64"}
# Where a ctypes call is known to differ from the native one, and why. Windows and Linux are
# measured; the macOS entries follow the same ABI rules and are confirmed only where CI runs.
WRONG: dict[str, tuple[set[str], str]] = {
    "TakeLocation": (
        SYSV,
        "SysV passes a record of up to 16 bytes with a misaligned member in memory, but libffi "
        "uses registers",
    ),
    "ReturnId": (
        {"windows-x86_64"},
        "MSVC returns a class that is no aggregate through a hidden pointer, not in RAX",
    ),
    "ReturnGame": (EVERY, "a class with its own copy constructor is returned through a pointer"),
    "TakeGame": (EVERY, "a class with its own copy constructor is passed by reference"),
}
CASES = [
    "TakeLocation",
    "ReturnStick",
    "ReturnMotion",
    "ReturnButton",
    "ReturnAddress",
    "TakeAddress",
    "ReturnId",
    "TakeId",
    "ReturnGame",
    "TakeGame",
]


@pytest.fixture(scope="module")
def library(
    build_library: Callable[[Path, Path], Path], tmp_path_factory: pytest.TempPathFactory
) -> Path:
    return build_library(FIXTURES / "calls.cpp", tmp_path_factory.mktemp("calls with space"))


def call(library: Path, case: str) -> str:
    found = subprocess.run(
        [sys.executable, "-I", str(CHILD), str(library), case],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if found.returncode:
        return f"crashed with status {found.returncode}"
    return found.stdout.strip()


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            case,
            marks=pytest.mark.xfail(TARGET in WRONG[case][0], reason=WRONG[case][1], strict=True),
        )
        if case in WRONG
        else case
        for case in CASES
    ],
)
def test_ctypes_passes_the_record_as_the_compiler_does(library: Path, case: str) -> None:
    assert call(library, case) == "ok"
