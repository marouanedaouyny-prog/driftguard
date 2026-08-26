"""Recursive-descent SQL parser over the token stream (ARCHITECTURE §4.1).

Grammar subset (documented, fail-loud):

- SELECT [ALL|DISTINCT] projection
  projection: expr [AS alias] {, expr}  (exprs tracked with paren depth)
- FROM/JOIN clauses: consumed structurally (refs are collected in a separate
  full-stream walk; subqueries are balanced-paren opaque regions)
- WITH [RECURSIVE] cte AS ( query ) {, ...}
- UNION [ALL|DISTINCT] select-core
- CREATE [OR REPLACE] [TEMP|TEMPORARY] TABLE|VIEW|MATERIALIZED VIEW [schema.]name AS query
- top-level INSERT INTO ... SELECT query

Unknown constructs never produce a guessed IR: they either stop a clause
(with the rest consumed conservatively) or surface as a structured
`Diagnostic`. The output columns of a stage come from the projection of the
main (final) SELECT — the same contract the seed's regex parser documented,
now with real structure and spans.
"""
from __future__ import annotations

from driftguard.core.ir.model import Column, Cte, Diagnostic, Span
from driftguard.core.parser.tokenizer import (
    TEMPLATE_CLOSE,
    TEMPLATE_OPEN,
    TEMPLATE_TAG_CLOSE,
    TEMPLATE_TAG_OPEN,
    Token,
    TokenizerError,
    byte_prefix,
    tokenize,
    unquote,
)

# Keywords that end a SELECT projection / a select-core (at paren depth 0).
CLAUSE_END = {
    "FROM", "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET", "FETCH",
    "QUALIFY", "WINDOW", "UNION", "PIVOT", "UNPIVOT", "INTO", "RETURNING",
    "FOR",
}

_TEMPLATE_SKIP = (TEMPLATE_OPEN, TEMPLATE_TAG_OPEN)
_TEMPLATE_END = (TEMPLATE_CLOSE, TEMPLATE_TAG_CLOSE)


def _is_star_segment(seg: list[Token]) -> bool:
    """True for `*` or `qual.*` projection segments (expanded at runtime to
    unknown columns — the drift contract cannot be asserted)."""
    if len(seg) == 1 and seg[0].kind == "OP" and seg[0].value == "*":
        return True
    return (len(seg) >= 3 and seg[-1].kind == "OP" and seg[-1].value == "*"
            and seg[-2].kind == "OP" and seg[-2].value == ".")


