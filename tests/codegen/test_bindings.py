"""The declarations codegen.bindings writes, from invented metadata and invented checks.

These need neither libclang nor a compiler; test_bindings_native.py builds and calls the real
thing. A declarations module checks itself as it is imported, so the tests that import one run
on whichever target this machine is.
"""

import ctypes
import dataclasses
import importlib.util
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from codegen import bindings, ir
from codegen.abi import TARGETS
from codegen.cdecl import parse_type
from pysteamsdk._native import bindings as runtime

HOST = runtime.host_target()
OTHER = next(target for target in TARGETS if target != HOST)
host_only = pytest.mark.skipif(HOST is None, reason="this machine is not one of the targets")


def on(metadata: ir.Metadata, target: str) -> ir.Metadata:
    """widgets.json, whose layouts are alike on every target, laid out for ``target``."""
    assert metadata.abi is not None
    layout = dataclasses.replace(
        metadata.abi.targets[0], target=target, triple=TARGETS[target].triple
    )
    return dataclasses.replace(metadata, abi=dataclasses.replace(metadata.abi, targets=(layout,)))


def layout_of(metadata: ir.Metadata) -> ir.TargetLayout:
    assert metadata.abi is not None
    return metadata.abi.targets[0]


def with_layouts(metadata: ir.Metadata, **changes: object) -> ir.Metadata:
    layout = layout_of(metadata)
    extended: dict[str, Any] = {
        key: (*getattr(layout, key), *value)  # type: ignore[misc]
        for key, value in changes.items()
    }
    assert metadata.abi is not None
    moved = dataclasses.replace(layout, **extended)
    return dataclasses.replace(metadata, abi=dataclasses.replace(metadata.abi, targets=(moved,)))


def with_sizes(metadata: ir.Metadata) -> ir.Metadata:
    """``int`` and ``bool`` get verified sizes through typedefs, as SDK records give them."""
    found = dataclasses.replace(
        metadata,
        typedefs=(
            *metadata.typedefs,
            ir.Typedef("int32", parse_type("int"), "test"),
            ir.Typedef("WidgetFlag_t", parse_type("bool"), "test"),
        ),
    )
    layouts = (
        ir.TypedefLayout("int32", "int", 4, 4),
        ir.TypedefLayout("WidgetFlag_t", "bool", 1, 1),
    )
    return with_layouts(found, typedefs=layouts)


def verification(metadata: ir.Metadata, **statuses: str) -> bindings.Verification:
    """Every layout verified, except those ``statuses`` names, by name with :: as __."""
    layout = layout_of(metadata)
    groups = {
        "record": {item.name: "verified" for item in layout.records},
        "enum": {item.name: "verified" for item in layout.enums},
        "typedef": {item.name: "verified" for item in layout.typedefs},
    }
    for name, status in statuses.items():
        name = name.replace("__", "::")
        (group,) = [found for found in groups.values() if name in found]
        group[name] = status
    return bindings.Verification(layout.target, "Test C++ 1.0", groups)


def generate(metadata: ir.Metadata, **statuses: str) -> bindings.Generated:
    return bindings.generate(metadata, verification(metadata, **statuses))


def skips(generated: bindings.Generated) -> dict[str, tuple[str, str]]:
    return {item.name: (item.category, item.reason) for item in generated.skipped}


