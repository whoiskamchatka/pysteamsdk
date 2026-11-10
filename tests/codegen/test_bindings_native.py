"""Declarations generated for an invented SDK, bound and called through a compiled library.

fixtures/gadgets.h, gadgets_flat.h and gadgets.json form an SDK in the Steamworks shape, and
gadgets.cpp implements its flat functions. The SDK is extracted with libclang, its layouts are
checked by the host's C++ compiler, and declarations are generated for the host, as for the real
SDK. Each scenario then calls the library through them in a child process, since a call ctypes
gets wrong can crash it.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

from codegen import bindings, ir
from codegen.__main__ import REPOSITORY, main
from codegen.abi import probe
from codegen.extract import extract, layout_source, overrides

FIXTURES = Path(__file__).parent / "fixtures"
CHILD = Path(__file__).parent / "bindings_child.py"
TARGET = probe.host_target()
# The SDK's own reviews would not match these records; these stand in for them, with the host
# as the target a review names. GadgetSize_t is passed by value without one.
OVERRIDES = """
[[external_type]]
name = "CGadgetID"
declaration = "class"
header = "gadgets.h"
reason = "An invented ID wrapper, as CSteamID is one."

[[external_type]]
name = "GadgetTicket"
declaration = "incomplete"
header = "gadgets.h"
reason = "Declared and never defined."

[[external_type]]
name = "CallbackMsg_t"
declaration = "struct"
header = "gadgets.h"
reason = "The manual dispatch message the runtime reads."

[[callback]]
function = "GadgetAPI_IGadgets_SetNotice"
param = "pfnNotice"
reason = "Notify calls it on the thread that calls Notify."

[[retained_pointer]]
function = "GadgetAPI_GadgetValue_t_SetText"
param = "pchText"
holder = "record"
reason = "Stores the pointer in the record's m_val."

[[retained_pointer]]
function = "GadgetAPI_GadgetValue_t_SetData"
param = "pData"
holder = "none"
reason = "Invented, to see that such a function is not bound."

[[by_value]]
record = "GadgetSpot_t"
direct = ["{target}"]
shim = []
reason = "GetSpot and SpotSum call it by value in the native scenarios."

