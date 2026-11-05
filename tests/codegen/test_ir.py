import dataclasses
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from codegen import ir
from codegen.extract import extract

MakeSdk = Callable[..., Path]


def test_the_fixture_round_trips_byte_for_byte(widgets_text: str) -> None:
    # The fixture is in canonical form, so this also pins the serialization on every Python
    # version CI runs.
    metadata = ir.loads(widgets_text)
    assert ir.dumps(metadata) == widgets_text
    assert ir.loads(ir.dumps(metadata)) == metadata


def test_extracted_metadata_round_trips(make_sdk: MakeSdk) -> None:
    metadata = extract(make_sdk()).metadata
    assert ir.loads(ir.dumps(metadata)) == metadata


def test_provenance_and_member_order_survive_a_round_trip(widgets: ir.Metadata) -> None:
    (interface,) = widgets.interfaces
    changed = dataclasses.replace(
        widgets,
        interfaces=(dataclasses.replace(interface, version_source="overrides.toml#/x/0"),),
    )
    (read,) = ir.loads(ir.dumps(changed)).interfaces
    assert read.version_source == "overrides.toml#/x/0"
    assert read.methods[4].source == "steam_api.json#/interfaces/0/methods/4"
    assert [m.name for m in read.methods] == [m.name for m in interface.methods]
    assert [p.name for p in read.methods[1].params] == ["iWidget", "pchName", "cchName"]


def test_integer_and_float_values_stay_distinct(widgets: ir.Metadata) -> None:
    values = {c.name: c.value for c in ir.loads(ir.dumps(widgets)).constants}
    assert type(values["k_flWidgetScale"]) is float
    assert type(values["k_cchWidgetNameMax"]) is int


@pytest.mark.parametrize(
    ("version", "message"),
    [
        (2, "format 2 is not supported; extract the SDK again for 3"),
        (4, "format 4 is not supported"),
        ("3", "format '3' is not supported"),
        (True, "format True is not supported"),
        (None, "format None is not supported"),
    ],
)
def test_other_format_versions_are_rejected(
    widgets_text: str, version: object, message: str
) -> None:
    data = json.loads(widgets_text)
    data["format"] = version
    with pytest.raises(ir.ReadError, match=message):
        ir.loads(json.dumps(data))


# Metadata of the current format with an unknown field "x", whose value comes next.
FIELD_X = f'{{"format": {ir.FORMAT}, "x": '


# Cases with huge texts get short ids: pytest puts each test's id in an environment variable,
# and Windows refuses variables longer than 32767 characters.
@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "not valid JSON"),
        ("{", "not valid JSON"),
        ("]" * 100 + "[" * 100, "not valid JSON"),
        pytest.param(FIELD_X + '"' + "[" * 100_000, "not valid JSON", id="unterminated"),
        # Unterminated, and full of escaped quotes: the depth scan must stay linear.
        pytest.param('"' + '\\"[' * 100_000, "not valid JSON", id="escaped-quotes"),
        ("[]", "no format"),
        ('{"interfaces": []}', "no format"),
        ('{"format": 3, "format": 3}', "duplicate key 'format'"),
        (FIELD_X + "NaN}", "NaN is not a JSON number"),
    ],
)
def test_malformed_text_is_rejected(text: str, message: str) -> None:
    with pytest.raises(ir.ReadError, match=message):
        ir.loads(text)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(FIELD_X + "[" * ir.MAX_DEPTH + "]" * ir.MAX_DEPTH + "}", id="arrays"),
        pytest.param(
            FIELD_X + '{"x": ' * ir.MAX_DEPTH + "1" + "}" * ir.MAX_DEPTH + "}", id="objects"
        ),
        pytest.param("[" * 100_000 + "]" * 100_000, id="hostile"),
        pytest.param("[" * 100_000, id="hostile-unclosed"),
    ],
)
def test_deep_nesting_is_refused_before_parsing(text: str) -> None:
    # MAX_DEPTH + 1 levels is far below where any Python's JSON parser gives up, so the outcome
    # cannot depend on the interpreter or on the stack it runs with.
    with pytest.raises(ir.ReadError) as raised:
        ir.loads(text)
    assert raised.value.problems == (f"nested more than {ir.MAX_DEPTH} levels deep",)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("[" * (ir.MAX_DEPTH - 1) + "]" * (ir.MAX_DEPTH - 1), id="at-the-limit"),
        pytest.param('"' + "[{" * 100_000 + '"', id="brackets-in-a-string"),
        pytest.param('"\\"' + "[" * 100_000 + '"', id="after-an-escaped-quote"),
    ],
)
def test_nesting_within_the_limit_reaches_the_field_checks(value: str) -> None:
    with pytest.raises(ir.ReadError) as raised:
        ir.loads(FIELD_X + value + "}")
    assert raised.value.problems[0] == "/x: unknown field"


