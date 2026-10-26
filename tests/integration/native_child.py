"""Load the Steamworks library in this throwaway process and print what happened, as JSON."""

import ctypes
import gc
import json
import sys

from pysteamsdk._native.loader import REQUIRED_EXPORTS, find_library, load_library
from pysteamsdk.errors import SteamError

STEAMCLIENT = "steamclient64.dll" if sys.platform == "win32" else "steamclient"


def native_environment() -> bytes:
    if sys.platform == "win32":
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetEnvironmentStringsW.restype = ctypes.c_void_p
        kernel32.FreeEnvironmentStringsW.argtypes = [ctypes.c_void_p]
        block = kernel32.GetEnvironmentStringsW()
        units = ctypes.cast(block, ctypes.POINTER(ctypes.c_uint16))
        length = 0
        while units[length] or units[length + 1]:
            length += 1
        snapshot = ctypes.string_at(block, 2 * length)
        kernel32.FreeEnvironmentStringsW(block)
        return snapshot
    else:
        libc = ctypes.CDLL(None)
        if sys.platform == "darwin":
            libc._NSGetEnviron.restype = ctypes.POINTER(ctypes.POINTER(ctypes.c_char_p))
            environ = libc._NSGetEnviron().contents
        else:
            environ = ctypes.POINTER(ctypes.c_char_p).in_dll(libc, "environ")
        entries: list[bytes] = []
        while (entry := environ[len(entries)]) is not None:
            entries.append(entry)
        return b"\n".join(entries)


def mapped(name: str) -> bool:
    if sys.platform == "win32":
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        return bool(kernel32.GetModuleHandleW(name))
    elif sys.platform == "darwin":
        libc = ctypes.CDLL(None)
        libc._dyld_get_image_name.argtypes = [ctypes.c_uint32]
        libc._dyld_get_image_name.restype = ctypes.c_char_p
        images = (libc._dyld_get_image_name(i) or b"" for i in range(libc._dyld_image_count()))
        return any(name.encode() in image for image in images)
    else:
        with open("/proc/self/maps") as maps:
            return name in maps.read()


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else None
    environment = native_environment()
    found = find_library(path)
    report: dict[str, object]
    try:
        library = load_library(path)
    except SteamError as exc:
        cause = type(exc.__cause__).__name__
        report = {"error": type(exc).__name__, "message": str(exc), "cause": cause}
        print(json.dumps(report))
        return
    missing = [name for name in REQUIRED_EXPORTS if not hasattr(library, name)]
    # steam_api.h: S_API bool S_CALLTYPE SteamAPI_IsSteamRunning(); C++ bool is one byte.
    is_steam_running = library.SteamAPI_IsSteamRunning
    is_steam_running.argtypes = []
    is_steam_running.restype = ctypes.c_bool
    del library
    gc.collect()
    steam_running = is_steam_running()
    del is_steam_running
    gc.collect()
    report = {
        "path": str(found),
        "missing_exports": missing,
        "steam_running": steam_running,
        "mapped_after_release": mapped(found.name),
        "steamclient_loaded": mapped(STEAMCLIENT),
        "environment_changed": native_environment() != environment,
    }
    print(json.dumps(report))


if __name__ == "__main__":
    main()
