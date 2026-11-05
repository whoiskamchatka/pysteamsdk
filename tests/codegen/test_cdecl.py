import pytest

from codegen.cdecl import TypeSyntaxError, parse_param, parse_type, split_params
from codegen.ir import CType


@pytest.mark.parametrize(
    ("spelling", "base", "const", "pointers", "reference", "array"),
    [
        ("int", "int", False, 0, False, None),
        ("unsigned long long", "unsigned long long", False, 0, False, None),
        ("const char *", "char", True, 1, False, None),
        ("char **", "char", False, 2, False, None),
        ("const Widget_t &", "Widget_t", True, 0, True, None),
        ("uint8 [16]", "uint8", False, 0, False, 16),
        ("IWidgets::EShape", "IWidgets::EShape", False, 0, False, None),
        ("IWidgets*", "IWidgets", False, 1, False, None),
    ],
)
def test_named_types_keep_their_spelling(
    spelling: str, base: str, const: bool, pointers: int, reference: bool, array: int | None
) -> None:
    assert parse_type(spelling) == CType(
        spelling, base=base, const=const, pointers=pointers, reference=reference, array=array
    )


def test_function_pointers_are_split_into_return_and_parameter_types() -> None:
    parsed = parse_type("void (*)(EKind, const char *)")
    assert parsed.kind == "function_pointer"
    assert parsed.spelling == "void (*)(EKind, const char *)"
    assert parsed.returns == CType("void", base="void")
    assert parsed.params == (CType("EKind", base="EKind"), parse_type("const char *"))
    assert parsed.referenced_names() == ("void", "EKind", "char")


def test_spacing_does_not_change_the_shape() -> None:
    assert parse_type("IWidgets* ").shape() == parse_type("IWidgets *").shape()
    assert parse_type("const char*").shape() != parse_type("char *").shape()


@pytest.mark.parametrize(
    "spelling",
    [
        "",
        "char const *",
        "struct Widget_t",
        "int [k_nMax]",
        "int x",
        "void (*)(int x)",
        "Holder<int>",
        "int *const",
    ],
)
def test_unsupported_spellings_are_rejected(spelling: str) -> None:
    with pytest.raises(TypeSyntaxError):
        parse_type(spelling)


def test_parameters_split_at_top_level_commas_only() -> None:
    assert split_params(" int a, void (*)(int, char) b ") == ["int a", "void (*)(int, char) b"]
    assert split_params("  ") == []


def test_parameters_have_a_type_and_a_name() -> None:
    assert parse_param("IWidgets* self") == (parse_type("IWidgets*"), "self")
    assert parse_param("const char * pchKey")[1] == "pchKey"
    for unnamed in ("int", "unsigned int", "const char *"):
        with pytest.raises(TypeSyntaxError):
            parse_param(unnamed)
