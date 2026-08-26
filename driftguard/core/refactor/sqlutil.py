"""Token-level structural helpers for refactor rules.

Rules read `Stage.raw` and derive precise byte spans via the tokenizer
(char offsets -> UTF-8 byte offsets through `byte_prefix`). All helpers are
pure and deterministic; "if a precondition cannot be proven, the rule does
not fire" (ARCHITECTURE §4.4).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from driftguard.core.parser.tokenizer import Token, byte_prefix, tokenize

_RESERVED = frozenset("""
select from where group by having order limit offset union all distinct as
with recursive not null and or in like between is case when then else end
join left right inner full outer cross on using exists any some insert into
values update delete create table view index drop alter grant revoke begin
commit rollback set cast asc desc over partition window rows range current
date time timestamp interval true false unknown default primary key
references check constraint unique foreign if else for do return procedure
function database schema columns values coalesce count sum avg min max
returning materialized
""".split())


@dataclass(frozen=True)
class CteDef:
    name: str
    name_start: int      # byte offset of the CTE name token
    def_start: int       # byte offset of `AS (` (body start)
    def_end: int         # byte offset just past the closing `)`
    body_start: int      # byte offset just past `AS (`
    body_end: int        # byte offset of the matching closing `)`
    comma_end: int | None = None  # byte offset past a trailing `,` if any


def is_keyword(word: str) -> bool:
    return word.lower() in _RESERVED


def structure(text: str) -> tuple[list[Token], list[int]]:
    """(tokens, byte_prefix) for a stage's raw text."""
    return tokenize(text), byte_prefix(text)


def to_byte(tokens: list[Token], pref: list[int], char_idx: int) -> int:
    return pref[char_idx]


def cte_definitions(tokens: list[Token], pref: list[int]
                    ) -> tuple[list[CteDef], int]:
    """Locate every top-level CTE definition: `WITH name AS ( body ) [, ...]`.

    Returns (definitions in source order with byte spans, byte offset of the
    `WITH` keyword). Bodies are tracked by paren depth; a CTE whose body
    cannot be closed is skipped (the rule must not fire on unprovable
    structure).
    """
    defs: list[CteDef] = []
    i = 0
    n = len(tokens)
    with_start = 0
    while i < n:
        if tokens[i].kind == "WORD" and tokens[i].value.upper() == "WITH":
            with_start = pref[tokens[i].start]
            break
        i += 1
    if i >= n:
        return defs, with_start
    i += 1
    if i < n and tokens[i].kind == "WORD" and tokens[i].value.upper() == "RECURSIVE":
        i += 1
    while i < n:
        t = tokens[i]
        if t.kind not in ("WORD", "QUOTED_IDENT"):
            return defs, with_start
        name = t.value[1:-1] if t.kind == "QUOTED_IDENT" else t.value
        name_start = pref[t.start]
        i += 1
        while i < n and tokens[i].value.upper() != "AS":
            i += 1
        if i >= n:
            return defs, with_start
        i += 1
        # optional NOT MATERIALIZED between AS and (
        while i < n and tokens[i].value.upper() in ("NOT", "MATERIALIZED"):
            i += 1
        if i >= n or tokens[i].value != "(":
            return defs, with_start
        def_start = pref[tokens[i].start]
        body_start = pref[tokens[i].end]
        depth = 0
        j = i
        while j < n:
            if tokens[j].value == "(":
                depth += 1
            elif tokens[j].value == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if j >= n:
            return defs, with_start
        body_end = pref[tokens[j].start]
        def_end = pref[tokens[j].end]
        comma_end = None
        if j + 1 < n and tokens[j + 1].value == ",":
            comma_end = pref[tokens[j + 1].end]
        defs.append(CteDef(name=name, name_start=name_start,
                           def_start=def_start, def_end=def_end,
                           body_start=body_start, body_end=body_end,
                           comma_end=comma_end))
        i = j + 1
        if i < n and tokens[i].value == ",":
            i += 1
            continue
        return defs, with_start
    return defs, with_start


def name_references(tokens: list[Token], pref: list[int],
                    name: str, exclude: tuple[int, int] | None = None
                    ) -> list[tuple[int, int]]:
    """Byte spans of every unquoted token equal to `name` (case-insensitive).

    `exclude` = (start, end) region to skip (e.g. the CTE's own body when
    checking recursion).
    """
    out = []
    for t in tokens:
        if t.kind != "WORD" or t.value.lower() != name.lower():
            continue
        start, end = pref[t.start], pref[t.end]
        if exclude and start >= exclude[0] and end <= exclude[1]:
            continue
        out.append((start, end))
    return out