[[by_value]]
record = "GadgetPair_t"
direct = []
shim = ["{target}"]
reason = "Invented, to see that a shim review keeps the function out."
"""
SKIPPED = {
    "GadgetAPI_IGadgetListener_OnDone": "callback-object",
    "GadgetAPI_IGadgets_Listen": "callback-object",
    # No review says when SetHook's callback runs; one does for SetNotice's.
    "GadgetAPI_IGadgets_SetHook": "callback",
    "GadgetHook_t": "callback",
    "GadgetAPI_GadgetValue_t_SetData": "retained-pointer",
    "GadgetAPI_IGadgets_Area": "by-value-unverified",
    "GadgetAPI_IGadgets_PairSum": "by-value-shim",
    "GadgetAPI_IGadgets_ReadTicket": "opaque",
    "GadgetTicket": "opaque",
    "GadgetAPI_IGadgets_Inspect": "unknown-type",
    # Nothing the compiler checked holds a long, whose size differs by target.
    "GadgetAPI_IGadgets_EchoLong": "unsupported-type",
    "GadgetAPI_GameServerGadgetBox_v001": "game-server",
    "GadgetAPI_RunFrame": "no-signature",
}
SCENARIOS = [
    "integers",
    "mix",
    "enums",
    "strings",
    "buffers",
    "pointers",
    "records",
    "by_value",
    "interfaces",
    "skipped",
    "shutdown",
    "callbacks",
    "call_results",
    "dispatch_errors",
    "shutdown_from_a_handler",
    "callback_functions",
    "retained",
]


@dataclass(frozen=True)
class Gadgets:
    sdk: Path
    overrides: Path
    extraction: ir.Metadata
    report: probe.Report
    generated: bindings.Generated
    declarations: Path
    library: Path


def write_sdk(root: Path) -> Path:
    folder = root / "public" / "steam"
    folder.mkdir(parents=True)
    for source, name in (
        ("gadgets.json", "steam_api.json"),
        ("gadgets_flat.h", "steam_api_flat.h"),
        ("gadgets.h", "gadgets.h"),
    ):
        shutil.copyfile(FIXTURES / source, folder / name)
    (root / "Readme.txt").write_text("Gadget SDK\n\nv9.99 1st January 2000\n", encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def gadgets(
    compiler: probe.Compiler,
    build_library: Callable[[Path, Path], Path],
    tmp_path_factory: pytest.TempPathFactory,
) -> Gadgets:
    pytest.importorskip("codegen.abi.clang", reason="needs the codegen dependency group")
    if TARGET is None:
        pytest.skip("this machine is not one of the targets")
    folder = tmp_path_factory.mktemp("gadget sdk")
    sdk = write_sdk(folder / "sdk")
    reviewed = folder / "overrides.toml"
    reviewed.write_text(OVERRIDES.replace("{target}", TARGET), encoding="utf-8")
    loaded = overrides.load(reviewed.read_text(encoding="utf-8"), reviewed.name)
    metadata = extract(sdk, loaded, (TARGET,)).metadata
    assert metadata.abi is not None
    (layout,) = metadata.abi.targets
    include, folders = layout_source(sdk)
    report = probe.verify(layout, include, folders, compiler)
    generated = bindings.generate(metadata, bindings.Verification.from_report(report, layout))
    declarations = folder / "gadget_declarations.py"
    declarations.write_text(generated.text, encoding="utf-8")
    library = build_library(FIXTURES / "gadgets.cpp", folder)
    return Gadgets(sdk, reviewed, metadata, report, generated, declarations, library)


def load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"declarations_{id(path)}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_invented_sdk_extracts_and_checks_cleanly(gadgets: Gadgets) -> None:
    issues = gadgets.extraction.issues
    assert [issue for issue in issues if issue.severity == "error"] == []
    assert {issue.code for issue in issues if issue.severity == "warning"} == {
        "json-fields-differ",
        "unresolved-type",
        "unreviewed-by-value",
        "unsupported-type-syntax",
    }
    assert gadgets.report.failures == ()


def test_every_declaration_is_generated_or_skipped_with_its_reason(gadgets: Gadgets) -> None:
    generated = gadgets.generated
    assert {item.name: item.category for item in generated.skipped} == SKIPPED
    metadata = gadgets.extraction
    flat = {function.flat_name for function in metadata.functions()}
    accessors = {
        item.flat_name for interface in metadata.interfaces for item in interface.accessors
    }
    skipped = {item.name for item in generated.skipped}
    assert set(generated.generated["function"]) == flat - skipped
    assert set(generated.generated["accessor"]) == accessors - skipped
    assert generated.totals["function"] == len(flat) == 52
    assert len(generated.generated["function"]) == 43
    notes = [(item.name, item.message) for item in generated.notes]
    assert (
        "CGadgetID::GadgetID_t::GadgetIDParts_t",
        "its bit-fields m_unSerial, m_unBatch, m_unKind, m_eColor are not exposed: its 8 bytes "
        "are kept as storage of the same alignment",
    ) in notes
    assert ("GadgetValue_t.m_pfnHook", "a function pointer, kept as an address to pass on") in notes


def test_ctypes_lays_out_every_record_as_the_compiler_did(gadgets: Gadgets) -> None:
    # Importing checks each record against the sizes and offsets the compiler verified.
    module = load(gadgets.declarations)
    assert gadgets.extraction.abi is not None
    (layout,) = gadgets.extraction.abi.targets
    records = {item.name: item for item in layout.records if item.status == "complete"}
    for name, record in records.items():
        found = module.LAYOUTS[getattr(module, name.replace("::", "_"))]
        assert found[:2] == (record.size, record.align), name
    windows = TARGET == "windows-x86_64"
    assert module.GadgetReady_t.m_ulId.offset == (8 if windows else 4)
    assert module.EGadgetColor.k_EGadgetColorAll == (-1 if windows else 0xFFFFFFFF)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_calls_reach_the_library_as_declared(gadgets: Gadgets, scenario: str) -> None:
    # Warnings are errors, as in this process: one from ctypes would mean a record it may lay out
    # differently in a later Python.
    arguments = [str(gadgets.declarations), str(gadgets.library), scenario]
    done = subprocess.run(
        [sys.executable, "-I", "-W", "error", str(CHILD), *arguments],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert (done.returncode, done.stdout.strip()) == (0, "ok"), done.stdout + done.stderr


def test_the_cli_writes_the_same_declarations_in_any_process(
    gadgets: Gadgets,
    compiler: probe.Compiler,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    options = ["--overrides", str(gadgets.overrides), "--cxx", compiler.command[0]]
    out = tmp_path / "declarations.py"
    assert main(["bindings", str(gadgets.sdk), *options, "-o", str(out)]) == 0
    printed = capsys.readouterr().out
    assert printed == bindings.summary(gadgets.generated) + f"Wrote {out}\n"
    assert out.read_text(encoding="utf-8") == gadgets.generated.text
    again = tmp_path / "again.py"
    command = [sys.executable, "-m", "codegen", "bindings", str(gadgets.sdk), *options]
    subprocess.run(
        [*command, "-o", str(again)],
        capture_output=True,
        check=True,
        cwd=REPOSITORY,
        env={**os.environ, "PYTHONHASHSEED": "4242"},
        timeout=600,
    )
    assert again.read_bytes() == out.read_bytes()


def test_the_cli_writes_nothing_from_layouts_the_compiler_contradicts(
    gadgets: Gadgets,
    compiler: probe.Compiler,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert TARGET is not None
    options = ["--overrides", str(gadgets.overrides)]
    extracted = tmp_path / "metadata.json"
    command = ["extract", str(gadgets.sdk), *options, "--targets", TARGET, "-o", str(extracted)]
    assert main(command) == 0
    text = extracted.read_text(encoding="utf-8")
    assert '"offset": 4' in text
    bad = tmp_path / "bad.json"
    bad.write_text(text.replace('"offset": 4', '"offset": 5', 1), encoding="utf-8")
    capsys.readouterr()
    out = tmp_path / "declarations.py"
    cxx = ["--cxx", compiler.command[0]]
    assert main(["bindings", str(gadgets.sdk), "--metadata", str(bad), *cxx, "-o", str(out)]) == 1
    printed = capsys.readouterr().out
    assert "extracted 5, compiler 4" in printed
    assert f"No declarations written: not every layout for {TARGET} is correct." in printed
    assert not out.exists()
    inside = REPOSITORY / "declarations.py"
    assert main(["bindings", str(gadgets.sdk), *cxx, "-o", str(inside)]) == 2
    assert "Refusing to write" in capsys.readouterr().err
    assert not inside.exists()
