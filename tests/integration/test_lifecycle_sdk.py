import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from codegen.abi import probe
from pysteamsdk._native import loader
from pysteamsdk._native.loader import ENV_VAR

SDK = os.environ.get("STEAMWORKS_SDK", "")
# Valve's public test app, Spacewar. The live tests accept no other ID.
SPACEWAR = "480"
APP_ID = os.environ.get("STEAMWORKS_APP_ID", "")
# A separate opt-in, since STEAMWORKS_APP_ID may be set for other reasons.
LIVE_STEAM = os.environ.get("PYSTEAMSDK_RUN_LIVE_STEAM", "")
CHILD = Path(__file__).with_name("lifecycle_child.py")
ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.sdk,
    pytest.mark.skipif(not SDK, reason="set STEAMWORKS_SDK to a Steamworks SDK folder"),
]


def host_library() -> Path:
    name, folder = loader._target(*loader._host())
    return Path(SDK, "redistributable_bin", folder, name).resolve()


def redact(output: str) -> str:
    # steam_api prints the user's 64-bit Steam ID, a 17-digit number, when init succeeds.
    return re.sub(r"\b\d{17}\b", "<steam id>", output)


def run_child(cwd: Path, report: Path, scenario: str, *args: str, **env: str) -> dict[str, object]:
    # Without SteamAppId and friends the child looks like a process Steam did not launch.
    inherited = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("STEAM") and key != ENV_VAR
    }
    result = subprocess.run(
        [sys.executable, str(CHILD), scenario, str(host_library()), str(report), *args],
        cwd=cwd,
        env=inherited | env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, redact(result.stderr + result.stdout)
    outcome: dict[str, object] = json.loads(report.read_text(encoding="utf-8"))
    return outcome


def test_init_without_a_steam_client_fails_cleanly(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    # On Linux and macOS steam_api finds Steam through $HOME, so an empty home hides it.
    env = {} if sys.platform == "win32" else {"HOME": str(home)}
    report = run_child(cwd, tmp_path / "report.json", "without-steam", **env)
    if "skipped" in report:
        pytest.skip(f"{report['skipped']}; init would reach it")
    # The SDK promises no specific code here: Linux without Steam reports FailedGeneric.
    first = report["first"]
    assert isinstance(first, dict)
    assert (first["error"], first["result"]) in {("InitError", 1), ("NoSteamClientError", 2)}
    assert first["reason"]
    assert report["second"] == first
    assert report["initialized"] is False
    assert report["steamclient_loaded"] is False
    assert report["environment_changed"] is False
    assert list(cwd.iterdir()) == []
    assert list(home.iterdir()) == []


@pytest.fixture
def spacewar(tmp_path: Path) -> Iterator[Path]:
    """A working directory whose steam_appid.txt makes the running Steam client see Spacewar."""
    # Checked here, not by a skipif marker, so disabling skip markers cannot bypass it.
    if not (SDK and APP_ID == SPACEWAR and LIVE_STEAM == "1"):
        pytest.skip(
            "initializes Steam as Spacewar, which shows it as running; to opt in, set "
            f"STEAMWORKS_SDK, STEAMWORKS_APP_ID={SPACEWAR} and PYSTEAMSDK_RUN_LIVE_STEAM=1"
        )
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    app_id_file = cwd / "steam_appid.txt"
    # Exclusive create, outside the try, so a file this test did not write is never deleted.
    with app_id_file.open("x", encoding="ascii") as file:
        file.write(SPACEWAR)
    try:
        yield cwd
    finally:
        app_id_file.unlink()


@pytest.mark.steam
def test_init_and_shutdown_with_a_running_steam_client(spacewar: Path, tmp_path: Path) -> None:
    report = run_child(spacewar, tmp_path / "report.json", "init-shutdown")
    print(json.dumps(report, indent=2))
    assert report["first"] == {"error": None}
    assert report["initialized"] is True
    # The SDK defines no invalid handle, so nonzero handles are an expectation, not a promise.
    assert report["pipe"] != 0
    assert report["user"] != 0
    assert report["steamclient_loaded"] is True
    rejected = {"error": "LifecycleError", "result": None, "reason": ""}
    assert report["duplicate"] == rejected
    assert report["session_after_shutdown"] == "NotInitializedError"
    assert report["session_closed"] is True
    assert report["initialized_after_shutdown"] is False
    assert report["again"] == rejected
    assert report["cwd_files"] == ["steam_appid.txt"]


@pytest.mark.steam
def test_generated_bindings_with_a_running_steam_client(spacewar: Path, tmp_path: Path) -> None:
    # Declarations made from the local SDK as a developer makes them, which needs the compiler
    # that checks their layouts.
    try:
        probe.find_compiler()
    except probe.ProbeError as exc:
        pytest.skip(str(exc))
    declarations = tmp_path / "declarations.py"
    made = subprocess.run(
        [
            sys.executable,
            "-m",
            "codegen",
            "bindings",
            str(Path(SDK).resolve()),
            "-o",
            str(declarations),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert made.returncode == 0, made.stderr + made.stdout
    report = run_child(spacewar, tmp_path / "report.json", "bindings", str(declarations))
    print(json.dumps(report, indent=2))
    assert report == {
        "first": {"error": None},
        "null_accessors": [],
        "logged_on": True,
        "steam_id": {
            "universe": "k_EUniversePublic",
            "account_type": "k_EAccountTypeIndividual",
            "account_number": True,
        },
        "persona_name": {"readable": True, "as_friends_see_it": True},
        "app_id": int(SPACEWAR),
        # A callback through manual dispatch, and a function Steam calls back only from
        # RunCallbacks, on the thread that runs it.
        "relay_status": {"delivered": True, "known": True, "to_a_cancelled_handler": 0},
        "hook": {"set": True, "called_during": ["RunCallbacks"], "removed": True},
        # Two call results, and a cancelled call Steam completed without calling its handler.
        "players": [True],
        "own_stats": [{"game": int(SPACEWAR), "result": "k_EResultOK", "own_user": True}],
        "cancelled_call": {"completed": True, "delivered": 0},
        # The SDK keeps the pointer itself, not a copy, so the binding must keep the string.
        "retained": {
            "type": "k_ESteamNetworkingConfig_String",
            "same_pointer": True,
            "alive": True,
        },
        "after_shutdown": {
            "dispatch": "NotInitializedError",
            "call": "NotInitializedError",
            "pending_call": {"delivered": 0, "waiting": False},
            "copies_intact": True,
        },
    }
