"""Static Jinja subset (ARCHITECTURE §4.1).

v1 understands exactly these template expressions:
- `ref('name')` / `ref("name")`          → RefEdge kind "ref"
- `source('source','table')`             → SourceRef
- `config(key=value, ...)`               → understood, no hint
- `{% raw %} ... {% endraw %}` blocks    → literal content, understood

Anything else inside a template region (`var('x')`, `{% for %}`, `{% if %}`,
`set ...`) is extracted as an *unresolved template marker*: the IR records a
`dialect_hint` ("unknown_template_region") so downstream rules can refuse to
rewrite inside unknown regions. Full Jinja rendering is out of scope.

The token stream is flat (template markers are tokenized like any other
token), so these helpers operate on the token list as a whole.
"""
from __future__ import annotations

from driftguard.core.ir.model import RefEdge, SourceRef
from driftguard.core.parser.tokenizer import (
    TEMPLATE_CLOSE,
    TEMPLATE_COMMENT_CLOSE,
    TEMPLATE_COMMENT_OPEN,
    TEMPLATE_OPEN,
    TEMPLATE_TAG_CLOSE,
    TEMPLATE_TAG_OPEN,
    Token,
    unquote,
)

_KNOWN_TEMPLATE_WORDS = {"REF", "SOURCE", "CONFIG", "RAW", "ENDRAW"}

_TEMPLATE_BOUNDS = {
    TEMPLATE_OPEN: TEMPLATE_CLOSE,
    TEMPLATE_TAG_OPEN: TEMPLATE_TAG_CLOSE,
    TEMPLATE_COMMENT_OPEN: TEMPLATE_COMMENT_CLOSE,
}


def _function_call(tokens: list[Token], i: int) -> tuple[str, list[str]] | None:
    """If tokens[i] starts a function call with string args, return (name, args)."""
    t = tokens[i]
    if t.kind != "WORD" or t.value.lower() not in ("ref", "source", "config"):
        return None
    if i + 1 >= len(tokens) or tokens[i + 1].kind != "OP" or tokens[i + 1].value != "(":
        return None
    args: list[str] = []
    j = i + 2
    while j < len(tokens):
        tok = tokens[j]
        if tok.kind == "OP" and tok.value == ")":
            return t.value.lower(), args
        if tok.kind in ("STRING", "QUOTED_IDENT"):
            args.append(unquote(tok.value).lower())
        elif tok.kind == "EOF":
            return None
        j += 1
    return None


def extract_refs_and_sources(tokens: list[Token]) -> tuple[list[RefEdge], list[SourceRef]]:
    """Collect ref()/source() calls anywhere in the token stream.

    Works both inside `{{ ... }}` and as bare SQL function calls (the seed
    accepted both forms). Order is preserved; duplicates are dropped.
    """
    refs: list[RefEdge] = []
    sources: list[SourceRef] = []
    seen_refs: set[str] = set()
    seen_sources: set[tuple[str, str]] = set()
    i = 0
    n = len(tokens)
    while i < n:
        call = _function_call(tokens, i)
        if call is not None:
            name, args = call
            if name == "ref" and args:
                if args[0] not in seen_refs:
                    seen_refs.add(args[0])
                    refs.append(RefEdge(args[0], "ref"))
            elif name == "source" and len(args) >= 2:
                key = (args[0], args[1])
                if key not in seen_sources:
                    seen_sources.add(key)
                    sources.append(SourceRef(key[0], key[1]))
        i += 1
    return refs, sources


def template_hints(tokens: list[Token]) -> list[str]:
    """Dialect hints for template constructs v1 does not resolve.

    A template region is marked `unknown_template_region` when it contains a
    function call other than ref/source/config, or a control-flow keyword
    (for/if/set/var/macro/...). Plain argument words (e.g. `materialized`
    inside `config(materialized='table')`) never hint; `{# #}` comments and
    `{% raw %}` blocks are literal and never hint.
    """
    hints: list[str] = []
    opening: str | None = None
    _TAG_WORDS = {"FOR", "ENDFOR", "IF", "ELSE", "ELIF", "ENDIF", "SET",
                  "ENDSET", "VAR", "DO", "WITH", "MACRO", "ENDMACRO", "IN",
                  "IS", "NOT", "TRUE", "FALSE", "NONE"}
    for i, t in enumerate(tokens):
        if t.kind in _TEMPLATE_BOUNDS:
            opening = t.kind
            continue
        if t.kind in _TEMPLATE_BOUNDS.values():
            opening = None
            continue
        if opening not in (TEMPLATE_OPEN, TEMPLATE_TAG_OPEN):
            continue
        if t.kind != "WORD":
            continue
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        is_call = nxt is not None and nxt.kind == "OP" and nxt.value == "("
        unknown = (is_call and t.value.upper() not in _KNOWN_TEMPLATE_WORDS) \
            or (not is_call and t.value.upper() in _TAG_WORDS)
        if unknown and "unknown_template_region" not in hints:
            hints.append("unknown_template_region")
    return hints