class SQLParser:
    """Parses one .sql file's token stream into IR shape.

    Raises TokenizerError only when the tokenizer itself fails; structural
    unknowns are diagnostics, never exceptions.
    """

    def __init__(self, raw: str, file: str = ""):
        self.raw = raw
        self.file = file
        self.pref = byte_prefix(raw)
        self.tokens: list[Token] = []
        try:
            self.tokens = tokenize(raw)
        except TokenizerError as exc:
            self.tokenizer_error = exc
        else:
            self.tokenizer_error = None

    # ---- token-level helpers -------------------------------------------------

    def _span(self, char_start: int, char_end: int) -> Span:
        return Span(self.pref[char_start], self.pref[char_end])

    def _token_span(self, t: Token) -> Span:
        return self._span(t.start, t.end)

    def _line_col(self, t: Token) -> tuple[int, int]:
        return t.line, t.col

    # ---- top level -----------------------------------------------------------

    def parse(self) -> tuple[str | None, list[Cte], list[Column], list[Diagnostic]]:
        """Returns (create_name, ctes, columns, diagnostics)."""
        diags: list[Diagnostic] = []
        if self.tokenizer_error is not None:
            err = self.tokenizer_error
            diags.append(Diagnostic(self.file, err.line, err.col, err.reason,
                                    kind="error"))
            return None, [], [], diags

        main_idx = _find_main_select(self.tokens)
        if main_idx is None:
            t = _first_meaningful(self.tokens)
            if t is not None and t.kind != "EOF":
                diags.append(self._warning(t, "no top-level SELECT found"))
            return None, [], [], diags

        create_name = _find_create_name(self.tokens, main_idx)

        cursor = _Cursor(self.tokens, main_idx)
        ctes, columns = self._parse_query(cursor, diags, warn_star=True)
        return create_name, ctes, columns, diags

    def _warning(self, t: Token, reason: str) -> Diagnostic:
        line, col = self._line_col(t)
        return Diagnostic(self.file, line, col, reason, kind="warning")

    # ---- query structure ------------------------------------------------------

    def _parse_query(self, cursor: "_Cursor", diags: list[Diagnostic],
                     warn_star: bool = False) -> tuple[list[Cte], list[Column]]:
        ctes: list[Cte] = []
        if cursor.accept_keyword("WITH"):
            cursor.accept_keyword("RECURSIVE")
            while True:
                t = cursor.peek()
                if t.kind in ("WORD", "QUOTED_IDENT"):
                    name = unquote(t.value).lower()
                    cursor.next()
                else:
                    break
                cursor.accept_keyword("AS")
                cursor.accept_keyword("NOT")
                cursor.accept_keyword("MATERIALIZED")
                if cursor.accept_op("("):
                    start_tok = cursor.peek()
                    inner_ctes, _ = self._parse_query(cursor, diags)
                    ctes.extend(inner_ctes)
                    if cursor.accept_op(")"):
                        ctes.append(Cte(name=name,
                                        span=self._span(start_tok.start,
                                                        start_tok.start)))
                    else:
                        ctes.append(Cte(name=name,
                                        span=self._span(start_tok.start,
                                                        start_tok.start)))
                else:
                    self._skip_opaque_cte(cursor)
                if not cursor.accept_op(","):
                    break

        columns = self._parse_select_core(cursor, diags, warn_star=warn_star)
        while cursor.accept_keyword("UNION"):
            cursor.accept_keyword("ALL")
            cursor.accept_keyword("DISTINCT")
            self._parse_select_core(cursor, diags)
        return ctes, columns

    def _skip_opaque_cte(self, cursor: "_Cursor") -> None:
        depth = 0
        while True:
            t = cursor.peek()
            if t.kind == "EOF":
                return
            if t.kind == "OP" and t.value in ("(", "["):
                depth += 1
            elif t.kind == "OP" and t.value in (")", "]"):
                if depth == 0:
                    return
                depth -= 1
            elif t.kind == "OP" and t.value == "," and depth == 0:
                return
            elif t.kind == "WORD" and depth == 0 and t.value.upper() == "SELECT":
                return
            cursor.next()

    def _parse_select_core(self, cursor: "_Cursor",
                           diags: list[Diagnostic],
                           warn_star: bool = False) -> list[Column]:
        if not cursor.accept_keyword("SELECT"):
            t = cursor.peek()
            if t.kind != "EOF":
                diags.append(self._warning(t, "expected SELECT"))
            return []
        cursor.accept_keyword("ALL")
        cursor.accept_keyword("DISTINCT")
        segments = self._parse_projection(cursor)
        if warn_star:
            for seg in segments:
                if _is_star_segment(seg):
                    diags.append(self._warning(
                        seg[0],
                        "SELECT * expands to unknown columns; drift "
                        "assertions are skipped for this stage — pin an "
                        "explicit projection for schema-drift safety"))
                    break
        self._consume_rest(cursor)
        return [c for c in (self._extract_column(seg) for seg in segments)
                if c is not None]

    def _parse_projection(self, cursor: "_Cursor") -> list[list[Token]]:
        segments: list[list[Token]] = []
        current: list[Token] = []
        depth = 0
        while True:
            t = cursor.peek()
            if t.kind == "EOF":
                break
            if t.kind in _TEMPLATE_SKIP:
                cursor.skip_template()
                continue
            if t.kind in _TEMPLATE_END:
                cursor.next()
                continue
            if t.kind == "OP" and t.value in ("(", "["):
                depth += 1
                current.append(cursor.next())
                continue
            if t.kind == "OP" and t.value in (")", "]"):
                if depth == 0:
                    break
                depth -= 1
                current.append(cursor.next())
                continue
            if t.kind == "OP" and t.value == "," and depth == 0:
                if current:
                    segments.append(current)
                current = []
                cursor.next()
                continue
            if t.kind == "WORD" and depth == 0 and t.value.upper() in CLAUSE_END:
                break
            current.append(cursor.next())
        if current:
            segments.append(current)
        return segments

    def _consume_rest(self, cursor: "_Cursor") -> None:
        """Consume FROM/JOIN/WHERE/... until UNION, a closing paren, or EOF."""
        depth = 0
        while True:
            t = cursor.peek()
            if t.kind == "EOF":
                return
            if t.kind == "OP" and t.value in ("(", "["):
                depth += 1
            elif t.kind == "OP" and t.value in (")", "]"):
                if depth == 0:
                    return
                depth -= 1
            elif t.kind == "WORD" and depth == 0 and t.value.upper() == "UNION":
                return
            cursor.next()

    # ---- projection → columns ------------------------------------------------

    def _extract_column(self, seg: list[Token]) -> Column | None:
        if not seg:
            return None
        # `*` and `qual.*` expand to unknown columns — never asserted (the
        # stage still gets a warning diagnostic when this is the main select).
        if _is_star_segment(seg):
            return None
        # AS alias (last top-level AS wins, like SQL engines)
        alias: Token | None = None
        for k in range(len(seg) - 1):
            t = seg[k]
            if t.kind == "WORD" and t.value.upper() == "AS" \
                    and seg[k + 1].kind in ("WORD", "QUOTED_IDENT"):
                alias = seg[k + 1]
        if alias is not None:
            return Column(name=unquote(alias.value).lower(),
                          source_expr=self.raw[seg[0].start:seg[-1].end],
                          alias=unquote(alias.value).lower(),
                          span=self._span(seg[0].start, seg[-1].end))
        # bare identifier
        if len(seg) == 1 and seg[0].kind in ("WORD", "QUOTED_IDENT"):
            return Column(name=unquote(seg[0].value).lower(),
                          source_expr=self.raw[seg[0].start:seg[-1].end],
                          alias=None,
                          span=self._span(seg[0].start, seg[-1].end))
        # simple qualified name `qual.ident`
        if (len(seg) == 3 and seg[1].kind == "OP" and seg[1].value == "."
                and seg[0].kind in ("WORD", "QUOTED_IDENT")
                and seg[2].kind in ("WORD", "QUOTED_IDENT")):
            return Column(name=unquote(seg[2].value).lower(),
                          source_expr=self.raw[seg[0].start:seg[-1].end],
                          alias=None,
                          span=self._span(seg[0].start, seg[-1].end))
        # complex expression without alias → not asserted as a column
        return None


