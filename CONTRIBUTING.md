# Contributing

## Setup

You need 64-bit CPython 3.11 or newer and pip 25.1 or newer (for dependency groups).

```sh
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -e . --group dev
```

`uv sync` works as well; it reads the same `dev` group.

## Checks

CI runs these on every pull request; run them before pushing:

```sh
ruff check .
ruff format --check .
mypy
pytest
python tools/check_sdk_files.py
```

To build and check the distributions:

```sh
python -m pip install --group dist
python -m build
twine check --strict dist/*
python tools/check_sdk_files.py dist
```

## Steamworks SDK

The SDK is proprietary and must never enter the repository or its packages.

- Keep your local copy in `sdk/` at the repository root. Git ignores it and the build never
  includes it.
- Do not commit or paste SDK headers, `steam_api.json`, native libraries or `steam_appid.txt`,
  including into issues and pull requests.
- `tools/check_sdk_files.py` checks tracked files and built distributions in CI. If it flags a
  file you wrote yourself, rename the file or adjust the rule in the same pull request and say
  why.

Tests marked `sdk` load the native library from your SDK in a child process. They never
initialize Steam, write `steam_appid.txt` or set environment variables, and CI does not run
them. Point `STEAMWORKS_SDK` at the SDK folder to run them:

```sh
STEAMWORKS_SDK=sdk pytest -m sdk          # PowerShell: $env:STEAMWORKS_SDK = "sdk"; pytest -m sdk
```

## Pull requests

- One topic per pull request, with tests for any behavior change.
- Add a line under "Unreleased" in [CHANGELOG.md](CHANGELOG.md) for user-visible changes.
- Commit subjects are short and imperative, e.g. "Add native library loader".
- Comments explain why something is done, not what the code does.

