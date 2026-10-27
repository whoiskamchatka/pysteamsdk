# pysteamsdk

Python bindings for Valve's Steamworks SDK, built on `ctypes`: no compiled extension and no
runtime dependencies.

> **Status: early development.** The package can initialize and shut down Steam but exposes no
> Steamworks interfaces yet, and is not published on PyPI.

## Goals

- Raw bindings generated from the SDK's metadata and headers, mirroring the flat C API.
- A small runtime that loads the native library, manages initialization and shutdown, and
  dispatches callbacks.
- Pythonic APIs for achievements, cloud saves, matchmaking, networking and the Workshop.
- Windows, Linux and macOS, verified against each platform's struct layout.

## Requirements

- 64-bit CPython 3.11 or newer
- Windows, Linux or macOS
- The Steamworks SDK, downloaded from Valve

## The Steamworks SDK is not included

The SDK is proprietary. This repository and its packages contain no SDK headers, metadata or
native libraries. Download the SDK from the
[Steamworks partner site](https://partner.steamgames.com/downloads/list) and use it under Valve's
terms; your game ships the SDK's redistributable library itself.

## Initializing Steam

```python
import pysteamsdk

pysteamsdk.init()
try:
    ...
finally:
    pysteamsdk.shutdown()
```

- `init()` loads the Steamworks library from the path you pass, then `PYSTEAMSDK_LIB`, then the
  folder of your script or frozen executable, then the working directory. Copy it from the SDK's
  `redistributable_bin` folder.
- Steam must be running and logged in. Steam supplies the app ID when it launches your game;
  outside Steam, put a `steam_appid.txt` containing the ID in the working directory.
  pysteamsdk never writes that file or sets environment variables for you.
- Failures raise `pysteamsdk.errors.InitError`, or its subclasses `NoSteamClientError` and
  `VersionMismatchError`; the message includes Steam's own explanation.
- Steam can be initialized once per process. `shutdown()` may be called repeatedly, but
  `init()` after `shutdown()` raises `LifecycleError`: Valve does not document initializing
  again.
- `init()` and `shutdown()` are safe to call from several threads, but pysteamsdk cannot see
  Steam calls made by other code; stop those before calling `shutdown()`.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

pysteamsdk's own source code is released under the [Apache License 2.0](LICENSE). The Steamworks
SDK is licensed separately by Valve. Bindings derived from the SDK will be published only after a
redistribution review.

Steam is a trademark of Valve Corporation. This project is not affiliated with or endorsed by
Valve.