# ---- module-level helpers ------------------------------------------------------


class _Cursor:
    def __init__(self, tokens: list[Token], start: int = 0):
        self.tokens = tokens
        self.i = start

    def _at(self, k: int) -> Token:
        j = self.i + k
        if j >= len(self.tokens):
            return self.tokens[-1]
        return self.tokens[j]

    def peek(self, k: int = 0) -> Token:
        return self._at(k)

    def next(self) -> Token:
        t = self._at(0)
        if t.kind != "EOF":
            self.i += 1
        return t

    def accept_op(self, op: str) -> bool:
        t = self.peek()
        if t.kind == "OP" and t.value == op:
            self.next()
            return True
        return False

    def accept_keyword(self, word: str) -> bool:
        t = self.peek()
        if t.kind == "WORD" and t.value.upper() == word.upper():
            self.next()
            return True
        return False

    def skip_template(self) -> None:
        while self.peek().kind in _TEMPLATE_SKIP:
            opening = self.peek().kind
            closing = TEMPLATE_CLOSE if opening == TEMPLATE_OPEN else TEMPLATE_TAG_CLOSE
            self.next()
            while self.peek().kind not in (closing, "EOF"):
                self.next()
            if self.peek().kind == closing:
                self.next()


def _find_main_select(tokens: list[Token]) -> int | None:
    """Index of the last top-level SELECT — the stage's output query."""
    idx: int | None = None
    depth = 0
    for i, t in enumerate(tokens):
        if t.kind == "OP" and t.value in ("(", "["):
            depth += 1
        elif t.kind == "OP" and t.value in (")", "]"):
            depth = max(0, depth - 1)
        elif t.kind == "WORD" and t.value.upper() == "SELECT" and depth == 0:
            idx = i
    return idx