def projection_items(tokens: list[Token], pref: list[int],
                     text: str) -> list[tuple[int, int, str]]:
    """Top-level SELECT projection items as (byte_start, byte_end, text).

    Starts at the first depth-0 SELECT; splits at depth-0 commas; stops at
    FROM/WHERE/GROUP/HAVING/ORDER/LIMIT/UNION or the end of a subquery.
    """
    items: list[tuple[int, int, str]] = []
    i, n = 0, len(tokens)
    depth = 0
    while i < n:
        t = tokens[i]
        if t.kind == "OP" and t.value == "(":
            depth += 1
        elif t.kind == "OP" and t.value == ")":
            if depth == 0:
                return items
            depth -= 1
        elif t.kind == "WORD" and depth == 0 and t.value.upper() == "SELECT":
            i += 1
            break
        i += 1
    start = None
    while i < n:
        t = tokens[i]
        v = t.value
        if t.kind == "OP" and v == "(":
            depth += 1
        elif t.kind == "OP" and v == ")":
            if depth == 0:
                break
            depth -= 1
        elif t.kind == "OP" and v == "," and depth == 0:
            if start is not None:
                items.append((start, pref[t.start], text[start:pref[t.start]].rstrip()))
            start = None
            i += 1
            continue
        elif depth == 0 and t.kind == "WORD" and v.upper() in (
                "FROM", "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "UNION"):
            if start is not None:
                items.append((start, pref[t.start], text[start:pref[t.start]].rstrip()))
            return items
        if start is None:
            start = pref[t.start]
        i += 1
    if start is not None:
        end = len(text)
        items.append((start, end, text[start:end].rstrip()))
    return items


def normalize_expr(expr: str) -> str:
    """Canonical form for duplicate detection: lowercase, single spaces."""
    return re.sub(r"\s+", " ", expr).strip().lower()


def from_items(tokens: list[Token], pref: list[int],
               text: str) -> list[tuple[int, int, str]]:
    """Depth-0 FROM/JOIN table items as (byte_start, byte_end, text).

    CTE bodies live inside parens, so they are naturally excluded. Stops at
    WHERE/GROUP/HAVING/ORDER/LIMIT/UNION/SET or a closing paren.
    """
    items: list[tuple[int, int, str]] = []
    depth = 0
    scanning = False
    cur_start = None
    for i, t in enumerate(tokens):
        v = t.value
        if t.kind == "OP" and v in ("(", "["):
            depth += 1
            continue
        if t.kind == "OP" and v in (")", "]"):
            if depth == 0:
                if cur_start is not None:
                    items.append((cur_start, pref[t.start],
                                  text[cur_start:pref[t.start]].rstrip()))
                return items
            depth -= 1
            continue
        if depth == 0 and t.kind == "WORD":
            up = v.upper()
            if up == "FROM":
                scanning = True
                continue
            if up in ("WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "UNION",
                      "SET", "ON"):
                if cur_start is not None:
                    items.append((cur_start, pref[t.start],
                                  text[cur_start:pref[t.start]].rstrip()))
                return items
            if up == "JOIN":
                if cur_start is not None:
                    items.append((cur_start, pref[t.start],
                                  text[cur_start:pref[t.start]].rstrip()))
                cur_start = None
                continue
        if not scanning:
            continue
        if depth == 0 and t.kind == "OP" and v == ",":
            if cur_start is not None:
                items.append((cur_start, pref[t.start],
                              text[cur_start:pref[t.start]].rstrip()))
            cur_start = None
            continue
        if cur_start is None:
            cur_start = pref[t.start]
    if cur_start is not None:
        items.append((cur_start, len(text), text[cur_start:].rstrip()))
    return items


def item_alias(item_text: str) -> str | None:
    """Alias of a FROM item: `orders o` -> `o`, `orders AS o` -> `o`.

    Conservative: qualified names (`schema.orders o`) or multi-word items
    without `AS` never yield an alias (precondition unproven -> no fire).
    """
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_$]*", item_text)
    if not words:
        return None
    if words[0].upper() == "AS":
        return None
    if len(words) == 2 and not is_keyword(words[1]):
        return words[1]
    if "AS" in [w.upper() for w in words]:
        idx = [w.upper() for w in words].index("AS")
        if idx + 1 < len(words) and not is_keyword(words[idx + 1]):
            return words[idx + 1]
    return None


def from_target(item_text: str) -> str | None:
    """Resolve a FROM item to its table/stage name.

    `{{ ref('orders') }}` -> `orders`; `orders`/`schema.orders` -> last
    identifier; quoted/backticked identifiers are unquoted.
    """
    m = re.search(r"ref\(\s*['\"]([^'\"]+)['\"]", item_text, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.match(r"\s*[`\"']?([A-Za-z_][A-Za-z0-9_$]*)", item_text)
    if not m:
        return None
    return m.group(1)