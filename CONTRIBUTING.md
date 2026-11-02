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

## Steamworks metadata

`codegen/` is maintainer tooling that reads a local SDK's `steam_api.json` and headers into a
normalized metadata format. It never ships in the wheel and needs no Steam client or SDK binary.

```sh
python -m codegen inventory sdk                     # counts, cross-checks and open issues
python -m codegen extract sdk -o ../metadata.json   # the normalized metadata as JSON
```

The extracted metadata is derived from Valve's SDK, so it stays out of the repository until a
redistribution review allows otherwise; `extract` refuses to write inside it. The tests in
`tests/codegen` use small invented fixtures.

## Steamworks SDK

The SDK is proprietary and must never enter the repository or its packages.

- Keep your local copy in `sdk/` at the repository root. Git ignores it and the build never
  includes it.
- Do not commit or paste SDK headers, `steam_api.json`, native libraries or `steam_appid.txt`,
  including into issues and pull requests.
- `tools/check_sdk_files.py` checks tracked files and built distributions in CI. If it flags a
  file you wrote yourself, rename the file or adjust the rule in the same pull request and say
  why.

Tests marked `sdk` load the native library from your SDK in a child process, and CI does not
run them. They call `SteamAPI_InitFlat` only when no Steam client is running and no app ID is
set, so it fails without touching Steam. Point `STEAMWORKS_SDK` at the SDK folder to run them:

```sh
STEAMWORKS_SDK=sdk pytest -m sdk          # PowerShell: $env:STEAMWORKS_SDK = "sdk"; pytest -m sdk
```

The one test marked `steam` initializes your running Steam client as Spacewar (app ID 480),
Valve's test app, so your Steam activity can show Spacewar for a few seconds. It is skipped
unless all three of these are set, and CI never sets them:

- `STEAMWORKS_SDK` to the SDK folder
- `STEAMWORKS_APP_ID` to exactly `480`
- `PYSTEAMSDK_RUN_LIVE_STEAM` to exactly `1`

It writes `steam_appid.txt` only into a new temporary folder and deletes it afterwards. Close
any running game first, and set the variables only for the command that runs the test:

```powershell
$env:STEAMWORKS_SDK = "sdk"; $env:STEAMWORKS_APP_ID = "480"; $env:PYSTEAMSDK_RUN_LIVE_STEAM = "1"
try { pytest -m steam -s }
finally { Remove-Item Env:STEAMWORKS_SDK, Env:STEAMWORKS_APP_ID, Env:PYSTEAMSDK_RUN_LIVE_STEAM }
```

## Pull requests

- One topic per pull request, with tests for any behavior change.
- Add a line under "Unreleased" in [CHANGELOG.md](CHANGELOG.md) for user-visible changes.
- Comments explain why something is done, not what the code does.

## Commits and branches

Commit subjects follow [Conventional Commits](https://www.conventionalcommits.org/):
`type(scope): summary`, imperative and lowercase, no trailing period, at most 72 characters.
The body, if any, explains why.

| Type | For |
| --- | --- |
| `feat` | new behavior |
| `fix` | wrong behavior made right, including struct layouts |
| `refactor` | restructuring with identical behavior and generated output |
| `test` | test-only changes |
| `docs` | documentation-only changes |
| `build` | `pyproject.toml`, dependencies, tool versions and their settings |
| `ci` | workflows and CI scripts such as `tools/check_sdk_files.py` |
| `chore` | other housekeeping |

A fix to a test, a workflow or the docs uses that type, not `fix`.

The scope is optional and names the part of the architecture a change is for: `native`
(`pysteamsdk._native`), `codegen` (the generator), `raw` (the generated bindings as users see
them), `abi` (struct layout and calling conventions), `api` (the high-level modules) or `deps`
(version bumps, as `build(deps)` or `ci(deps)`). Leave it out when no single scope fits.

Mark a breaking change to a public module with `!` and describe it in a `BREAKING CHANGE:`
footer, e.g. `feat(raw)!: update to Steamworks SDK 1.66`.

Name branches `type/topic` in kebab-case, e.g. `feat/native-lifecycle` or
`fix/abi-callback-packing`. Pull request titles follow the commit format.
