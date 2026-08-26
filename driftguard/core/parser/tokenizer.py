"""SQL tokenizer preserving char offsets (the sourcemap base).

Token kinds:
- WORD          bare identifier / keyword
- STRING        single-quoted literal ('...', with '' escape)
- QUOTED_IDENT  "..." or `...` (identifier quoting)
- NUMBER        numeric literal
- OP            punctuation / operators (incl. multi-char <= >= <> != || :: etc.)
- TEMPLATE_*    Jinja markers: {{ }}, {% %}, {# #} (dbt static subset)

The tokenizer is the sourcemap base for rewrites: every token carries its
start/end as character offsets (plus line/col for diagnostics). Byte offsets
are derived by the parser via a prefix map (`byte_prefix`), so `Span` objects
in the IR are byte offsets per ARCHITECTURE.md §4.2.

Fails loud: an unterminated string/comment raises `TokenizerError` — never
produces a silently-wrong token stream.
"""
from __future__ import annotations

from dataclasses import dataclass

TEMPLATE_OPEN = "TEMPLATE_OPEN"              # {{
TEMPLATE_CLOSE = "TEMPLATE_CLOSE"            # }}
TEMPLATE_TAG_OPEN = "TEMPLATE_TAG_OPEN"      # {%
TEMPLATE_TAG_CLOSE = "TEMPLATE_TAG_CLOSE"    # %}
TEMPLATE_COMMENT_OPEN = "TEMPLATE_COMMENT_OPEN"  # {#
TEMPLATE_COMMENT_CLOSE = "TEMPLATE_COMMENT_CLOSE"  # #}

_TEMPLATES = (
    ("{{", TEMPLATE_OPEN),
    ("}}", TEMPLATE_CLOSE),
    ("{%", TEMPLATE_TAG_OPEN),
    ("%}", TEMPLATE_TAG_CLOSE),
    ("{#", TEMPLATE_COMMENT_OPEN),
    ("#}", TEMPLATE_COMMENT_CLOSE),
)

_OPERATORS = ("<=", ">=", "<>", "!=", "||", "::", "->>", "->", ":=", "==")
_OP_CHARS = set("()[],.;:+-*/%=<>@?!~^&|")


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    start: int            # char offset
    end: int              # char offset (exclusive)
    line: int
    col: int


class TokenizerError(Exception):
    def __init__(self, line: int, col: int, reason: str):
        super().__init__(f"{reason} at line {line}, col {col}")
        self.line = line
        self.col = col
        self.reason = reason


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    i, n = 0, len(text)
    line, col = 1, 1

    def advance(k: int = 1) -> None:
        nonlocal i, col
        i += k
        col += k

    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            if ch == "\n":
                line += 1
                col = 1
            else:
                col += 1
            i += 1
            continue

        start, sline, scol = i, line, col

        if text.startswith("--", i):
            while i < n and text[i] != "\n":
                i += 1
                col += 1
            continue

        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end == -1:
                raise TokenizerError(sline, scol, "unterminated block comment")
            newlines = text.count("\n", i, end)
            last_nl = text.rfind("\n", i, end)
            i = end + 2
            if newlines:
                line += newlines
                col = (end + 2) - last_nl
            else:
                col += (end + 2) - start
            continue

        matched_template = False
        for marker, kind in _TEMPLATES:
            if text.startswith(marker, i):
                advance(len(marker))
                tokens.append(Token(kind, marker, start, i, sline, scol))
                matched_template = True
                break
        if matched_template:
            continue

        if ch == "'":
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    break
                if text[j] == "\n":
                    raise TokenizerError(sline, scol, "unterminated string literal")
                j += 1
            if j >= n:
                raise TokenizerError(sline, scol, "unterminated string literal")
            val = text[i : j + 1]
            for k in range(len(val)):
                if val[k] == "\n":
                    line += 1
                    col = 1
                else:
                    col += 1
            i = j + 1
            tokens.append(Token("STRING", val, start, i, sline, scol))
            continue

        if ch in "\"`":
            q = ch
            j = i + 1
            while j < n and text[j] != q:
                if text[j] == "\n":
                    raise TokenizerError(sline, scol,
                                         "unterminated quoted identifier")
                j += 1
            if j >= n:
                raise TokenizerError(sline, scol,
                                     "unterminated quoted identifier")
            val = text[i : j + 1]
            advance(len(val))
            tokens.append(Token("QUOTED_IDENT", val, start, i, sline, scol))
            continue

        if ch.isdigit():
            j = i + 1
            while j < n and (text[j].isdigit() or text[j] in "._"):
                j += 1
            val = text[i:j]
            advance(len(val))
            tokens.append(Token("NUMBER", val, start, i, sline, scol))
            continue

        if ch.isalpha() or ch == "_":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] in "_$"):
                j += 1
            val = text[i:j]
            advance(len(val))
            tokens.append(Token("WORD", val, start, i, sline, scol))
            continue

        op2 = text[i : i + 2]
        if op2 in _OPERATORS:
            advance(2)
            tokens.append(Token("OP", op2, start, i, sline, scol))
            continue

        if ch in _OP_CHARS:
            advance(1)
            tokens.append(Token("OP", ch, start, i, sline, scol))
            continue

        raise TokenizerError(sline, scol, f"unexpected character {ch!r}")

    tokens.append(Token("EOF", "", n, n, line, col))
    return tokens


def byte_prefix(text: str) -> list[int]:
    """prefix[i] = number of UTF-8 bytes before char offset i."""
    pref = [0] * (len(text) + 1)
    total = 0
    for i, ch in enumerate(text):
        total += len(ch.encode("utf-8"))
        pref[i + 1] = total
    return pref


def unquote(value: str) -> str:
    """Strip ' \" ` quoting from a token value."""
    if len(value) >= 2 and value[0] in ("'", '"', "`") and value[-1] == value[0]:
        return value[1:-1]
    return value