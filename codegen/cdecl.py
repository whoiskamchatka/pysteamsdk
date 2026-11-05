"""Parse the C and C++ type spellings used by steam_api.json and steam_api_flat.h.

Only the forms the SDK actually uses are accepted: ``[const] base [*...] [&] [[N]]`` and
``returns (*)(types)``. Anything else raises TypeSyntaxError, so callers report the declaration
instead of guessing what it means.
"""

import re
from typing import NoReturn

from codegen.ir import CType

# Words that combine into one fundamental type, as in "unsigned long long".
FUNDAMENTAL_WORDS = frozenset({"signed", "unsigned", "short", "long", "int", "char"})
_NOT_NAMES = FUNDAMENTAL_WORDS | {"const", "void"}
_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
_TOKEN = re.compile(r"::|[A-Za-z_]\w*|\d+|[*&\[\](),]")


class TypeSyntaxError(ValueError):
    """A type spelling outside the grammar this tool understands."""


def parse_type(text: str) -> CType:
    """Parse a type with no declarator name, such as ``const char *``."""
    parser = _Parser(text)
    result = parser.type(0)
    parser.expect_end()
    return result


def parse_param(text: str) -> tuple[CType, str]:
    """Parse a parameter declaration such as ``const char * pchKey`` into type and name."""
    parser = _Parser(text)
    tokens = parser.tokens
    if len(tokens) < 2 or not _is_name(tokens[-1][0]):
        raise TypeSyntaxError(f"parameter has no name: {text.strip()!r}")
    name, start, _ = tokens[-1]
    return parse_type(text[:start]), name


def split_params(text: str) -> list[str]:
    """Split a parameter list at the commas outside parentheses."""
    parts, depth, start = [], 0, 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    tail = text[start:]
    if parts or tail.strip():
        parts.append(tail)
    return [part.strip() for part in parts]


def _is_name(token: str) -> bool:
    return bool(_IDENTIFIER.fullmatch(token)) and token not in _NOT_NAMES


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens: list[tuple[str, int, int]] = []
        position = 0
        while True:
            while position < len(text) and text[position].isspace():
                position += 1
            if position == len(text):
                break
            match = _TOKEN.match(text, position)
            if match is None:
                self.fail(f"unexpected character {text[position]!r}")
            self.tokens.append((match.group(), match.start(), match.end()))
            position = match.end()
        self.index = 0

    def fail(self, reason: str) -> NoReturn:
        raise TypeSyntaxError(f"{reason} in {self.text.strip()!r}")

    def peek(self) -> str | None:
        return self.tokens[self.index][0] if self.index < len(self.tokens) else None

    def take(self, expected: str | None = None) -> str:
        token = self.peek()
        if token is None or (expected is not None and token != expected):
            self.fail(f"expected {expected or 'more'}, found {token or 'the end'}")
        self.index += 1
        return token

    def expect_end(self) -> None:
        if self.peek() is not None:
            self.fail(f"unexpected {self.peek()!r}")

    def spelling(self, start: int, end: int) -> str:
        """The source text of tokens start..end-1, as written."""
        return self.text[self.tokens[start][1] : self.tokens[end - 1][2]]

    def type(self, start: int) -> CType:
        const = self.peek() == "const"
        if const:
            self.take()
        base = self.base()
        pointers = 0
        while self.peek() == "*":
            self.take()
            pointers += 1
        if self.peek() == "(":
            returns = CType(
                self.spelling(start, self.index), base=base, const=const, pointers=pointers
            )
            return self.function_pointer(start, returns)
        reference = self.peek() == "&"
        if reference:
            self.take()
        array = None
        if self.peek() == "[":
            self.take()
            size = self.take()
            if not size.isdigit():
                self.fail("array size is not a number")
            self.take("]")
            array = int(size)
        return CType(
            self.spelling(start, self.index),
            base=base,
            const=const,
            pointers=pointers,
            reference=reference,
            array=array,
        )

    def function_pointer(self, start: int, returns: CType) -> CType:
        for token in ("(", "*", ")", "("):
            self.take(token)
        params: list[CType] = []
        while self.peek() != ")":
            if params:
                self.take(",")
            params.append(self.type(self.index))
        self.take(")")
        return CType(
            self.spelling(start, self.index),
            kind="function_pointer",
            pointers=1,
            returns=returns,
            params=tuple(params),
        )

    def base(self) -> str:
        token = self.peek()
        if token is None or not _IDENTIFIER.fullmatch(token) or token == "const":
            self.fail(f"expected a type name, found {token or 'the end'}")
        if token in FUNDAMENTAL_WORDS:
            words = []
            while self.peek() in FUNDAMENTAL_WORDS:
                words.append(self.take())
            return " ".join(words)
        name = self.take()
        while self.peek() == "::":
            self.take()
            name += "::" + self.take()
        return name