@pytest.mark.parametrize(
    ("value", "message"), [("9" * 5000, "not valid JSON"), ("1e400", "1e400 is out of range")]
)
def test_huge_numbers_are_rejected(widgets_text: str, value: str, message: str) -> None:
    text = widgets_text.replace('"value": 0.5', f'"value": {value}')
    assert text != widgets_text
    with pytest.raises(ir.ReadError, match=message):
        ir.loads(text)


DELETE = object()


def mutate(data: dict[str, Any], path: tuple[str | int, ...], value: object) -> None:
    node: Any = data
    for key in path[:-1]:
        node = node[key]
    if value is DELETE:
        del node[path[-1]]
    else:
        node[path[-1]] = value


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (
            ("interfaces", 0, "methods", 0, "flat_name"),
            DELETE,
            "missing required field 'flat_name'",
        ),
        (("external_types",), DELETE, "/: missing required field 'external_types'"),
        (("structs", 1, "callback_id"), "701", "expected an integer, found a string"),
        (("structs", 1, "callback_id"), True, "expected an integer, found a boolean"),
        (("structs", 1, "callback_id"), 701.0, "expected an integer, found a number"),
        (("typedefs", 0, "comment"), "x", "/typedefs/0/comment: unknown field"),
        (("interfaces", 0, "methods", 0, "returns", "kind"), "pointer", "one of 'named'"),
        (("issues",), [{"severity": "fatal", "code": "x", "message": "", "source": ""}], "'error'"),
        (("constants", 0, "value"), True, "expected an integer or a number or null"),
        (("constants", 0, "value"), "256", "found a string"),
        (("enums",), {}, "/enums: expected a list, found an object"),
        (("enums", 0), [], "/enums/0: expected an object, found a list"),
        (("interfaces", 0, "methods", 1, "params", 1, "annotations", "x"), 1, "found an integer"),
        (("sdk_version",), 9.99, "/sdk_version: expected a string, found a number"),
        (("abi",), [], "/abi: expected an object, found a list"),
        (
            ("abi", "targets", 0, "triple"),
            DELETE,
            "/abi/targets/0: missing required field 'triple'",
        ),
        (
            ("abi", "targets", 0, "records", 0, "status"),
            "partial",
            "one of 'complete', 'opaque', 'unsupported'",
        ),
        (
            ("abi", "targets", 0, "records", 0, "fields", 0, "offset"),
            "0",
            "/abi/targets/0/records/0/fields/0/offset: expected an integer, found a string",
        ),
        (
            ("abi", "targets", 0, "records", 0, "fields", 0, "record"),
            [],
            "/abi/targets/0/records/0/fields/0/record: expected an object, found a list",
        ),
        (("abi", "targets", 1, "enums", 0, "size"), 4.0, "expected an integer, found a number"),
        (
            ("abi", "targets", 1, "typedefs", 0, "size"),
            True,
            "expected an integer, found a boolean",
        ),
    ],
)
def test_fields_are_checked_with_their_location(
    widgets_text: str, path: tuple[str | int, ...], value: object, message: str
) -> None:
    data = json.loads(widgets_text)
    mutate(data, path, value)
    with pytest.raises(ir.ReadError, match=message):
        ir.loads(json.dumps(data))


def test_every_problem_is_listed(widgets_text: str) -> None:
    data = json.loads(widgets_text)
    del data["typedefs"][0]["name"]
    data["structs"][0]["fields"][0]["private"] = "no"
    data["exports"][0]["line"] = 3
    with pytest.raises(ir.ReadError) as raised:
        ir.loads(json.dumps(data))
    assert raised.value.problems == (
        "/structs/0/fields/0/private: expected a boolean, found a string",
        "/typedefs/0: missing required field 'name'",
        "/exports/0/line: unknown field",
    )
