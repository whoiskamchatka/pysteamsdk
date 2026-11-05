"""Evaluate the C constant expressions that steam_api.json gives as constant values.

The grammar is what the SDK's constants use and nothing more: integer, character and
floating-point literals, references to other constants, casts to arithmetic types, unary - and
~, binary *, +, << and |, and parentheses. Anything else is rejected with a reason.

Integers follow C++ on every platform Steam supports, where int has 32 bits and long long 64.
long has 32 bits on Windows but 64 elsewhere, so an expression that needs it is rejected, and so
is one whose result C leaves undefined, such as signed overflow. A floating-point value must be
exactly representable, because C's rounding of literals is not modelled.
"""

import functools
import re
import struct
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from typing import NoReturn

from codegen import ir

MAX_LENGTH = 256
MAX_DEPTH = 32


class ExpressionError(ValueError):
    """An expression outside the supported grammar, or one without a portable value."""


@dataclass(frozen=True)
class _Type:
    name: str
    bits: int
    signed: bool = True
    floating: bool = False

    @property
    def low(self) -> int:
        return -(1 << (self.bits - 1)) if self.signed else 0

    @property
    def high(self) -> int:
        return (1 << (self.bits - 1 if self.signed else self.bits)) - 1


# Fundamental types with the same size and signedness on every supported platform.
_TYPES = {
    item.name: item
    for item in (
        _Type("signed char", 8),
        _Type("unsigned char", 8, signed=False),
        _Type("short", 16),
        _Type("unsigned short", 16, signed=False),
        _Type("int", 32),
        _Type("unsigned int", 32, signed=False),
        _Type("long long", 64),
        _Type("unsigned long long", 64, signed=False),
        _Type("float", 32, floating=True),
        _Type("double", 64, floating=True),
    )
}
_INT, _UINT = _TYPES["int"], _TYPES["unsigned int"]
_LLONG, _ULLONG = _TYPES["long long"], _TYPES["unsigned long long"]
_LONG = "long has 32 bits on Windows and 64 elsewhere"
_NOT_PORTABLE = {
    "long": _LONG,
    "unsigned long": _LONG,
    "char": "char is signed on some platforms and unsigned on others",
}
_TYPE_WORDS = frozenset(
    {"signed", "unsigned", "char", "short", "int", "long", "float", "double", "bool", "void"}
)

_TOKEN = re.compile(
    r"""
      (?P<float> (?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?[A-Za-z]*
               | [0-9]+[eE][+-]?[0-9]+[A-Za-z]* )
    | (?P<integer> 0[xX][0-9A-Fa-f]+[A-Za-z]* | [0-9]+[A-Za-z]* )
    | (?P<char> '[^'\n]*'? )
    | (?P<name> [A-Za-z_][A-Za-z0-9_]* )
    | (?P<operator> <<|>>|&&|\|\||[-+*/%&|^~!<>=?:,.;()\[\]{}] )
    """,
    re.VERBOSE | re.ASCII,
)
_LITERAL_PARTS = re.compile(r"(0[xX][0-9A-Fa-f]+|[0-9]+)([A-Za-z]*)")
_FLOAT_PARTS = re.compile(r"([0-9.]+)(?:[eE]([+-]?[0-9]+))?([A-Za-z]*)")


@dataclass(frozen=True)
class _Value:
    number: int | float
    type: _Type


def evaluate_constants(
    constants: Sequence[ir.Constant], typedefs: Iterable[ir.Typedef]
) -> tuple[tuple[ir.Constant, ...], list[ir.Issue]]:
    """Give each constant the value of its expression, converted to its declared type.

    A name refers to a constant in the same scope first, then at file scope, with the type the
    constant is declared with, as for a C++ const. A constant that cannot be evaluated keeps
    None and is reported with the reason, as is every constant in a reference cycle.
    """
    return _Evaluation(constants, _Types(typedefs)).run()


def integer_value(text: str) -> int:
    """The value of an integer constant expression that names nothing, as an enumerator has.

    No conversion is applied: steam_api.json does not give an enum's underlying type.
    Raises ExpressionError.
    """
    value = _Parser(text, _Types(()), _unknown, []).parse()
    if isinstance(value.number, float):
        raise ExpressionError("not an integer")
    return value.number


def _unknown(name: str) -> NoReturn:
    raise ExpressionError(f"{name} is not a known constant")


