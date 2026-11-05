from collections import Counter
from collections.abc import Callable
from dataclasses import replace

import pytest

from codegen import ir
from codegen.cdecl import parse_type
from codegen.validate import validate

Change = Callable[[ir.Metadata], ir.Metadata]


@pytest.fixture
def widgets(widgets: ir.Metadata) -> ir.Metadata:
    # The changes below add declarations without layouts; test_abi_checks.py covers layouts.
    return replace(widgets, abi=None)


def codes(metadata: ir.Metadata) -> Counter[tuple[str, str]]:
    return Counter((issue.severity, issue.code) for issue in validate(metadata))


def struct(metadata: ir.Metadata, name: str) -> ir.Struct:
    (found,) = [item for item in metadata.structs if item.name == name]
    return found


def with_struct(metadata: ir.Metadata, *structs: ir.Struct) -> ir.Metadata:
    return replace(metadata, structs=(*metadata.structs, *structs))


def with_method(metadata: ir.Metadata, index: int, **changes: object) -> ir.Metadata:
    (interface,) = metadata.interfaces
    methods = list(interface.methods)
    methods[index] = replace(methods[index], **changes)  # type: ignore[arg-type]
    return replace(metadata, interfaces=(replace(interface, methods=tuple(methods)),))


def test_the_fixture_is_consistent(widgets: ir.Metadata) -> None:
    assert validate(widgets) == []


def gone(metadata: ir.Metadata) -> ir.Struct:
    """A second callback struct for ID 701, with the same fields as WidgetReady_t."""
    ready = struct(metadata, "WidgetReady_t")
    return replace(ready, name="WidgetGone_t", source="test#/gone")


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda m: with_struct(m, ir.Struct("WidgetPos_t", (), "dup")), "duplicate-declaration"),
        (
            lambda m: replace(
                m, typedefs=(*m.typedefs, ir.Typedef("IWidgets", parse_type("int"), "dup"))
            ),
            "duplicate-declaration",
        ),
        (
            lambda m: replace(
                m, external_types=(ir.ExternalType("WidgetPos_t", "struct", "w.h", "dup"),)
            ),
            "duplicate-declaration",
        ),
        (lambda m: replace(m, constants=(*m.constants, m.constants[0])), "duplicate-declaration"),
        (
            lambda m: replace(
                m,
                constants=(
                    *m.constants,
                    ir.Constant("k_EWidgetResultOK", parse_type("int"), "1", "dup", value=1),
                ),
            ),
            "duplicate-declaration",
        ),
        (
            lambda m: with_struct(
                m, ir.Struct("Pair_t", (ir.Field("x", parse_type("int")),) * 2, "dup")
            ),
            "duplicate-member",
        ),
        (
            lambda m: with_method(m, 1, params=(*m.interfaces[0].methods[1].params,) * 2),
            "duplicate-member",
        ),
        (
            lambda m: with_method(m, 0, flat_name="WidgetAPI_Widgets_v007"),
            "duplicate-flat-function",
        ),
        (
            lambda m: replace(
                m, interfaces=(*m.interfaces, ir.Interface("IGadgets", (), "g", "Widgets007"))
            ),
            "duplicate-version",
        ),
    ],
)
def test_conflicting_declarations_are_errors(
    widgets: ir.Metadata, change: Change, code: str
) -> None:
    assert ("error", code) in codes(change(widgets))


def test_a_call_result_must_name_a_callback_struct(widgets: ir.Metadata) -> None:
    changed = with_method(widgets, 4, callresult="WidgetPos_t")
    assert codes(changed) == {("error", "unknown-callback-struct"): 1}


def test_a_shared_callback_id_is_a_warning_until_reviewed(widgets: ir.Metadata) -> None:
    shared = with_struct(widgets, gone(widgets))
    (issue,) = validate(shared)
    assert (issue.severity, issue.code) == ("warning", "shared-callback-id")
    assert issue.message == "callback ID 701 is shared by WidgetGone_t, WidgetReady_t"
    review = ir.SharedCallback(701, ("WidgetReady_t", "WidgetGone_t"), "overrides.toml#/x/0")
    assert validate(replace(shared, shared_callbacks=(review,))) == []


