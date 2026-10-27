import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pysteamsdk._native import loader
from pysteamsdk._native.loader import ENV_VAR

SDK = os.environ.get("STEAMWORKS_SDK", "")
APP_ID = os.environ.get("STEAMWORKS_APP_ID", "")
CHILD = Path(__file__).with_name("lifecycle_child.py")

pytestmark = [
    pytest.mark.sdk,
    pytest.mark.skipif(not SDK, reason="set STEAMWORKS_SDK to a Steamworks SDK folder"),
]


def host_library() -> Path:
    name, folder = loader._target(*loader._host())
    return Path(SDK, "redistributable_bin", folder, name).resolve()


def run_child(cwd: Path, scenario: str, **env: str) -> dict[str, object]:
    # Without SteamAppId and friends the child looks like a process Steam did not launch.
    inherited = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("STEAM") and key != ENV_VAR
    }
    result = subprocess.run(
        [sys.executable, str(CHILD), scenario, str(host_library())],
        cwd=cwd,
        env=inherited | env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report: dict[str, object] = json.loads(result.stdout)
    return report


def test_init_without_a_steam_client_fails_cleanly(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    # On Linux and macOS steam_api finds Steam through $HOME, so an empty home hides it.
    env = {} if sys.platform == "win32" else {"HOME": str(home)}
    report = run_child(cwd, "without-steam", **env)
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


@pytest.mark.steam
@pytest.mark.skipif(
    not APP_ID,
    reason="initializes Steam and shows the app as running; set STEAMWORKS_APP_ID to opt in",
)
def test_init_and_shutdown_with_a_running_steam_client(tmp_path: Path) -> None:
    (tmp_path / "steam_appid.txt").write_text(APP_ID)
    report = run_child(tmp_path, "init-shutdown")
    print(json.dumps(report, indent=2))
    assert report["first"] == {"error": None}
    assert report["pipe"]
    assert report["user"]
    rejected = {"error": "LifecycleError", "result": None, "reason": ""}
    assert report["duplicate"] == rejected
    assert report["initialized_after_shutdown"] is False
    assert report["again"] == rejected
