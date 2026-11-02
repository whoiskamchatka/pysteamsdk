"""Run one Steam lifecycle scenario in this throwaway process and write the outcome as JSON.

usage: lifecycle_child.py without-steam|init-shutdown LIBRARY REPORT

The report goes to a file, not stdout: steam_api prints its own diagnostics, including the
user's Steam ID, through a C runtime of its own.
"""

import ctypes
import json
import os
import sys
from pathlib import Path

from native_child import STEAMCLIENT, mapped, native_environment

import pysteamsdk
from pysteamsdk._native.loader import load_library
from pysteamsdk.errors import NotInitializedError, SteamError

# The variables steam_api's binaries name; only their values are reported, since others may
# hold paths or account details.
APP_ID_VARIABLES = {"SteamAppId", "SteamGameId", "SteamOverlayGameId"}


def attempt_init(library: str) -> dict[str, object]:
    try:
        pysteamsdk.init(library)
    except SteamError as exc:
        result, reason = getattr(exc, "result", None), getattr(exc, "reason", "")
        return {"error": type(exc).__name__, "result": result, "reason": reason}
    return {"error": None}


def fold(name: str) -> str:
    # Windows environment names are case-insensitive.
    return name.upper() if sys.platform == "win32" else name


def variables() -> dict[str, str]:
    result = {}
    for entry in native_environment():
        # Windows lists per-drive working directories as "=C:=C:\...", so skip a leading "=".
        name, _, value = entry[1:].partition("=")
        result[fold(entry[:1] + name)] = value
    return result


def environment_change(before: dict[str, str], after: dict[str, str]) -> dict[str, object]:
    shared = before.keys() & after.keys()
    app_ids = {fold(name) for name in APP_ID_VARIABLES} & after.keys()
    return {
        "added": sorted(after.keys() - before.keys()),
        "removed": sorted(before.keys() - after.keys()),
        "changed": sorted(name for name in shared if before[name] != after[name]),
        "app_id_values": {name: after[name] for name in sorted(app_ids)},
    }


def without_steam(library: str) -> dict[str, object]:
    # steam_api.h: S_API bool S_CALLTYPE SteamAPI_IsSteamRunning();
    is_steam_running = load_library(library).SteamAPI_IsSteamRunning
    is_steam_running.argtypes = []
    is_steam_running.restype = ctypes.c_bool
    if is_steam_running():
        return {"skipped": "a Steam client is running"}
    environment = native_environment()
    first, second = attempt_init(library), attempt_init(library)
    pysteamsdk.shutdown()
    return {
        "first": first,
        "second": second,
        "initialized": pysteamsdk.is_initialized(),
        "steamclient_loaded": mapped(STEAMCLIENT),
        "environment_changed": native_environment() != environment,
    }


def init_shutdown(library: str) -> dict[str, object]:
    from pysteamsdk._native.lifecycle import _lifecycle

    before = variables()
    first = attempt_init(library)
    report: dict[str, object] = {"first": first}
    if first["error"] is not None:
        return report
    try:
        session = _lifecycle.session()
        report |= {
            "initialized": pysteamsdk.is_initialized(),
            "pipe": session.pipe,
            "user": session.user,
            "steamclient_loaded": mapped(STEAMCLIENT),
            "environment_after_init": environment_change(before, variables()),
            "duplicate": attempt_init(library),
        }
    finally:
        pysteamsdk.shutdown()
    try:
        report["session_after_shutdown"] = f"usable, pipe {session.pipe}"
    except NotInitializedError:
        report["session_after_shutdown"] = "NotInitializedError"
    report |= {
        "session_closed": session.closed,
        "initialized_after_shutdown": pysteamsdk.is_initialized(),
        "steamclient_loaded_after_shutdown": mapped(STEAMCLIENT),
        "environment_after_shutdown": environment_change(before, variables()),
        "again": attempt_init(library),
        "cwd_files": sorted(os.listdir()),
    }
    return report


if __name__ == "__main__":
    scenario, path, out = sys.argv[1:4]
    run = {"without-steam": without_steam, "init-shutdown": init_shutdown}[scenario]
    Path(out).write_text(json.dumps(run(path)), encoding="utf-8")
