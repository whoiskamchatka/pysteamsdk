import pytest

from codegen import ir
from codegen.cdecl import parse_type
from codegen.cexpr import ExpressionError, evaluate_constants, integer_value

TYPEDEFS = tuple(
    ir.Typedef(name, parse_type(spelling), "test")
    for name, spelling in (
        ("int16", "short"),
        ("uint16", "unsigned short"),
        ("uint32", "unsigned int"),
        ("uint64", "unsigned long long"),
        ("Handle_t", "uint64"),
        ("Long_t", "long"),
        ("Pointer_t", "void *"),
        ("Loop_a", "Loop_b"),
        ("Loop_b", "Loop_a"),
    )
)

Other = tuple[str, str, str]


def evaluate(expression: str, ctype: str, *others: Other) -> tuple[ir.Constant, list[ir.Issue]]:
    """Evaluate ``expression`` as k_value; ``others`` are (name, type, expression) constants."""
    constants = [
        ir.Constant("k_value", parse_type(ctype), expression, "test"),
        *(ir.Constant(name, parse_type(t), text, f"test/{name}") for name, t, text in others),
    ]
    found, issues = evaluate_constants(constants, TYPEDEFS)
    assert found[0].expression == expression
    return found[0], [issue for issue in issues if issue.source == "test"]


def value_of(expression: str, ctype: str = "int", *others: Other) -> int | float | None:
    constant, issues = evaluate(expression, ctype, *others)
    assert not issues
    return constant.value


def error_of(expression: str, ctype: str = "int", *others: Other) -> str:
    constant, issues = evaluate(expression, ctype, *others)
    (issue,) = issues
    assert constant.value is None
    assert (issue.severity, issue.code) == ("warning", "unevaluated-constant")
    return issue.message


@pytest.mark.parametrize(
    ("expression", "ctype", "expected"),
    [
        ("0", "int", 0),
        ("42", "int", 42),
        ("0x2A", "int", 42),
        ("0X2a", "int", 42),
        ("052", "int", 42),
        ("42u", "uint32", 42),
        ("42LL", "int", 42),
        ("4294967296", "uint64", 2**32),
        ("18446744073709551615u", "uint64", 2**64 - 1),
        ("0xffffffffffffffffull", "uint64", 2**64 - 1),
        ("'d'", "int", 100),
        ("' '", "int", 32),
        ("512 * 1024", "int", 524288),
        ("128 + 1", "uint32", 129),
        ("100 * 1024 * 1024", "uint32", 104857600),
        ("1 + 2 * 3", "int", 7),
        ("(1 + 2) * 3", "int", 9),
        ("1 << 2 + 1", "int", 8),
        ("1 | 2 << 3", "int", 17),
        ("- 1", "int", -1),
        ("- - 5", "int", 5),
        ("- 2147483648", "int", -(2**31)),
        ("~ 0", "int", -1),
        ("~0u", "uint32", 2**32 - 1),
        ("-1u", "uint32", 2**32 - 1),
        ("0xFFFFFFFFu + 1", "uint32", 0),
        ("0x80000000u << 1", "uint32", 0),
        ("4294967295u * 2u", "uint32", 2**32 - 2),
        ("-1 | 0u", "uint32", 2**32 - 1),
        ("2147483647 + 1u", "uint32", 2**31),
        ("-1 + 0ull", "uint64", 2**64 - 1),
        ("( uint32 ) 'd' << 16U", "uint32", 6553600),
        ("( Handle_t ) ~ 0", "Handle_t", 2**64 - 1),
        ("( unsigned int ) -1", "uint32", 2**32 - 1),
        ("( uint16 ) 65537", "uint16", 1),
        ("( int16 ) 1 + 1", "int", 2),
        ("600.f", "float", 600.0),
        ("1e3f", "float", 1000.0),
        (".25F", "float", 0.25),
        ("0.5", "double", 0.5),
        ("2.5e-1", "double", 0.25),
    ],
)
def test_supported_expressions_follow_c_semantics(
    expression: str, ctype: str, expected: int | float
) -> None:
    value = value_of(expression, ctype)
    assert type(value) is type(expected)
    assert value == expected


