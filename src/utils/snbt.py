"""Small structured SNBT parser used for Minecraft RCON responses."""

from __future__ import annotations

import re
from typing import Any


class SnbtParseError(ValueError):
    """Raised when a textual SNBT value cannot be parsed."""


_INTEGER_RE = re.compile(r"^[-+]?\d+[bBsSlL]?$")
_FLOAT_RE = re.compile(
    r"^[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?[fFdD]?$"
)


class _Parser:
    def __init__(self, text: str, start: int = 0) -> None:
        self.text = text
        self.index = start

    def parse(self) -> Any:
        self._space()
        if self.index >= len(self.text):
            raise SnbtParseError("SNBT 为空")
        marker = self.text[self.index]
        if marker == "[":
            return self._list()
        if marker == "{":
            return self._compound()
        if marker in "\"'":
            return self._string()
        return self._bare()

    def _list(self) -> list[Any]:
        self.index += 1
        values: list[Any] = []
        self._space()
        if self._accept("]"):
            return values
        if (
            self.index + 1 < len(self.text)
            and self.text[self.index] in "BILbil"
            and self.text[self.index + 1] == ";"
        ):
            self.index += 2
            self._space()
            if self._accept("]"):
                return values
        while True:
            values.append(self.parse())
            self._space()
            if self._accept("]"):
                return values
            self._expect(",")

    def _compound(self) -> dict[str, Any]:
        self.index += 1
        values: dict[str, Any] = {}
        self._space()
        if self._accept("}"):
            return values
        while True:
            self._space()
            key_value = self._string() if self._peek() in "\"'" else self._bare(raw=True)
            key = str(key_value)
            self._space()
            self._expect(":")
            values[key] = self.parse()
            self._space()
            if self._accept("}"):
                return values
            self._expect(",")

    def _string(self) -> str:
        quote = self.text[self.index]
        self.index += 1
        chars: list[str] = []
        while self.index < len(self.text):
            char = self.text[self.index]
            self.index += 1
            if char == quote:
                return "".join(chars)
            if char == "\\":
                if self.index >= len(self.text):
                    raise SnbtParseError("字符串转义不完整")
                chars.append(self.text[self.index])
                self.index += 1
            else:
                chars.append(char)
        raise SnbtParseError("字符串未闭合")

    def _bare(self, *, raw: bool = False) -> Any:
        self._space()
        start = self.index
        while self.index < len(self.text) and self.text[self.index] not in ",:{}[] \t\r\n":
            self.index += 1
        token = self.text[start:self.index]
        if not token:
            raise SnbtParseError(f"位置 {self.index} 缺少值")
        if raw:
            return token
        if _INTEGER_RE.fullmatch(token):
            return int(token.rstrip("bBsSlL"))
        if _FLOAT_RE.fullmatch(token):
            return float(token.rstrip("fFdD"))
        if token == "true":
            return True
        if token == "false":
            return False
        return token

    def _space(self) -> None:
        while self.index < len(self.text) and self.text[self.index].isspace():
            self.index += 1

    def _peek(self) -> str:
        self._space()
        return self.text[self.index] if self.index < len(self.text) else ""

    def _accept(self, marker: str) -> bool:
        self._space()
        if self.text.startswith(marker, self.index):
            self.index += len(marker)
            return True
        return False

    def _expect(self, marker: str) -> None:
        if not self._accept(marker):
            raise SnbtParseError(f"位置 {self.index} 预期 {marker!r}")


def parse_snbt(value: str) -> Any:
    parser = _Parser(str(value))
    result = parser.parse()
    parser._space()
    if parser.index != len(parser.text):
        raise SnbtParseError(f"位置 {parser.index} 存在多余内容")
    return result


def parse_first_snbt_list(response: Any) -> list[Any]:
    return _parse_first(response, "[", list, "列表")


def parse_first_snbt_compound(response: Any) -> dict[str, Any]:
    return _parse_first(response, "{", dict, "compound")


def _parse_first(response: Any, marker: str, expected: type, label: str) -> Any:
    text = str(response or "")
    for index, char in enumerate(text):
        if char != marker:
            continue
        try:
            value = _Parser(text, index).parse()
        except SnbtParseError:
            continue
        if isinstance(value, expected):
            return value
    raise SnbtParseError(f"RCON 响应中没有合法 SNBT {label}: {text!r}")


__all__ = [
    "SnbtParseError",
    "parse_snbt",
    "parse_first_snbt_list",
    "parse_first_snbt_compound",
]