def load(text: str, folder: Path, name: str = "declarations") -> ModuleType:
    path = folder / f"{name}.py"
    path.write_text(text, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def method(
    name: str, returns: str, *params: tuple[str, str], owner: str = "IWidgets"
) -> ir.Function:
    return ir.Function(
        name,
        f"WidgetAPI_{owner}_{name}",
        parse_type(returns),
        tuple(ir.Param(param, parse_type(spelling)) for param, spelling in params),
        "test",
    )


def with_methods(metadata: ir.Metadata, *methods: ir.Function) -> ir.Metadata:
    (widgets,) = metadata.interfaces
    widgets = dataclasses.replace(widgets, methods=(*widgets.methods, *methods))
    return dataclasses.replace(metadata, interfaces=(widgets,))


@pytest.fixture
def host(widgets: ir.Metadata) -> ir.Metadata:
    if HOST is None:
        pytest.skip("this machine is not one of the targets")
    return with_sizes(on(widgets, HOST))


def test_widgets_become_declarations_for_their_target(host: ir.Metadata, tmp_path: Path) -> None:
    generated = generate(host)
    assert generated.generated["function"] == (
        "WidgetAPI_IWidgets_GetWidgetCount",
        "WidgetAPI_IWidgets_GetWidgetName",
        "WidgetAPI_IWidgets_GetWidgetPos",
        "WidgetAPI_IWidgets_RequestWidget",
        "WidgetAPI_IWidgets_SetWidgetName",
    )
    assert generated.generated["accessor"] == ("WidgetAPI_Widgets_v007",)
    assert skips(generated) == {
        "WidgetAPI_Init": (
            "no-signature",
            "declared at widget_api.h:3, but steam_api.json gives no signature",
        )
    }
    # Typedefs keep their names, and each function its C signature, for whoever reads the file.
    lines = generated.text.splitlines()
    assert "uint32 = ctypes.c_uint32" in lines
    assert (
        "    # bool WidgetAPI_IWidgets_SetWidgetName( IWidgets* self, int iWidget, "
        "const char * pchName )"
    ) in lines
    module = load(generated.text, tmp_path)
    assert module.TARGET == HOST
    assert module.PROTOCOL == runtime.PROTOCOL
    assert set(module.FUNCTIONS) == {*generated.generated["function"], "WidgetAPI_Widgets_v007"}
    signatures = {
        name: (restype, [repr(t) if isinstance(t, runtime.Pointer) else t for t in argtypes])
        for name, (restype, argtypes) in module.FUNCTIONS.items()
    }
    widgets = module.IWidgets
    assert signatures["WidgetAPI_IWidgets_GetWidgetCount"] == (ctypes.c_int32, [widgets])
    assert signatures["WidgetAPI_IWidgets_GetWidgetName"] == (
        ctypes.c_bool,
        [widgets, ctypes.c_int32, "Out(c_char)", ctypes.c_int32],
    )
    assert signatures["WidgetAPI_IWidgets_SetWidgetName"] == (
        ctypes.c_bool,
        [widgets, ctypes.c_int32, "In(c_char)"],
    )
    assert signatures["WidgetAPI_IWidgets_GetWidgetPos"] == (
        ctypes.c_bool,
        [widgets, ctypes.c_int32, "Out(WidgetPos_t)"],
    )
    assert signatures["WidgetAPI_IWidgets_RequestWidget"] == (
        ctypes.c_uint64,
        [widgets, ctypes.c_uint32],
    )
    assert skips(generated)["WidgetAPI_Init"][1] == module.SKIPPED["WidgetAPI_Init"]
    assert set(module.SKIPPED) == {"WidgetAPI_Init"}
    assert ctypes.sizeof(module.WidgetPos_t) == 12
    assert module.EWidgetResult.k_EWidgetResultFail == -1


def test_metadata_with_errors_or_contradicted_layouts_is_refused(widgets: ir.Metadata) -> None:
    broken = dataclasses.replace(
        widgets, issues=(ir.Issue("error", "flat-signature-mismatch", "invented", "test"),)
    )
    with pytest.raises(bindings.GenerationError, match="flat-signature-mismatch: invented"):
        bindings.generate(broken, verification(on(widgets, "linux-x86_64")))
    with pytest.raises(bindings.GenerationError, match="no layouts for macos-arm64"):
        bindings.generate(widgets, dataclasses.replace(verification(widgets), target="macos-arm64"))
    with pytest.raises(bindings.GenerationError, match="plan9-mips is not a target"):
        bindings.generate(widgets, dataclasses.replace(verification(widgets), target="plan9-mips"))
    with pytest.raises(bindings.GenerationError, match="contradicts the layouts of record Widget"):
        generate(widgets, WidgetPos_t="mismatched")


def test_layouts_the_compiler_did_not_verify_are_never_used(widgets: ir.Metadata) -> None:
    metadata = with_sizes(widgets)
    found = skips(generate(metadata, WidgetPos_t="not checked", CallID_t="opaque"))
    assert found["WidgetPos_t"] == (
        "unverified-layout",
        "the compiler did not verify WidgetPos_t on windows-x86_64: it is not checked",
    )
    assert found["WidgetAPI_IWidgets_GetWidgetPos"][0] == "unverified-layout"
    assert found["WidgetAPI_IWidgets_GetWidgetPos"][1].startswith("parameter pPos: ")
    assert found["WidgetAPI_IWidgets_RequestWidget"] == (
        "unverified-layout",
        "the result: the compiler did not verify typedef CallID_t on windows-x86_64: it is opaque",
    )
    partly = generate(metadata, WidgetPos_t="partly verified")
    assert "WidgetAPI_IWidgets_GetWidgetPos" in partly.generated["function"]
    assert [note.message for note in partly.notes] == [
        "partly verified: not every check was possible"
    ]


def test_sizes_come_only_from_verified_layouts(widgets: ir.Metadata) -> None:
    # Nothing in widgets.json lays out an int, so no function may take one.
    without = skips(generate(widgets))
    assert without["WidgetAPI_IWidgets_GetWidgetCount"] == (
        "unsupported-type",
        "the result: no verified layout on windows-x86_64 gives the size of int",
    )
    assert without["WidgetAPI_IWidgets_SetWidgetName"][0] == "unsupported-type"
    unverified = skips(generate(with_sizes(widgets), int32="not checked"))
    assert "WidgetAPI_IWidgets_GetWidgetCount" in unverified
    assert "WidgetAPI_IWidgets_GetWidgetCount" not in skips(generate(with_sizes(widgets)))


@pytest.mark.parametrize(
    ("direct", "shim", "category"),
    [
        ((), (), "by-value-unverified"),
        (("linux-x86_64",), (), "by-value-unverified"),
        ((), ("windows-x86_64",), "by-value-shim"),
        (("windows-x86_64",), (), None),
    ],
)
def test_records_pass_by_value_only_where_a_review_makes_them_direct(
    widgets: ir.Metadata, direct: tuple[str, ...], shim: tuple[str, ...], category: str | None
) -> None:
    metadata = with_methods(widgets, method("GetFirstPos", "WidgetPos_t"))
    metadata = dataclasses.replace(
        metadata, by_value=(ir.ByValue("WidgetPos_t", direct, shim, "test"),)
    )
    generated = generate(metadata)
    if category is None:
        assert '    "WidgetAPI_IWidgets_GetFirstPos": (WidgetPos_t, (IWidgets,)),' in (
            generated.text.splitlines()
        )
    else:
        assert skips(generated)["WidgetAPI_IWidgets_GetFirstPos"][0] == category


def test_signature_shapes_ctypes_cannot_express_safely_are_skipped(host: ir.Metadata) -> None:
    metadata = with_methods(
        host,
        method("SetRawHook", "void", ("pfn", "void (*)(uint32)")),
        method("Fill", "void", ("rgValues", "uint32 [4]")),
        method("Ref", "WidgetPos_t &"),
    )
    assert {name: reason for name, (_, reason) in skips(generate(metadata)).items()} == {
        "WidgetAPI_IWidgets_SetRawHook": "parameter pfn: void (*)(uint32) is a C function pointer",
        "WidgetAPI_IWidgets_Fill": "parameter rgValues: uint32 [4] is an array",
        "WidgetAPI_IWidgets_Ref": "the result: returns a reference, WidgetPos_t &",
        "WidgetAPI_Init": "declared at widget_api.h:3, but steam_api.json gives no signature",
    }


def test_typedefs_of_pointers_stay_pointers(host: ir.Metadata, tmp_path: Path) -> None:
    pointers = {
        "HWidgetQuery": ("void *", "void*"),
        "PWidgetPos": ("WidgetPos_t *", "struct WidgetPos_t*"),
        "PWidgetName": ("char *", "char*"),
        "HWidgetTicket": ("WidgetTicket *", "struct WidgetTicket*"),
    }
    metadata = with_methods(
        host,
        method("StartQuery", "HWidgetQuery"),
        method("EndQuery", "void", ("hQuery", "HWidgetQuery")),
        method("MovePos", "bool", ("pPos", "PWidgetPos")),
        method("GetLabel", "PWidgetName"),
        method("Redeem", "bool", ("hTicket", "HWidgetTicket")),
    )
    metadata = dataclasses.replace(
        metadata,
        typedefs=(
            *metadata.typedefs,
            *(ir.Typedef(name, parse_type(c), "test") for name, (c, _) in pointers.items()),
        ),
        external_types=(ir.ExternalType("WidgetTicket", "incomplete", "widgets.h", "test"),),
    )
    metadata = with_layouts(
        metadata,
        records=(ir.RecordLayout("WidgetTicket", "struct", "opaque", "widgets.h:9"),),
        typedefs=tuple(ir.TypedefLayout(n, text, 8, 8) for n, (_, text) in pointers.items()),
    )
    generated = generate(metadata)
    assert skips(generated)["WidgetAPI_IWidgets_Redeem"] == (
        "opaque",
        "parameter hTicket: WidgetTicket is never defined, so its size is unknown",
    )
    lines = generated.text.splitlines()
    for line in (
        "HWidgetQuery = bindings.VoidPointer",
        "PWidgetPos = ctypes.POINTER(WidgetPos_t)",
        "PWidgetName = bindings.CString",
        "HWidgetTicket = bindings.VoidPointer",
        '    "WidgetAPI_IWidgets_StartQuery": (bindings.VoidPointer, (IWidgets,)),',
        '    "WidgetAPI_IWidgets_EndQuery": (None, (IWidgets, bindings.Out(None))),',
        '    "WidgetAPI_IWidgets_MovePos": (ctypes.c_bool, (IWidgets, bindings.Out(WidgetPos_t))),',
        '    "WidgetAPI_IWidgets_GetLabel": (bindings.CString, (IWidgets,)),',
    ):
        assert line in lines
    module = load(generated.text, tmp_path)
    assert module.PWidgetPos._type_ is module.WidgetPos_t


def field(
    name: str, ctype: str, size: int, offset: int, align: int | None = None
) -> ir.FieldLayout:
    return ir.FieldLayout(name, ctype, size, align or size, offset)


RECORDS = (
    ir.RecordLayout(
        "Packed_t",
        "struct",
        "complete",
        "test",
        9,
        1,
        (field("m_ubKind", "unsigned char", 1, 0), field("m_ulId", "unsigned long long", 8, 1)),
    ),
    ir.RecordLayout(
        "Packed4_t",
        "struct",
        "complete",
        "test",
        12,
        4,
        (field("m_ubKind", "unsigned char", 1, 0), field("m_ulId", "unsigned long long", 8, 4)),
    ),
    # alignas(16), which no _pack_ reproduces.
    ir.RecordLayout(
        "Aligned_t",
        "struct",
        "complete",
        "test",
        16,
        16,
        (field("m_unValue", "unsigned int", 4, 0),),
    ),
    ir.RecordLayout(
        "HoldsAligned_t",
        "struct",
        "complete",
        "test",
        16,
        16,
        (field("m_aligned", "struct Aligned_t", 16, 0, 16),),
    ),
    ir.RecordLayout(
        "PointsAtAligned_t",
        "struct",
        "complete",
        "test",
        16,
        8,
        (
            field("m_pAligned", "struct Aligned_t*", 8, 0),
            field("m_pSelf", "struct PointsAtAligned_t*", 8, 8),
        ),
    ),
    ir.RecordLayout(
        "Bits_t",
        "struct",
        "complete",
        "test",
        4,
        4,
        (
            ir.FieldLayout("m_low", "unsigned int", 4, 4, bit_offset=0, bit_width=3),
            ir.FieldLayout("m_high", "unsigned int", 4, 4, bit_offset=3, bit_width=29),
        ),
    ),
    ir.RecordLayout("Empty_t", "struct", "complete", "test", 1, 1),
    ir.RecordLayout(
        "Mixed_t",
        "struct",
        "complete",
        "test",
        24,
        8,
        (
            ir.FieldLayout(
                "",
                "union",
                8,
                8,
                0,
                record=ir.RecordLayout(
                    "",
                    "union",
                    "complete",
                    "test",
                    8,
                    8,
                    (field("m_n", "int", 4, 0), field("m_ll", "long long", 8, 0)),
                ),
            ),
            field("m_grid", "short[2][3]", 12, 8, 2),
            field("_pad1__", "unsigned short", 2, 20),
        ),
    ),
    ir.RecordLayout(
        "Reserved_t", "struct", "complete", "test", 4, 4, (field("_fields_", "unsigned int", 4, 0),)
    ),
)


def test_records_get_the_layout_the_compiler_found(widgets: ir.Metadata, tmp_path: Path) -> None:
    target = HOST or "windows-x86_64"
    metadata = with_layouts(on(widgets, target), records=RECORDS)
    generated = generate(metadata)
    found = skips(generated)
    assert found["Aligned_t"] == (
        "unsupported-record",
        "no _pack_ makes ctypes lay out Aligned_t as compiled",
    )
    assert found["HoldsAligned_t"] == (
        "unsupported-record",
        "HoldsAligned_t.m_aligned holds Aligned_t: no _pack_ makes ctypes lay out Aligned_t as "
        "compiled",
    )
    assert found["Reserved_t"] == ("invalid-name", "Reserved_t._fields_ cannot be a ctypes member")
    assert {"Packed_t", "Packed4_t", "PointsAtAligned_t", "Bits_t", "Empty_t", "Mixed_t"} <= set(
        generated.generated["record"]
    )
    assert {(note.name, note.message) for note in generated.notes} >= {
        (
            "Bits_t",
            "its bit-fields m_low, m_high are not exposed: its 4 bytes are kept as storage of "
            "the same alignment",
        ),
        ("Empty_t", "it has no data members: its 1 byte is kept as storage of the same alignment"),
        (
            "PointsAtAligned_t.m_pAligned",
            "a pointer to struct Aligned_t, which has no class, kept untyped",
        ),
    }
    text = generated.text
    assert 'class Packed_t(ctypes.Structure):\n    _pack_ = 1\n    _layout_ = "ms"\n' in text
    assert "class Packed4_t(ctypes.Structure):\n    _pack_ = 4\n" in text
    assert '    ("m_pSelf", ctypes.POINTER(PointsAtAligned_t)),' in text
    assert '    ("m_pAligned", bindings.VoidPointer),' in text
    assert '    ("m_grid", ctypes.c_int16 * 3 * 2),' in text
    assert '    _anonymous_ = ("_0",)' in text
    if HOST is None:
        return
    module = load(text, tmp_path)
    assert ctypes.sizeof(module.Packed_t) == 9
    assert module.Packed4_t.m_ulId.offset == 4
    mixed = module.Mixed_t()
    mixed.m_ll = -1
    mixed.m_grid[1][2] = 7
    assert (mixed.m_n, mixed.m_grid[1][2], mixed._pad1__) == (-1, 7, 0)
    assert ctypes.sizeof(module.Bits_t) == 4
    assert ctypes.sizeof(module.Empty_t) == 1


def test_a_layout_ctypes_disagrees_with_fails_the_import(host: ir.Metadata, tmp_path: Path) -> None:
    text = generate(host).text
    assert "    WidgetPos_t: (12, 4, (" in text
    with pytest.raises(runtime.BindingError, match="WidgetPos_t is 12 bytes aligned to 4, not 16"):
        load(text.replace("    WidgetPos_t: (12, 4, (", "    WidgetPos_t: (16, 4, ("), tmp_path)


@host_only
def test_declarations_for_another_target_fail_the_import(
    widgets: ir.Metadata, tmp_path: Path
) -> None:
    text = generate(with_sizes(on(widgets, OTHER))).text
    compile(text, "declarations.py", "exec")
    with pytest.raises(runtime.BindingError, match=f"are for {OTHER}, but this process is {HOST}"):
        load(text, tmp_path)


def test_names_python_cannot_use_are_skipped(widgets: ir.Metadata) -> None:
    kind = ir.Enum(
        "Kind",
        "Widget::Kind",
        (
            ir.EnumValue("k_EKindOne", "1", 1),
            ir.EnumValue("k_EKindLater", "k_Unknown", None),
            ir.EnumValue("mro", "2", 2),
            ir.EnumValue("_private", "3", 3),
        ),
        "test",
        scope="Widget",
    )
    metadata = dataclasses.replace(
        widgets,
        enums=(*widgets.enums, kind),
        typedefs=(
            *widgets.typedefs,
            ir.Typedef("Widget_Kind", parse_type("unsigned int"), "test"),
            ir.Typedef("lambda", parse_type("unsigned int"), "test"),
        ),
    )
    metadata = with_layouts(
        metadata,
        enums=(ir.EnumLayout("Widget::Kind", "int", 4),),
        typedefs=(
            ir.TypedefLayout("Widget_Kind", "unsigned int", 4, 4),
            ir.TypedefLayout("lambda", "unsigned int", 4, 4),
        ),
    )
    found = skips(generate(metadata))
    assert found["Widget::Kind"] == (
        "invalid-name",
        "Widget::Kind and Widget_Kind would both be Widget_Kind in Python",
    )
    assert found["Widget_Kind"][0] == "invalid-name"
    assert found["lambda"] == ("invalid-name", "lambda has no usable Python name")
    kind_only = dataclasses.replace(widgets, enums=(*widgets.enums, kind))
    kind_only = with_layouts(kind_only, enums=(ir.EnumLayout("Widget::Kind", "unsigned int", 4),))
    generated = generate(kind_only)
    assert skips(generated)["Widget::Kind::k_EKindLater"] == (
        "unsupported-type",
        "Widget::Kind::k_EKindLater has no evaluated value",
    )
    assert skips(generated)["Widget::Kind::mro"][0] == "invalid-name"
    assert skips(generated)["Widget::Kind::_private"][0] == "invalid-name"
    assert "class Widget_Kind(enum.IntEnum):\n    k_EKindOne = 1\n" in generated.text


@pytest.mark.parametrize(("underlying", "stored"), [("int", -1), ("unsigned int", 2**32 - 1)])
def test_enum_values_are_written_as_the_target_stores_them(
    widgets: ir.Metadata, underlying: str, stored: int
) -> None:
    (enum,) = layout_of(widgets).enums
    layout = dataclasses.replace(
        layout_of(widgets), enums=(dataclasses.replace(enum, underlying=underlying),)
    )
    assert widgets.abi is not None
    metadata = dataclasses.replace(widgets, abi=dataclasses.replace(widgets.abi, targets=(layout,)))
    text = generate(metadata).text
    storage = "ctypes.c_int32" if underlying == "int" else "ctypes.c_uint32"
    assert f"# enum EWidgetResult: {storage}\n" in text
    assert f"    k_EWidgetResultFail = {stored}\n" in text
    assert f'    ("m_eResult", {storage}),  # EWidgetResult' in text


def test_generation_does_not_depend_on_declaration_order(widgets: ir.Metadata) -> None:
    metadata = with_sizes(widgets)
    first = generate(metadata)
    shuffled = dataclasses.replace(
        metadata,
        typedefs=metadata.typedefs[::-1],
        enums=metadata.enums[::-1],
    )
    assert generate(shuffled).text == first.text


@pytest.mark.parametrize("target", TARGETS)
def test_declarations_are_python_on_every_target(widgets: ir.Metadata, target: str) -> None:
    generated = generate(with_sizes(on(widgets, target)))
    compile(generated.text, f"{target}.py", "exec")
    assert f'TARGET = "{target}"\n' in generated.text


def test_the_summary_counts_and_explains_everything(widgets: ir.Metadata) -> None:
    text = bindings.summary(generate(widgets))
    lines = text.splitlines()
    assert lines[0] == (
        "Declarations for windows-x86_64 from SDK 9.99, layouts checked by Test C++ 1.0"
    )
    assert "  functions        5  1 generated, 4 skipped" in lines
    assert "  accessors        1  1 generated" in lines
    assert "  exports          1  0 generated, 1 skipped" in lines
    assert "  records          2  2 generated" in lines
    assert any(re.fullmatch(r"  unsupported-type +4  uses a type .*", line) for line in lines)
    assert (
        "  function WidgetAPI_IWidgets_GetWidgetCount: the result: no verified layout on "
        "windows-x86_64 gives the size of int" in lines
    )
    assert lines[-1] == (
        "  export WidgetAPI_Init: declared at widget_api.h:3, but steam_api.json gives no signature"
    )