def test_conversion_to_the_declared_type_wraps_like_cpp20() -> None:
    constant, (note,) = evaluate("0xffffffff", "int")
    assert constant.value == -1
    assert (note.severity, note.code) == ("note", "wrapped-constant")
    assert "4294967295 does not fit int and wraps to -1" in note.message
    constant, (note,) = evaluate("40000", "int16")
    assert constant.value == -25536
    # Conversion to an unsigned type is defined modulo 2**N in every C and C++ version.
    assert value_of("0x1ffff", "uint16") == 0xFFFF


def test_references_use_the_referenced_constant_type() -> None:
    assert value_of("k_a | k_b", "int", ("k_a", "int", "1"), ("k_b", "int", "8")) == 9
    # uint32 + int is unsigned int arithmetic in C, so this wraps before the uint64 conversion.
    assert value_of("k_big + 1", "uint64", ("k_big", "uint32", "0xFFFFFFFF")) == 0


def test_references_resolve_in_any_declaration_order() -> None:
    constants = [
        ir.Constant(name, parse_type("int"), expression, name)
        for name, expression in (("k_c", "k_b * 2"), ("k_b", "k_a + 1"), ("k_a", "20"))
    ]
    found, issues = evaluate_constants(constants, ())
    assert [c.value for c in found] == [42, 21, 20]
    assert not issues
    assert evaluate_constants(constants[::-1], ())[0] == found[::-1]


def test_a_scoped_constant_sees_its_own_scope_first() -> None:
    constants = [
        ir.Constant("k_n", parse_type("int"), "1", "global"),
        ir.Constant("k_n", parse_type("int"), "2", "scoped", scope="Widget_t"),
        ir.Constant("k_inner", parse_type("int"), "k_n", "inner", scope="Widget_t"),
        ir.Constant("k_outer", parse_type("int"), "k_n", "outer"),
        ir.Constant("k_other", parse_type("int"), "k_n", "other", scope="Gadget_t"),
    ]
    found, _ = evaluate_constants(constants, ())
    assert [c.value for c in found] == [1, 2, 2, 1, 1]