class _Types:
    def __init__(self, typedefs: Iterable[ir.Typedef]) -> None:
        self.typedefs = {item.name: item.type for item in typedefs}

    def is_type(self, name: str) -> bool:
        return name in _TYPE_WORDS or name in self.typedefs

    def resolve(self, ctype: ir.CType) -> _Type:
        seen = set()
        while True:
            plain = ctype.pointers == 0 and not ctype.reference and ctype.array is None
            if ctype.kind != "named" or not plain:
                raise ExpressionError(f"{ctype.spelling} is not an arithmetic type")
            if ctype.base not in self.typedefs:
                break
            if ctype.base in seen:
                raise ExpressionError(f"typedef {ctype.base} refers to itself")
            seen.add(ctype.base)
            ctype = self.typedefs[ctype.base]
        if ctype.base in _TYPES:
            return _TYPES[ctype.base]
        if ctype.base in _NOT_PORTABLE:
            raise ExpressionError(f"{ctype.base} is not portable: {_NOT_PORTABLE[ctype.base]}")
        raise ExpressionError(f"{ctype.base} is not a supported arithmetic type")


class _Evaluation:
    def __init__(self, constants: Sequence[ir.Constant], types: _Types) -> None:
        self.constants = constants
        self.types = types
        self.positions: dict[tuple[str | None, str], list[int]] = defaultdict(list)
        for position, constant in enumerate(constants):
            self.positions[constant.scope, constant.name].append(position)
        self.results: dict[int, _Value | None] = {}

    def targets(self, constant: ir.Constant, name: str) -> list[int]:
        return self.positions.get((constant.scope, name)) or self.positions.get((None, name)) or []

    def run(self) -> tuple[tuple[ir.Constant, ...], list[ir.Issue]]:
        dependencies = [
            sorted({p for name in _names(c.expression, self.types) for p in self.targets(c, name)})
            for c in self.constants
        ]
        order, cycles = _order(dependencies)
        constants = list(self.constants)
        issues: list[ir.Issue] = []
        for position in order:
            constant = self.constants[position]
            label = _label(constant)
            notes: list[str] = []
            try:
                if position in cycles:
                    path = " -> ".join(_label(self.constants[p]) for p in cycles[position])
                    raise ExpressionError(f"part of a reference cycle: {path}")
                lookup = functools.partial(self.lookup, constant)
                value = _Parser(constant.expression, self.types, lookup, notes).parse()
                value = _convert(value, self.types.resolve(constant.type), notes)
            except ExpressionError as exc:
                self.results[position] = None
                constants[position] = replace(constant, value=None)
                message = f"{label} = {constant.expression!r}: {exc}"
                issues.append(ir.Issue("warning", "unevaluated-constant", message, constant.source))
                continue
            self.results[position] = value
            constants[position] = replace(constant, value=value.number)
            for note in notes:
                issues.append(
                    ir.Issue("note", "wrapped-constant", f"{label}: {note}", constant.source)
                )
        return tuple(constants), issues

    def lookup(self, constant: ir.Constant, name: str) -> _Value:
        targets = self.targets(constant, name)
        if not targets:
            _unknown(name)
        if len(targets) > 1:
            raise ExpressionError(f"{name} is declared {len(targets)} times")
        value = self.results.get(targets[0])
        if value is None:
            raise ExpressionError(f"{name} cannot be evaluated")
        return value


def _label(constant: ir.Constant) -> str:
    return f"{constant.scope}::{constant.name}" if constant.scope else constant.name


def _names(text: str, types: _Types) -> set[str]:
    try:
        tokens = _tokenize(text)
    except ExpressionError:
        return set()
    return {token for kind, token in tokens if kind == "name" and not types.is_type(token)}


def _order(dependencies: list[list[int]]) -> tuple[list[int], dict[int, list[int]]]:
    """Positions ordered so that each follows those it depends on, and each cycle's path.

    Iterative, so a long chain of references cannot exhaust Python's recursion limit.
    """
    order: list[int] = []
    done: dict[int, bool] = {}
    cycles: dict[int, list[int]] = {}
    for start in range(len(dependencies)):
        if start in done:
            continue
        done[start] = False
        stack = [(start, iter(dependencies[start]))]
        while stack:
            node, pending = stack[-1]
            for child in pending:
                if child not in done:
                    done[child] = False
                    stack.append((child, iter(dependencies[child])))
                    break
                if not done[child]:
                    path = [item for item, _ in stack]
                    cycle = [*path[path.index(child) :], child]
                    for member in cycle:
                        cycles.setdefault(member, cycle)
            else:
                stack.pop()
                done[node] = True
                order.append(node)
    return order, cycles


def _tokenize(text: str) -> list[tuple[str, str]]:
    if len(text) > MAX_LENGTH:
        raise ExpressionError(f"longer than {MAX_LENGTH} characters")
    tokens: list[tuple[str, str]] = []
    position = 0
    while True:
        while position < len(text) and text[position] in " \t\r\n":
            position += 1
        if position == len(text):
            return tokens
        match = _TOKEN.match(text, position)
        if match is None or match.lastgroup is None:
            raise ExpressionError(f"unexpected character {text[position]!r}")
        tokens.append((match.lastgroup, match.group()))
        position = match.end()


