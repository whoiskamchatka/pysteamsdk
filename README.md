# pysteamsdk

Python bindings for Valve's Steamworks SDK, built on `ctypes`: no compiled extension and no
runtime dependencies.

> **Status: early development.** The package does not expose any Steamworks functionality yet
> and is not published on PyPI.

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

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

pysteamsdk's own source code is released under the [Apache License 2.0](LICENSE). The Steamworks
SDK is licensed separately by Valve. Bindings derived from the SDK will be published only after a
redistribution review.

Steam is a trademark of Valve Corporation. This project is not affiliated with or endorsed by
Valve.