def _find_create_name(tokens: list[Token], main_idx: int) -> str | None:
    """CREATE [OR REPLACE] [TEMP|TEMPORARY] [MATERIALIZED] TABLE|VIEW name."""
    for i in range(main_idx):
        if tokens[i].kind == "WORD" and tokens[i].value.upper() == "CREATE":
            j = i + 1
            if j < main_idx and tokens[j].kind == "WORD" \
                    and tokens[j].value.upper() == "OR":
                j += 1
                if j < main_idx and tokens[j].kind == "WORD" \
                        and tokens[j].value.upper() == "REPLACE":
                    j += 1
            if j < main_idx and tokens[j].kind == "WORD" \
                    and tokens[j].value.upper() in ("TEMP", "TEMPORARY"):
                j += 1
            if j < main_idx and tokens[j].kind == "WORD" \
                    and tokens[j].value.upper() == "MATERIALIZED":
                j += 1
            if j >= main_idx or tokens[j].kind != "WORD" \
                    or tokens[j].value.upper() not in ("TABLE", "VIEW"):
                continue
            k = j + 1
            parts: list[str] = []
            while k < main_idx and tokens[k].kind != "EOF":
                tok = tokens[k]
                if tok.kind == "OP" and tok.value == ".":
                    parts.append(".")
                    k += 1
                    continue
                if tok.kind == "OP" and tok.value == "(":
                    break
                if tok.kind == "WORD" and tok.value.upper() == "AS":
                    break
                if tok.kind in ("WORD", "QUOTED_IDENT"):
                    parts.append(unquote(tok.value).lower())
                    k += 1
                    continue
                break
            name = "".join(parts)
            if "." in name:
                name = name.rsplit(".", 1)[-1]
            return name or None
    return None


def _first_meaningful(tokens: list[Token]) -> Token | None:
    for t in tokens:
        if t.kind in ("EOF", "TEMPLATE_OPEN", "TEMPLATE_CLOSE",
                      TEMPLATE_TAG_OPEN, TEMPLATE_TAG_CLOSE):
            continue
        if t.kind == "OP" and t.value == ";":
            continue
        return t
    return None


def collect_bare_froms(tokens: list[Token]) -> list[str]:
    """Bare `FROM <table>` references (used when no ref() calls exist).

    Mirrors the seed contract: the name immediately following a FROM keyword
    is a producer dependency; dotted names and the pseudo-tables
    select/values/dual are excluded. Paren groups that begin a query
    (after FROM/JOIN/AS) are scanned recursively so subquery dependencies are
    captured; ordinary groups (function calls, EXTRACT(...)) are not — this
    avoids the seed's false positive on `EXTRACT(YEAR FROM ts)`.
    """
    names: list[str] = []
    seen: set[str] = set()

    def is_query_group(prev: Token | None) -> bool:
        if prev is None or prev.kind != "WORD":
            return False
        return prev.value.upper() in {
            "FROM", "JOIN", "AS", "INNER", "LEFT", "RIGHT", "FULL", "CROSS",
            "OUTER", "ON", "USING",
        }

    allow_stack: list[bool] = [True]
    prev: Token | None = None
    i = 0
    n = len(tokens)
    while i < n:
        t = tokens[i]
        if t.kind in _TEMPLATE_SKIP:
            depth = 1
            i += 1
            while i < n and depth:
                if tokens[i].kind in _TEMPLATE_SKIP:
                    depth += 1
                elif tokens[i].kind in _TEMPLATE_END:
                    depth -= 1
                i += 1
            continue
        if t.kind == "OP" and t.value in ("(", "["):
            allow_stack.append(is_query_group(prev))
            prev = None
            i += 1
            continue
        if t.kind == "OP" and t.value in (")", "]"):
            if len(allow_stack) > 1:
                allow_stack.pop()
            prev = None
            i += 1
            continue
        if t.kind == "OP" and t.value == ";":
            allow_stack = [True]
            prev = None
            i += 1
            continue
        if (allow_stack[-1] and t.kind == "WORD"
                and t.value.upper() == "FROM"):
            j = i + 1
            while j < n and tokens[j].kind in _TEMPLATE_SKIP:
                closing = TEMPLATE_CLOSE if tokens[j].kind == TEMPLATE_OPEN \
                    else TEMPLATE_TAG_CLOSE
                j += 1
                while j < n and tokens[j].kind not in (closing, "EOF"):
                    j += 1
                if j < n and tokens[j].kind == closing:
                    j += 1
            if j < n and tokens[j].kind in ("WORD", "QUOTED_IDENT"):
                name = unquote(tokens[j].value).lower()
                if name not in ("select", "values", "dual") and "." not in name \
                        and name not in seen:
                    seen.add(name)
                    names.append(name)
            prev = t
            i = j
            continue
        prev = t
        i += 1
    return names