class _Parser:
    """Parses and evaluates in one pass, binding as C does: unary, *, +, <<, then |."""

    def __init__(
        self,
        text: str,
        types: _Types,
        lookup: Callable[[str], _Value],
        notes: list[str],
    ) -> None:
        self.tokens = _tokenize(text)
        self.index = 0
        self.depth = 0
        self.types = types
        self.lookup = lookup
        self.notes = notes

    def parse(self) -> _Value:
        if not self.tokens:
            raise ExpressionError("empty expression")
        value = self.bitwise_or()
        if self.index < len(self.tokens):
            self.unexpected()
        return value

    def peek(self, offset: int = 0) -> tuple[str, str] | None:
        index = self.index + offset
        return self.tokens[index] if index < len(self.tokens) else None

    def accept(self, operator: str) -> bool:
        if self.peek() == ("operator", operator):
            self.index += 1
            return True
        return False

    def unexpected(self) -> NoReturn:
        token = self.peek()
        if token is None:
            raise ExpressionError("expression ends early")
        kind, text = token
        if kind == "operator" and text not in "()":
            raise ExpressionError(f"{text!r} is not supported here")
        raise ExpressionError(f"unexpected {text!r}")

    def bitwise_or(self) -> _Value:
        value = self.shift()
        while self.accept("|"):
            value = _arithmetic("|", value, self.shift())
        return value

    def shift(self) -> _Value:
        value = self.add()
        while self.accept("<<"):
            value = _shift_left(value, self.add())
        return value

    def add(self) -> _Value:
        value = self.multiply()
        while self.accept("+"):
            value = _arithmetic("+", value, self.multiply())
        return value

    def multiply(self) -> _Value:
        value = self.unary()
        while self.accept("*"):
            value = _arithmetic("*", value, self.unary())
        return value

    def unary(self) -> _Value:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise ExpressionError(f"nested more than {MAX_DEPTH} levels deep")
        try:
            if self.accept("-"):
                return _negate(self.unary())
            if self.accept("~"):
                return _complement(self.unary())
            target = self.cast()
            if target is not None:
                return _convert(self.unary(), target, self.notes)
            return self.primary()
        finally:
            self.depth -= 1

    def cast(self) -> _Type | None:
        """The target of a cast such as ``( uint32 )`` starting here, consumed; else None."""
        first = self.peek(1)
        if self.peek() != ("operator", "(") or first is None or first[0] != "name":
            return None
        if not self.types.is_type(first[1]):
            return None
        self.index += 1
        words = []
        while (token := self.peek()) is not None and token[0] == "name":
            words.append(token[1])
            self.index += 1
        if not self.accept(")"):
            raise ExpressionError("only casts to arithmetic types are supported")
        spelling = " ".join(words)
        return self.types.resolve(ir.CType(spelling, base=spelling))

    def primary(self) -> _Value:
        token = self.peek()
        if token is None:
            self.unexpected()
        kind, text = token
        if kind == "operator" and text != "(":
            self.unexpected()
        self.index += 1
        if kind == "integer":
            return _integer_literal(text)
        if kind == "float":
            return _float_literal(text)
        if kind == "char":
            return _char_literal(text)
        if kind == "name":
            return self.lookup(text)
        value = self.bitwise_or()
        if not self.accept(")"):
            self.unexpected()
        return value


def _integer_literal(text: str) -> _Value:
    parts = _LITERAL_PARTS.fullmatch(text)
    assert parts is not None
    digits, suffix = parts.groups()
    if digits[:2] in ("0x", "0X"):
        number, decimal = int(digits, 16), False
    elif len(digits) > 1 and digits[0] == "0":
        if not set(digits) <= set("01234567"):
            raise ExpressionError(f"{text} is not a valid octal literal")
        number, decimal = int(digits, 8), False
    else:
        number, decimal = int(digits), True
    lower = suffix.lower()
    if lower in ("l", "ul", "lu"):
        raise ExpressionError(f"{text} has type long, which is not portable: {_LONG}")
    if lower not in ("", "u", "ll", "ull", "llu") or "lL" in suffix or "Ll" in suffix:
        raise ExpressionError(f"{text} has an invalid suffix")
    # C++ gives a literal the first of these types that holds its value. long is left out:
    # wherever it would be chosen, long long has the same width and signedness everywhere.
    if "u" in lower:
        candidates = [_ULLONG] if "ll" in lower else [_UINT, _ULLONG]
    elif "ll" in lower:
        candidates = [_LLONG] if decimal else [_LLONG, _ULLONG]
    else:
        candidates = [_INT, _LLONG] if decimal else [_INT, _UINT, _LLONG, _ULLONG]
    for candidate in candidates:
        if number <= candidate.high:
            return _Value(number, candidate)
    raise ExpressionError(f"{text} is too large for any integer type")


