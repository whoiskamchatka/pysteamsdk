"""Run one Steam lifecycle scenario in this throwaway process and write the outcome as JSON.

usage: lifecycle_child.py without-steam|init-shutdown LIBRARY REPORT
       lifecycle_child.py bindings LIBRARY REPORT DECLARATIONS

The report goes to a file, not stdout: steam_api prints its own diagnostics, including the
user's Steam ID, through a C runtime of its own.
"""

import ctypes
import gc
import importlib.util
import json
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

from native_child import STEAMCLIENT, mapped, native_environment

import pysteamsdk
from pysteamsdk._native import bindings
from pysteamsdk._native import callbacks as dispatch
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


def load(path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location("steam_declarations", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def readable(name: bytes | None) -> bool:
    """Whether ``name`` is nonempty UTF-8, which is all that is reported of it."""
    if not name:
        return False
    try:
        name.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def refused(call: Callable[[], object]) -> str:
    try:
        call()
    except SteamError as exc:
        return type(exc).__name__
    return "allowed"


def with_bindings(library: str, declarations: str) -> dict[str, object]:
    """Use the running Steam client through generated declarations, then shut it down.

    Of the user's Steam ID and persona name, only their kind and how they compare are reported.
    """
    from pysteamsdk._native.lifecycle import _lifecycle

    flat = load(declarations)
    first = attempt_init(library)
    if first["error"] is not None:
        return {"first": first}
    session = _lifecycle.session()
    api = bindings.bind(flat, session)
    steam = dispatch.callbacks(flat, session)
    accessors = {
        name: getattr(api, name)()
        for name, (returns, params) in flat.FUNCTIONS.items()
        if isinstance(returns, type) and issubclass(returns, bindings.Interface) and not params
    }
    interfaces = {type(interface): interface for interface in accessors.values()}
    user, friends = interfaces[flat.ISteamUser], interfaces[flat.ISteamFriends]
    utils, stats = interfaces[flat.ISteamUtils], interfaces[flat.ISteamUserStats]
    relay = interfaces[flat.ISteamNetworkingUtils]
    sockets = interfaces[flat.ISteamNetworkingSockets]
    me = api.SteamAPI_ISteamUser_GetSteamID(user)
    persona = api.SteamAPI_ISteamFriends_GetPersonaName(friends).value
    report: dict[str, object] = {
        "first": first,
        "null_accessors": sorted(name for name, found in accessors.items() if found is None),
        "logged_on": api.SteamAPI_ISteamUser_BLoggedOn(user),
        # A CSteamID keeps its universe in the top 8 bits, its account type in the next 4 and
        # the account number in the low 32.
        "steam_id": {
            "universe": flat.EUniverse(me >> 56).name,
            "account_type": flat.EAccountType(me >> 52 & 0xF).name,
            "account_number": me & 0xFFFFFFFF != 0,
        },
        "persona_name": {
            "readable": readable(persona),
            "as_friends_see_it": (
                api.SteamAPI_ISteamFriends_GetFriendPersonaName(friends, me).value == persona
            ),
        },
        "app_id": api.SteamAPI_ISteamUtils_GetAppID(utils),
    }

    # Each record a handler gets, with its bytes then, to compare once Steam has shut down.
    copies: list[tuple[Any, bytes]] = []

    def keep(found: list[Any]) -> Callable[[Any], None]:
        def handler(value: Any) -> None:
            found.append(value)
            if value is not None:
                copies.append((value, bytes(value)))

        return handler

    # Starting relay network access makes Steam report the network's status, both as a callback
    # and through the hook, which ISteamNetworkingSockets::RunCallbacks calls.
    statuses: list[Any] = []
    steam.register(flat.SteamRelayNetworkStatus_t, keep(statuses))
    dropped: list[Any] = []
    steam.register(flat.SteamRelayNetworkStatus_t, dropped.append).cancel()
    main, phase = threading.get_ident(), ""
    hooked: list[Any] = []
    hooked_during: set[str] = set()

    def on_status(status: Any) -> None:
        hooked.append(status)
        copies.append((status, bytes(status)))
        hooked_during.add(phase if threading.get_ident() == main else "another thread")

    set_hook = api.SteamAPI_ISteamNetworkingUtils_SetGlobalCallback_SteamRelayNetworkStatusChanged
    hook_set = set_hook(relay, lambda status: on_status(status))
    # Every finished call, including the cancelled one, which Steam completes all the same.
    completed: set[int] = set()
    steam.register(flat.SteamAPICallCompleted_t, lambda done: completed.add(done.m_hAsyncCall))
    players: list[Any] = []
    own_stats: list[Any] = []
    cancelled: list[Any] = []
    players_call = api.SteamAPI_ISteamUserStats_GetNumberOfCurrentPlayers(stats)
    steam.expect(players_call, flat.NumberOfCurrentPlayers_t, keep(players))
    stats_call = api.SteamAPI_ISteamUserStats_RequestUserStats(stats, me)
    steam.expect(stats_call, flat.UserStatsReceived_t, keep(own_stats))
    dropped_call = api.SteamAPI_ISteamUserStats_GetNumberOfCurrentPlayers(stats)
    steam.expect(dropped_call, flat.NumberOfCurrentPlayers_t, keep(cancelled)).cancel()
    api.SteamAPI_ISteamNetworkingUtils_InitRelayNetworkAccess(relay)
    # Only pysteamsdk refers to the hook's function now.
    gc.collect()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if statuses and hooked and {players_call, stats_call, dropped_call} <= completed:
            break
        phase = "RunCallbacks"
        api.SteamAPI_ISteamNetworkingSockets_RunCallbacks(sockets)
        phase = "dispatch"
        steam.run()
        phase = ""
        time.sleep(0.05)
    hook_removed = set_hook(relay, None)
    availability = set(flat.ESteamNetworkingAvailability)
    report |= {
        "relay_status": {
            "delivered": bool(statuses),
            "known": all(status.m_eAvail in availability for status in statuses + hooked),
            "to_a_cancelled_handler": len(dropped),
        },
        "hook": {"set": hook_set, "called_during": sorted(hooked_during), "removed": hook_removed},
        "players": [
            value is not None and value.m_bSuccess == 1 and value.m_cPlayers >= 0
            for value in players
        ],
        "own_stats": [
            None
            if value is None
            else {
                "game": value.m_nGameID,
                "result": flat.EResult(value.m_eResult).name,
                "own_user": value.m_steamIDUser.m_steamid.m_unAll64Bits == me,
            }
            for value in own_stats
        ],
        "cancelled_call": {"completed": dropped_call in completed, "delivered": len(cancelled)},
    }

    # SetString keeps the pointer it is given, and the binding keeps the string alive with the
    # record that holds it.
    config = flat.SteamNetworkingConfigValue_t()
    text = f"pysteamsdk {os.getpid()}".encode()
    address = ctypes.cast(ctypes.c_char_p(text), ctypes.c_void_p).value
    assert address is not None
    stun = flat.ESteamNetworkingConfigValue.k_ESteamNetworkingConfig_P2P_STUN_ServerList
    api.SteamAPI_SteamNetworkingConfigValue_t_SetString(config, stun, text)
    del text
    gc.collect()
    report["retained"] = {
        "type": flat.ESteamNetworkingConfigDataType(config.m_eDataType).name,
        "same_pointer": config.m_val.m_ptr.value == address,
        "alive": ctypes.string_at(address) == f"pysteamsdk {os.getpid()}".encode(),
    }

    late: list[Any] = []
    pending = steam.expect(
        api.SteamAPI_ISteamUserStats_GetNumberOfCurrentPlayers(stats),
        flat.NumberOfCurrentPlayers_t,
        late.append,
    )
    pysteamsdk.shutdown()
    report["after_shutdown"] = {
        "dispatch": refused(steam.run),
        "call": refused(lambda: api.SteamAPI_ISteamUtils_GetAppID(utils)),
        "pending_call": {"delivered": len(late), "waiting": pending.active},
        "copies_intact": all(bytes(value) == data for value, data in copies),
    }
    return report


if __name__ == "__main__":
    scenario, path, out, *more = sys.argv[1:]
    scenarios: dict[str, Callable[..., dict[str, object]]] = {
        "without-steam": without_steam,
        "init-shutdown": init_shutdown,
        "bindings": with_bindings,
    }
    try:
        report = scenarios[scenario](path, *more)
    finally:
        # A scenario that fails part way still shuts Steam down.
        pysteamsdk.shutdown()
    Path(out).write_text(json.dumps(report), encoding="utf-8")
