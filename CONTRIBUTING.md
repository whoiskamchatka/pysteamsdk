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
python -m codegen check ../metadata.json            # validate metadata written earlier
```

The extracted metadata is derived from Valve's SDK, so it stays out of the repository until a
redistribution review allows otherwise; `extract` refuses to write inside it. The tests in
`tests/codegen` use small invented fixtures.

`extract` and `inventory` also record each struct, union, enum and typedef as Windows x64, Linux
x86_64 and macOS x86_64 and arm64 lay it out, by compiling the SDK headers with libclang from the
`codegen` dependency group, which `dev` includes. The only system headers they see are the few in
`codegen/abi/include`, built from the compiler's own definitions for each target, so every host
computes the same layouts. Pick targets with `--targets windows-x86_64,linux-x86_64`, or pass
`--no-layouts` to skip libclang. A record the layouts cannot describe is marked opaque or
unsupported instead of guessed, and a header that does not compile is an error.

`verify` checks one target's layouts against a native C++ compiler, which builds and runs a
probe program that includes the same headers with the compiler's own system headers:

```sh
python -m codegen verify sdk                          # this machine's target, extracted now
python -m codegen verify sdk --metadata ../metadata.json --only CSteamID,CGameID
```

It uses `--cxx`, else `$CXX`, else `cl` on Windows (run it from a Visual Studio developer prompt)
and `c++` elsewhere. Each record's size, alignment, members, bit-field positions and value bits,
and each enum's and typedef's type, are compared, and any difference fails with both values.
Value bits come from the compiler's padding-clearing builtin (MSVC and GCC have one, Clang has
not): a value bit no extracted member covers means a member is missing. An anonymous union's size
and alignment are derived from what the compiler measured. Whatever cannot be checked, and why,
is listed rather than passed. The probe has to run, so it verifies the target the compiler builds
for, normally the host's; pass `--target` and flags such as `--cxx-flag=-arch --cxx-flag=x86_64`
for another one the host can run. The tests in `tests/codegen` run the probe on invented headers
whenever a C++ compiler is found; CI sets `PYSTEAMSDK_REQUIRE_CXX=1` so that a missing compiler
fails.

macOS layouts of the real SDK are verified only on a Mac you are licensed to use the SDK on,
with the Xcode command line tools and the SDK in `sdk/`:

```sh
python -m codegen verify sdk                                   # arm64 on Apple silicon
python -m codegen verify sdk --target macos-x86_64 \
    --cxx-flag=-arch --cxx-flag=x86_64                         # x86_64; needs Rosetta 2
STEAMWORKS_SDK=sdk pytest -m sdk tests/integration/test_codegen_sdk.py
```

`bindings` checks one target's layouts the same way, then writes ctypes declarations for it:

```sh
python -m codegen bindings sdk -o ../steam_flat.py   # this machine's target; takes verify's options
```

The module holds the SDK's records, enums, typedefs and constants and the signature of each flat
function ctypes can call safely, and `pysteamsdk._native.bindings.bind()` attaches those to the
library an initialized session holds. Only layouts the compiler verified are used and nothing is
guessed: a function is left out if it takes a callback or an object the caller implements, needs
a type whose size is unknown, or passes a record by value without a `direct` review, and the
command lists every declaration it leaves out and why. A record with bit-fields keeps its bytes
as storage rather than fields. Importing the module checks that ctypes lays out each record as
the compiler did, and refuses a module made for another target. Like the extracted metadata, the
module is derived from Valve's SDK: `bindings` refuses to write inside the repository and
`tools/check_sdk_files.py` rejects a generated module, until a redistribution review allows
otherwise. `tests/codegen/test_bindings_native.py` generates declarations for an invented SDK,
`tests/codegen/fixtures/gadgets.*`, builds its library and calls every kind of signature through
them.

Correct layouts do not make a by-value call safe. `tests/codegen/test_abi_calls.py` calls
invented records shaped like those the flat API passes by value through ctypes and checks what
the callee receives; the SDK tests check that the shapes still match. The `by_value` entries in
`codegen/overrides.toml` record the result per target: `direct` where ctypes was shown to pass
the record correctly, `shim` where it was shown not to. Any other target is unverified, and a
binding generator must not pass the record by value there either.

Two more kinds of entry decide what pointers a binding may hand to Steam. A `callback` entry
allows a function pointer parameter, citing header evidence that Steam calls it back on the
thread of a call into Steam; its signature is read from the typedef's declaration. A
`retained_pointer` entry marks a pointer the function keeps: with `holder = "record"` the binding
keeps the argument alive with the record that holds the pointer, and with `"none"` the function
is not bound. `pysteamsdk._native.callbacks` delivers callbacks and call results through manual
dispatch, copying each before Steam frees it; `tests/codegen/test_bindings_native.py` exercises
both against the invented gadget library.

`codegen/overrides.toml` records reviewed corrections where the SDK's metadata is incomplete,
such as a missing interface version or types `steam_api.json` uses without describing. Each
entry needs SDK evidence and a reason, and is checked against the SDK on every run: when a new
SDK release no longer matches an entry, extraction reports an error until the entry is reviewed
again. Struct layouts never go in this file. Pass `--no-overrides` to see the SDK's metadata
as Valve ships it.

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