def _float_literal(text: str) -> _Value:
    parts = _FLOAT_PARTS.fullmatch(text)
    assert parts is not None
    mantissa, exponent, suffix = parts.groups()
    if suffix in ("l", "L"):
        raise ExpressionError(f"{text} has type long double, which is not supported")
    if suffix not in ("", "f", "F"):
        raise ExpressionError(f"{text} has an invalid suffix")
    # Checked before computing 10 ** exponent, which could otherwise take very long.
    if exponent and abs(int(exponent)) > 400:
        raise ExpressionError(f"{text} is out of range")
    target = _TYPES["float" if suffix else "double"]
    exact = Fraction(mantissa) * Fraction(10) ** int(exponent or 0)
    return _Value(_exact_float(exact, target, text), target)


def _char_literal(text: str) -> _Value:
    if len(text) != 3 or text[-1] != "'" or not " " <= text[1] <= "~" or text[1] in "'\\":
        raise ExpressionError(f"{text} is not one printable ASCII character")
    # The literal's type is char, whose signedness varies, but a printable ASCII character has
    # the same value either way, and every operator promotes it to int.
    return _Value(ord(text[1]), _INT)


def _exact_float(value: Fraction, target: _Type, text: str) -> float:
    try:
        number = float(value)
        if target.bits == 32:
            number = struct.unpack("<f", struct.pack("<f", number))[0]
    except OverflowError:
        raise ExpressionError(f"{text} is out of range for {target.name}") from None
    if Fraction(number) != value:
        raise ExpressionError(f"{text} is not exactly representable as {target.name}")
    return number


def _promoted(value: _Value) -> tuple[int, _Type]:
    """Integer promotion: types narrower than int become int, which holds all their values."""
    if isinstance(value.number, float):
        raise ExpressionError("floating-point arithmetic is not supported")
    return value.number, _INT if value.type.bits < _INT.bits else value.type


def _common(left: _Type, right: _Type) -> _Type:
    """C's usual arithmetic conversions, where rank follows width once long is left out."""
    if left.signed == right.signed:
        return left if left.bits >= right.bits else right
    unsigned, signed = (right, left) if left.signed else (left, right)
    return unsigned if unsigned.bits >= signed.bits else signed


def _wrap(number: int, target: _Type) -> int:
    number %= 1 << target.bits
    return number - (1 << target.bits) if number > target.high else number


def _checked(number: int, target: _Type, operation: str) -> _Value:
    if target.signed and not target.low <= number <= target.high:
        raise ExpressionError(f"{operation} overflows {target.name}, which C leaves undefined")
    return _Value(_wrap(number, target), target)


def _arithmetic(operator: str, left: _Value, right: _Value) -> _Value:
    a, left_type = _promoted(left)
    b, right_type = _promoted(right)
    target = _common(left_type, right_type)
    a, b = _wrap(a, target), _wrap(b, target)
    if operator == "|":
        return _Value(a | b, target)
    return _checked(a + b if operator == "+" else a * b, target, f"{a} {operator} {b}")


def _shift_left(left: _Value, right: _Value) -> _Value:
    number, target = _promoted(left)
    count, _ = _promoted(right)
    if not 0 <= count < target.bits:
        raise ExpressionError(f"shifting {target.name} by {count} is undefined")
    if number < 0:
        raise ExpressionError(f"shifting negative {number} left is undefined")
    return _checked(number << count, target, f"{number} << {count}")


def _negate(value: _Value) -> _Value:
    number, target = _promoted(value)
    return _checked(-number, target, f"-{number}")


def _complement(value: _Value) -> _Value:
    number, target = _promoted(value)
    return _Value(_wrap(~number, target), target)


def _convert(value: _Value, target: _Type, notes: list[str]) -> _Value:
    if target.floating:
        if not isinstance(value.number, float):
            raise ExpressionError(f"converting an integer to {target.name} is not supported")
        return _Value(_exact_float(Fraction(value.number), target, repr(value.number)), target)
    if isinstance(value.number, float):
        raise ExpressionError(f"converting floating-point to {target.name} is not supported")
    result = _wrap(value.number, target)
    if result != value.number and target.signed:
        notes.append(
            f"{value.number} does not fit {target.name} and wraps to {result}, "
            "as C++20 requires and GCC, Clang and MSVC do"
        )
    return _Value(result, target)
