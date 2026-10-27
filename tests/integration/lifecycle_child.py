"""Run one Steam lifecycle scenario in this throwaway process and print the outcome as JSON.

usage: lifecycle_child.py without-steam|init-shutdown LIBRARY
"""

import ctypes
import json
import sys

from native_child import STEAMCLIENT, mapped, native_environment

import pysteamsdk
from pysteamsdk._native.loader import load_library
from pysteamsdk.errors import SteamError


def attempt_init(library: str) -> dict[str, object]:
    try:
        pysteamsdk.init(library)
    except SteamError as exc:
        result, reason = getattr(exc, "result", None), getattr(exc, "reason", "")
        return {"error": type(exc).__name__, "result": result, "reason": reason}
    return {"error": None}


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

    environment = set(native_environment())
    first = attempt_init(library)
    session = _lifecycle.session() if first["error"] is None else None
    report: dict[str, object] = {
        "first": first,
        "pipe": session.pipe if session else None,
        "user": session.user if session else None,
        "environment_added": sorted(set(native_environment()) - environment),
        "duplicate": attempt_init(library),
    }
    pysteamsdk.shutdown()
    report["initialized_after_shutdown"] = pysteamsdk.is_initialized()
    report["again"] = attempt_init(library)
    return report


if __name__ == "__main__":
    scenario, path = sys.argv[1:3]
    run = {"without-steam": without_steam, "init-shutdown": init_shutdown}[scenario]
    print(json.dumps(run(path)))