@pytest.mark.parametrize(
    ("structs", "fields", "message"),
    [
        (("WidgetReady_t", "WidgetGone_t"), 1, "differ in fields"),
        (("WidgetReady_t", "WidgetPos_t"), 2, "belongs to WidgetGone_t, WidgetReady_t"),
        (("WidgetReady_t",), 2, "at least two different structs"),
        (("WidgetReady_t", "WidgetReady_t"), 2, "at least two different structs"),
    ],
)
def test_a_review_must_match_the_shared_callback(
    widgets: ir.Metadata, structs: tuple[str, ...], fields: int, message: str
) -> None:
    other = gone(widgets)
    other = replace(other, fields=other.fields[:fields])
    review = ir.SharedCallback(701, structs, "overrides.toml#/x/0")
    changed = replace(with_struct(widgets, other), shared_callbacks=(review,))
    (issue,) = [i for i in validate(changed) if i.severity == "error"]
    assert (issue.code, issue.source) == ("shared-callback-mismatch", "overrides.toml#/x/0")
    assert message in issue.message


def test_undeclared_types_are_warnings_not_errors(widgets: ir.Metadata) -> None:
    opaque = ir.Struct("Holder_t", (ir.Field("m_p", parse_type("Opaque_t *")),), "test")
    changed = with_struct(widgets, opaque)
    assert codes(changed) == {("warning", "unresolved-type"): 1}
    external = ir.ExternalType("Opaque_t", "incomplete", "opaque.h", "overrides.toml#/x/0")
    assert validate(replace(changed, external_types=(external,))) == []


def test_external_types_must_be_used_and_incomplete_ones_only_by_pointer(
    widgets: ir.Metadata,
) -> None:
    external = ir.ExternalType("Opaque_t", "incomplete", "opaque.h", "overrides.toml#/x/0")
    assert codes(replace(widgets, external_types=(external,))) == {
        ("error", "unused-external-type"): 1
    }
    by_value = ir.Struct("Holder_t", (ir.Field("m_o", parse_type("Opaque_t")),), "test")
    changed = replace(with_struct(widgets, by_value), external_types=(external,))
    assert codes(changed) == {("error", "incomplete-type-by-value"): 1}


def test_typedef_cycles_are_errors(widgets: ir.Metadata) -> None:
    loop = (
        ir.Typedef("Loop_a", parse_type("Loop_b"), "a"),
        ir.Typedef("Loop_b", parse_type("Loop_a"), "b"),
    )
    found = validate(replace(widgets, typedefs=(*widgets.typedefs, *loop)))
    assert [(i.code, i.source) for i in found] == [("typedef-cycle", "a"), ("typedef-cycle", "b")]


def test_a_type_must_match_its_spelling(widgets: ir.Metadata) -> None:
    wrong = ir.CType("char *", base="int", pointers=1)
    changed = with_method(widgets, 0, returns=wrong)
    assert codes(changed) == {("error", "inconsistent-type"): 1}


@pytest.mark.parametrize("value", [255, 256.0, None])
def test_constant_values_must_match_their_expressions(
    widgets: ir.Metadata, value: int | float | None
) -> None:
    constants = list(widgets.constants)
    assert constants[0].name == "k_cbWidgetBufferSize"
    constants[0] = replace(constants[0], value=value)
    found = validate(replace(widgets, constants=tuple(constants)))
    assert [(i.code, i.source) for i in found] == [
        ("constant-value-mismatch", "steam_api.json#/consts/2")
    ]


def test_a_value_its_expression_cannot_give_is_an_error(widgets: ir.Metadata) -> None:
    constants = list(widgets.constants)
    constants[0] = replace(constants[0], expression="256 / 1")
    (issue,) = validate(replace(widgets, constants=tuple(constants)))
    assert issue.code == "constant-value-mismatch"
    assert issue.message == "k_cbWidgetBufferSize is 256, but '256 / 1' gives None"