@pytest.mark.parametrize(
    ("expression", "ctype", "message"),
    [
        ("", "int", "empty expression"),
        ("1 +", "int", "expression ends early"),
        ("(1", "int", "expression ends early"),
        ("1 2", "int", "unexpected '2'"),
        ("1 - 2", "int", "'-' is not supported here"),
        ("1 / 2", "int", "'/' is not supported here"),
        ("7 % 2", "int", "'%' is not supported here"),
        ("1 >> 1", "int", "'>>' is not supported here"),
        ("1 & 1", "int", "'&' is not supported here"),
        ("1 ^ 1", "int", "'^' is not supported here"),
        ("1 ? 2 : 3", "int", "'?' is not supported here"),
        ("!1", "int", "'!' is not supported here"),
        ("1, 2", "int", "',' is not supported here"),
        ("5L", "int", "type long, which is not portable"),
        ("5ul", "int", "type long, which is not portable"),
        ("( long ) 1", "int", "long is not portable"),
        ("( Long_t ) 1", "int", "long is not portable"),
        ("1", "Long_t", "long is not portable"),
        ("( char ) 1", "int", "char is signed on some platforms"),
        ("( bool ) 1", "int", "bool is not a supported arithmetic type"),
        ("( Pointer_t ) 0", "int", "void * is not an arithmetic type"),
        ("( void * ) 0", "int", "only casts to arithmetic types are supported"),
        ("1", "Loop_a", "refers to itself"),
        ("1", "Unknown_t", "Unknown_t is not a supported arithmetic type"),
        ("0", "const char *", "const char * is not an arithmetic type"),
        ("08", "int", "08 is not a valid octal literal"),
        ("0x", "int", "0x has an invalid suffix"),
        ("1f", "float", "1f has an invalid suffix"),
        ("1lL", "int", "1lL has an invalid suffix"),
        ("1.5l", "double", "long double, which is not supported"),
        ("1.5q", "double", "1.5q has an invalid suffix"),
        ("'ab'", "int", "not one printable ASCII character"),
        ("'\\n'", "int", "not one printable ASCII character"),
        ("''", "int", "not one printable ASCII character"),
        ("'", "int", "not one printable ASCII character"),
        ("2147483647 + 1", "int", "2147483647 + 1 overflows int, which C leaves undefined"),
        ("65536 * 65536", "int", "overflows int"),
        ("~ 2147483647 * - 1", "int", "overflows int"),
        ("9223372036854775807 + 1", "uint64", "overflows long long"),
        ("1 << 31", "int", "1 << 31 overflows int"),
        ("1 << 32", "int", "shifting int by 32 is undefined"),
        ("1u << 32", "uint32", "shifting unsigned int by 32 is undefined"),
        ("1 << -1", "int", "shifting int by -1 is undefined"),
        ("-1 << 1", "int", "shifting negative -1 left is undefined"),
        ("9223372036854775808", "uint64", "too large for any integer type"),
        ("18446744073709551616u", "uint64", "too large for any integer type"),
        ("0.1f", "float", "not exactly representable as float"),
        ("0.1", "double", "not exactly representable as double"),
        ("1e39f", "float", "out of range for float"),
        ("1e999999999", "double", "out of range"),
        ("1.5f + 1", "float", "floating-point arithmetic is not supported"),
        ("- 1.5", "double", "floating-point arithmetic is not supported"),
        ("1.5", "int", "converting floating-point to int is not supported"),
        ("1", "float", "converting an integer to float is not supported"),
        ("k_missing", "int", "k_missing is not a known constant"),
    ],
)
def test_unsupported_or_undefined_expressions_are_reported(
    expression: str, ctype: str, message: str
) -> None:
    assert message in error_of(expression, ctype)


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        ("__import__('os').system('ls')", "__import__ is not a known constant"),
        ("eval('1')", "eval is not a known constant"),
        ("True", "True is not a known constant"),
        ("2 ** 10", "'*' is not supported here"),
        ("1; 2", "';' is not supported here"),
        ("1 if 1 else 0", "unexpected 'if'"),
        ("0b101", "invalid suffix"),
        ("1_000", "unexpected '_000'"),
        ('"text"', "unexpected character '\"'"),
        (chr(0xFF11), "unexpected character"),
        ("1 + " * 100 + "1", "longer than 256 characters"),
        ("(" * 40 + "1" + ")" * 40, "nested more than 32 levels deep"),
        ("- " * 40 + "1", "nested more than 32 levels deep"),
    ],
)
def test_hostile_input_is_rejected_without_evaluating_it(expression: str, message: str) -> None:
    assert message in error_of(expression)


def test_cycles_and_their_dependents_are_reported() -> None:
    constants = [
        ir.Constant(name, parse_type("int"), expression, name)
        for name, expression in (
            ("k_a", "k_b + 1"),
            ("k_b", "k_a"),
            ("k_c", "k_a * 2"),
            ("k_d", "k_d"),
        )
    ]
    found, issues = evaluate_constants(constants, ())
    assert [c.value for c in found] == [None] * 4
    messages = {issue.source: issue.message for issue in issues}
    assert "part of a reference cycle: k_a -> k_b -> k_a" in messages["k_a"]
    assert "part of a reference cycle: k_a -> k_b -> k_a" in messages["k_b"]
    assert messages["k_c"].endswith("k_a cannot be evaluated")
    assert "part of a reference cycle: k_d -> k_d" in messages["k_d"]


def test_long_reference_chains_do_not_exhaust_the_stack() -> None:
    constants = [ir.Constant("k_0", parse_type("int"), "0", "k_0")]
    constants += [
        ir.Constant(f"k_{n}", parse_type("int"), f"k_{n - 1} + 1", f"k_{n}") for n in range(1, 3000)
    ]
    found, issues = evaluate_constants(constants[::-1], ())
    assert found[0].value == 2999
    assert not issues


def test_a_reference_to_a_repeated_name_is_ambiguous() -> None:
    message = error_of("k_twice", "int", ("k_twice", "int", "1"), ("k_twice", "int", "2"))
    assert "k_twice is declared 2 times" in message


def test_enumerator_values_are_integer_expressions_without_names() -> None:
    assert integer_value("-2") == -2
    assert integer_value("1 << 3") == 8
    assert integer_value("0x80000000") == 2**31
    with pytest.raises(ExpressionError, match="k_EOther is not a known constant"):
        integer_value("k_EOther")
    with pytest.raises(ExpressionError, match="not an integer"):
        integer_value("1.5")
