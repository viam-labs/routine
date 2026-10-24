"""A small expression language for ``when``, ``cancel_when``, and ``${...}``.

Never calls ``eval``. Supports dotted paths, list indexes, comparisons,
``and`` / ``or`` / ``not``, parentheses, ``if`` / ``then`` / ``else``,
``split(value, sep)``, and literals. A bare word that is not a scope name is a
string, so ``result.status == handed_over`` works.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence, Union


class ExprError(ValueError):
    pass


@dataclass(frozen=True)
class Literal:
    value: Any


@dataclass(frozen=True)
class Path:
    parts: tuple[Union[str, int], ...]


@dataclass(frozen=True)
class Unary:
    op: str
    value: "Node"


@dataclass(frozen=True)
class Binary:
    op: str
    left: "Node"
    right: "Node"


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple["Node", ...]


@dataclass(frozen=True)
class Index:
    value: "Node"
    index: int


@dataclass(frozen=True)
class If:
    condition: "Node"
    then_value: "Node"
    else_value: "Node"


Node = Union[Literal, Path, Unary, Binary, Call, Index, If]

_KEYWORDS = {"true": True, "false": False, "null": None}
_RESERVED = {"and", "or", "not", "if", "then", "else"}
_FUNCTIONS = {"split"}
_CMP = {"==", "!=", "<", "<=", ">", ">="}


def parse_expr(source: str) -> Node:
    parser = _Parser(_tokenize(source))
    node = parser.parse_if()
    if parser.peek() is not None:
        raise ExprError(f"unexpected {parser.peek()!r} in expression {source!r}")
    return node


def evaluate(node: Node, scope: dict[str, Any], *, strict_paths: bool = False) -> Any:
    if isinstance(node, Literal):
        return node.value
    if isinstance(node, Path):
        return _lookup_path(node.parts, scope, strict=strict_paths)
    if isinstance(node, Unary):
        return not _truthy(evaluate(node.value, scope, strict_paths=strict_paths))
    if isinstance(node, Call):
        return _call(node, scope, strict_paths=strict_paths)
    if isinstance(node, Index):
        return _index(
            evaluate(node.value, scope, strict_paths=strict_paths), node.index
        )
    if isinstance(node, If):
        if _truthy(evaluate(node.condition, scope, strict_paths=strict_paths)):
            return evaluate(node.then_value, scope, strict_paths=strict_paths)
        return evaluate(node.else_value, scope, strict_paths=strict_paths)
    left = evaluate(node.left, scope, strict_paths=strict_paths)
    right = evaluate(node.right, scope, strict_paths=strict_paths)
    if node.op == "and":
        return _truthy(left) and _truthy(right)
    if node.op == "or":
        return _truthy(left) or _truthy(right)
    if node.op == "==":
        return left == right
    if node.op == "!=":
        return left != right
    if node.op in _CMP:
        try:
            if node.op == "<":
                return left < right
            if node.op == "<=":
                return left <= right
            if node.op == ">":
                return left > right
            return left >= right
        except TypeError as exc:
            raise ExprError(f"cannot compare {left!r} {node.op} {right!r}") from exc
    raise ExprError(f"unknown operator {node.op}")


def is_true(node: Node, scope: dict[str, Any]) -> bool:
    return _truthy(evaluate(node, scope))


def _truthy(value: Any) -> bool:
    return bool(value)


def _call(node: Call, scope: dict[str, Any], *, strict_paths: bool) -> Any:
    if node.name == "split":
        if len(node.args) != 2:
            raise ExprError("split takes a value and a separator")
        value = evaluate(node.args[0], scope, strict_paths=strict_paths)
        sep = evaluate(node.args[1], scope, strict_paths=strict_paths)
        if not isinstance(value, str):
            raise ExprError(f"split value must be a string, got {value!r}")
        if not isinstance(sep, str):
            raise ExprError(f"split separator must be a string, got {sep!r}")
        return value.split(sep)
    raise ExprError(f"unknown function {node.name!r}")


def _index(value: Any, index: int) -> Any:
    if not isinstance(value, list) or index < 0 or index >= len(value):
        return None
    return value[index]


def _lookup_path(
    parts: Sequence[Union[str, int]], scope: dict[str, Any], *, strict: bool = False
) -> Any:
    first = parts[0]
    if not isinstance(first, str):
        raise ExprError("invalid path")
    if first not in scope:
        if strict:
            raise ExprError(f"{first!r} is not in scope")
        if len(parts) == 1:
            return first
        return None
    current: Any = scope[first]
    for part in parts[1:]:
        if isinstance(part, int):
            if not isinstance(current, list) or part < 0 or part >= len(current):
                return None
            current = current[part]
            continue
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def parse_path(source: str) -> tuple[Union[str, int], ...]:
    """Parse a dotted path such as ``order.items[0].id``."""
    parser = _Parser(_tokenize(source))
    parts = parser.parse_path()
    if parser.peek() is not None:
        raise ExprError(f"invalid path {source!r}")
    return tuple(parts)


@dataclass
class _Tok:
    kind: str
    value: Any


def _tokenize(source: str) -> list[_Tok]:
    tokens: list[_Tok] = []
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        if ch.isspace():
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            buf: list[str] = []
            while i < n and source[i] != quote:
                if source[i] == "\\" and i + 1 < n:
                    buf.append(source[i + 1])
                    i += 2
                    continue
                buf.append(source[i])
                i += 1
            if i >= n or source[i] != quote:
                raise ExprError(f"unterminated string in {source!r}")
            i += 1
            tokens.append(_Tok("string", "".join(buf)))
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and source[i + 1].isdigit()):
            start = i
            i += 1
            while i < n and (source[i].isdigit() or source[i] == "."):
                i += 1
            raw = source[start:i]
            try:
                value: Any = int(raw) if raw.isdigit() else float(raw)
            except ValueError as exc:
                raise ExprError(f"invalid number {raw!r}") from exc
            tokens.append(_Tok("number", value))
            continue
        if ch.isalpha() or ch == "_":
            start = i
            i += 1
            while i < n and (source[i].isalnum() or source[i] == "_"):
                i += 1
            word = source[start:i]
            tokens.append(_Tok("word", word))
            continue
        for op in ("==", "!=", "<=", ">="):
            if source.startswith(op, i):
                tokens.append(_Tok("op", op))
                i += len(op)
                break
        else:
            if ch in "<>()[].,":
                tokens.append(_Tok(ch, ch))
                i += 1
                continue
            raise ExprError(f"unexpected character {ch!r} in {source!r}")
    return tokens


class _Parser:
    def __init__(self, tokens: list[_Tok]) -> None:
        self.tokens = tokens
        self.i = 0

    def peek(self) -> Optional[_Tok]:
        if self.i >= len(self.tokens):
            return None
        return self.tokens[self.i]

    def pop(self) -> _Tok:
        tok = self.peek()
        if tok is None:
            raise ExprError("unexpected end of expression")
        self.i += 1
        return tok

    def parse_if(self) -> Node:
        if self._word("if"):
            condition = self.parse_or()
            if not self._word("then"):
                raise ExprError("expected 'then'")
            then_value = self.parse_if()
            if not self._word("else"):
                raise ExprError("expected 'else'")
            return If(condition, then_value, self.parse_if())
        return self.parse_or()

    def parse_or(self) -> Node:
        node = self.parse_and()
        while self._word("or"):
            node = Binary("or", node, self.parse_and())
        return node

    def parse_and(self) -> Node:
        node = self.parse_not()
        while self._word("and"):
            node = Binary("and", node, self.parse_not())
        return node

    def parse_not(self) -> Node:
        if self._word("not"):
            return Unary("not", self.parse_not())
        return self.parse_cmp()

    def parse_cmp(self) -> Node:
        node = self.parse_postfix()
        tok = self.peek()
        if tok is not None and (tok.kind == "op" or tok.kind in "<>"):
            self.pop()
            return Binary(str(tok.value), node, self.parse_postfix())
        return node

    def parse_postfix(self) -> Node:
        node = self.parse_primary()
        while True:
            tok = self.peek()
            if tok is None or tok.kind != "[":
                return node
            self.pop()
            idx = self.pop()
            if idx.kind != "number" or not isinstance(idx.value, int):
                raise ExprError("index must be an integer")
            close = self.pop()
            if close.kind != "]":
                raise ExprError("expected ']'")
            node = Index(node, idx.value)

    def parse_primary(self) -> Node:
        tok = self.peek()
        if tok is None:
            raise ExprError("unexpected end of expression")
        if tok.kind == "(":
            self.pop()
            node = self.parse_if()
            close = self.pop()
            if close.kind != ")":
                raise ExprError("expected ')'")
            return node
        if tok.kind == "number":
            self.pop()
            return Literal(tok.value)
        if tok.kind == "string":
            self.pop()
            return Literal(tok.value)
        if tok.kind == "word":
            if tok.value in _FUNCTIONS and self._next_is("("):
                return self.parse_call()
            if tok.value in _KEYWORDS and not self._path_continues():
                self.pop()
                return Literal(_KEYWORDS[tok.value])
            return Path(tuple(self.parse_path()))
        raise ExprError(f"unexpected {tok.value!r}")

    def parse_call(self) -> Node:
        name = self.pop()
        if name.kind != "word":
            raise ExprError("expected a function name")
        open_paren = self.pop()
        if open_paren.kind != "(":
            raise ExprError("expected '('")
        args: list[Node] = []
        if self.peek() is not None and self.peek().kind != ")":
            args.append(self.parse_if())
            while self.peek() is not None and self.peek().kind == ",":
                self.pop()
                args.append(self.parse_if())
        close = self.pop()
        if close.kind != ")":
            raise ExprError("expected ')'")
        return Call(str(name.value), tuple(args))

    def _path_continues(self) -> bool:
        if self.i + 1 >= len(self.tokens):
            return False
        nxt = self.tokens[self.i + 1]
        return nxt.kind in ".["

    def _next_is(self, kind: str) -> bool:
        if self.i + 1 >= len(self.tokens):
            return False
        return self.tokens[self.i + 1].kind == kind

    def parse_path(self) -> list[Union[str, int]]:
        tok = self.pop()
        if tok.kind != "word":
            raise ExprError(f"expected a name, got {tok.value!r}")
        if tok.value in _RESERVED:
            raise ExprError(f"unexpected {tok.value!r}")
        parts: list[Union[str, int]] = [tok.value]
        while True:
            nxt = self.peek()
            if nxt is None:
                break
            if nxt.kind == ".":
                self.pop()
                field = self.pop()
                if field.kind != "word":
                    raise ExprError("expected a field name after '.'")
                parts.append(str(field.value))
                continue
            if nxt.kind == "[":
                self.pop()
                idx = self.pop()
                if idx.kind != "number" or not isinstance(idx.value, int):
                    raise ExprError("index must be an integer")
                close = self.pop()
                if close.kind != "]":
                    raise ExprError("expected ']'")
                parts.append(idx.value)
                continue
            break
        return parts

    def _word(self, word: str) -> bool:
        tok = self.peek()
        if tok is not None and tok.kind == "word" and tok.value == word:
            self.pop()
            return True
        return False
