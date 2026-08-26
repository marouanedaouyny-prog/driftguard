"""Rule catalog REF-001..REF-007 (ARCHITECTURE §4.4, ADR-006 risk tiers).

Every rule is deterministic and conservative: if a precondition cannot be
proven from tokens + IR, the rule does not fire. Rules never mutate; they
return RewriteCandidates with precise byte spans over `Stage.raw`.
"""
from __future__ import annotations

import re

from driftguard.core.ir.model import Stage, Span
from driftguard.core.refactor.model import AnalysisContext, RewriteCandidate
from driftguard.core.refactor import sqlutil as su

_SIDE_EFFECT_RE = re.compile(
    r"\b(insert|update|delete|create|drop|alter|grant|revoke|truncate|"
    r"merge|exec(ute)?|call)\b", re.IGNORECASE)


def _tokens(raw: str):
    return su.structure(raw)


class DropDeadCte:
    """REF-001: remove a CTE no query references (SAFE)."""
    id = "REF-001"
    version = 1
    tier = "safe"
    description = "Drop a CTE that no query references"

    def analyze(self, stage: Stage, ctx: AnalysisContext) -> list[RewriteCandidate]:
        tokens, pref = _tokens(stage.raw)
        defs, with_start = su.cte_definitions(tokens, pref)
        if not defs:
            return []
        out = []
        for i, d in enumerate(defs):
            body = stage.raw[d.body_start:d.body_end]
            if _SIDE_EFFECT_RE.search(body):
                continue
            refs = su.name_references(tokens, pref, d.name,
                                      exclude=(d.name_start, d.def_end))
            if refs:
                continue
            if len(defs) == 1:
                span = Span(with_start, d.def_end)
                after = ""
            elif i < len(defs) - 1:
                # delete up to the next CTE's name (absorbs the comma and
                # any separating whitespace)
                span = Span(d.name_start, defs[i + 1].name_start)
                after = ""
            else:
                prev = defs[i - 1]
                span = Span(prev.def_end, d.def_end)
                after = ""
            before = stage.raw[span.start:span.end]
            out.append(RewriteCandidate(
                rule_id=self.id, tier=self.tier, stage=stage.name,
                span=span, before=before, after=after,
                reason=f"CTE `{d.name}` is never referenced; removing it"))
        return out


class DedupeProjection:
    """REF-002: drop a duplicate SELECT projection item (SAFE)."""
    id = "REF-002"
    version = 1
    tier = "safe"
    description = "Remove duplicate projection columns"

    def analyze(self, stage: Stage, ctx: AnalysisContext) -> list[RewriteCandidate]:
        tokens, pref = _tokens(stage.raw)
        items = su.projection_items(tokens, pref, stage.raw)
        out = []
        seen: dict[tuple[str, str | None], int] = {}
        for k, (start, end, text) in enumerate(items):
            expr, alias = _split_projection_item(text)
            if expr is None:
                continue
            key = (expr, alias)
            if key in seen:
                comma = _comma_before(stage.raw, start)
                if comma is None:
                    continue
                while end > start and stage.raw[end - 1] in " \t\r\n":
                    end -= 1
                span = Span(comma, end)
                before = stage.raw[span.start:span.end]
                out.append(RewriteCandidate(
                    rule_id=self.id, tier=self.tier, stage=stage.name,
                    span=span, before=before, after="",
                    reason=f"duplicate of projection item {seen[key] + 1}"))
            else:
                seen[key] = k
        return out


class InlineSingleUseCte:
    """REF-003: inline a CTE referenced exactly once (SUGGESTED)."""
    id = "REF-003"
    version = 1
    tier = "suggested"
    description = "Inline a single-use CTE"

    def analyze(self, stage: Stage, ctx: AnalysisContext) -> list[RewriteCandidate]:
        tokens, pref = _tokens(stage.raw)
        defs, _with = su.cte_definitions(tokens, pref)
        out = []
        for d in defs:
            body = stage.raw[d.body_start:d.body_end]
            if _SIDE_EFFECT_RE.search(body):
                continue
            if re.search(r"\border\s+by\b|\blimit\b|\bunion\b", body,
                         re.IGNORECASE):
                continue
            all_refs = su.name_references(tokens, pref, d.name)
            # self-recursion guard: the body must not reference its own name
            if any(d.body_start <= r[0] < d.body_end for r in all_refs):
                continue
            main_refs = [r for r in all_refs
                         if not (d.name_start <= r[0] and r[1] <= d.def_end)]
            if len(main_refs) != 1:
                continue
            ref_start, ref_end = main_refs[0]
            # quoted same-name identifiers anywhere => ambiguity, skip
            if any(t.kind == "QUOTED_IDENT"
                   and t.value[1:-1].lower() == d.name.lower()
                   for t in tokens):
                continue
            before_ref = stage.raw[ref_start:ref_end]
            out.append(RewriteCandidate(
                rule_id=self.id, tier=self.tier, stage=stage.name,
                span=Span(ref_start, ref_end),
                before=before_ref, after=f"({body})",
                reason=f"CTE `{d.name}` is used exactly once; inlining it"))
        return out


class QuoteNormalize:
    """REF-004: lowercase unquoted identifiers (SAFE, canonical form)."""
    id = "REF-004"
    version = 1
    tier = "safe"
    description = "Normalize unquoted identifier case"

    def analyze(self, stage: Stage, ctx: AnalysisContext) -> list[RewriteCandidate]:
        tokens, pref = _tokens(stage.raw)
        raw = stage.raw
        edits: list[tuple[int, int, str]] = []
        depth = 0
        templ = 0
        for t in tokens:
            if t.kind == "TEMPLATE_OPEN":
                templ += 1
                continue
            if t.kind == "TEMPLATE_CLOSE":
                templ = max(0, templ - 1)
                continue
            if t.kind == "OP":
                if t.value == "(":
                    depth += 1
                elif t.value == ")":
                    depth = max(0, depth - 1)
                continue
            if templ or t.kind != "WORD":
                continue
            if su.is_keyword(t.value):
                continue
            lowered = t.value.lower()
            if lowered == t.value:
                continue
            edits.append((pref[t.start], pref[t.end], lowered))
        if not edits:
            return []
        start, end = edits[0][0], edits[-1][1]
        before = raw[start:end]
        after = raw[start:end]
        for s, e, repl in reversed(edits):
            after = after[: s - start] + repl + after[e - start:]
        return [RewriteCandidate(
            rule_id=self.id, tier=self.tier, stage=stage.name,
            span=Span(start, end), before=before, after=after,
            reason=f"lowercase {len(edits)} unquoted identifier(s) to "
                   f"canonical form")]


class StarExpand:
    """REF-005: expand `SELECT *` to an explicit column list (RISKY)."""
    id = "REF-005"
    version = 1
    tier = "risky"
    description = "Expand SELECT * to explicit columns"

    def analyze(self, stage: Stage, ctx: AnalysisContext) -> list[RewriteCandidate]:
        tokens, pref = _tokens(stage.raw)
        raw = stage.raw
        items = su.projection_items(tokens, pref, raw)
        fitems = su.from_items(tokens, pref, raw)
        if not fitems:
            return []
        out = []
        for start, end, text in items:
            m = re.fullmatch(r"\s*([A-Za-z_][A-Za-z0-9_$]*\.)?\*\s*", text)
            if not m:
                continue
            qual = m.group(1)
            qual = qual[:-1] if qual else None
            target = None
            if qual:
                for fs, fe, ft in fitems:
                    if su.item_alias(ft) == qual or \
                            su.from_target(ft) == qual:
                        target = ft
                        break
            elif len(fitems) == 1:
                target = fitems[0][2]
            if target is None:
                continue
            tname = su.from_target(target)
            if tname is None:
                continue
            src = ctx.stages_by_name.get(tname.lower())
            if src is None or not src.columns:
                continue
            cols = sorted(c.name for c in src.columns)
            if not cols:
                continue
            prefix = f"{qual}." if qual else ""
            after = ", ".join(f"{prefix}{c}" for c in cols)
            before = raw[start:end]
            out.append(RewriteCandidate(
                rule_id=self.id, tier=self.tier, stage=stage.name,
                span=Span(start, end), before=before, after=after,
                reason=f"expand `{before.strip()}` to explicit columns of "
                       f"`{tname}` ({len(cols)} columns)"))
        return out


class DeadAlias:
    """REF-006: drop a FROM alias that is never used (SAFE)."""
    id = "REF-006"
    version = 1
    tier = "safe"
    description = "Remove an unused table alias"

    def analyze(self, stage: Stage, ctx: AnalysisContext) -> list[RewriteCandidate]:
        tokens, pref = _tokens(stage.raw)
        raw = stage.raw
        items = su.from_items(tokens, pref, raw)
        out = []
        for start, end, text in items:
            alias = su.item_alias(text)
            if alias is None:
                continue
            if su.name_references(tokens, pref, alias,
                                  exclude=(start, end)):
                continue
            m = re.search(r"\b" + re.escape(alias) + r"\b\s*$", text)
            if not m:
                continue
            alias_start = start + m.start()
            alias_end = start + m.end()
            as_idx = text.upper().rfind(" AS ")
            del_start = (text.upper().rfind(" AS ") + start
                         if as_idx >= 0 else alias_start)
            before = raw[del_start:alias_end]
            out.append(RewriteCandidate(
                rule_id=self.id, tier=self.tier, stage=stage.name,
                span=Span(del_start, alias_end), before=before, after="",
                reason=f"alias `{alias}` is never referenced; dropping it"))
        return out


class DropSubqueryOrderBy:
    """REF-007: remove a subquery ORDER BY with no LIMIT (SUGGESTED).

    An ORDER BY directly inside a parenthesized query (`(SELECT ...)`,
    CTE bodies, IN/EXISTS subqueries) has no effect on the outer query
    unless a LIMIT/OFFSET/FETCH follows in the same subquery. Window
    (`OVER (ORDER BY ...)`) and aggregate (`f(x ORDER BY y)`) clauses are
    structurally excluded: their parens do not open onto SELECT/WITH.
    """
    id = "REF-007"
    version = 1
    tier = "suggested"
    description = "Drop a meaningless subquery ORDER BY"

    _MEANINGFUL_AFTER = frozenset((
        "LIMIT", "OFFSET", "FETCH", "UNION", "INTERSECT", "EXCEPT",
        "MINUS", "FOR"))

    def analyze(self, stage: Stage, ctx: AnalysisContext) -> list[RewriteCandidate]:
        tokens, pref = _tokens(stage.raw)
        raw = stage.raw
        if _SIDE_EFFECT_RE.search(raw):
            return []
        out: list[RewriteCandidate] = []
        opens: list[int] = []
        for i, t in enumerate(tokens):
            if t.kind != "OP":
                continue
            if t.value == "(":
                opens.append(i)
                continue
            if t.value != ")" or not opens:
                continue
            oi = opens.pop()
            inner = tokens[oi + 1:i]
            if not inner:
                continue
            first = inner[0]
            if not (first.kind == "WORD" and first.value.upper()
                    in ("SELECT", "WITH")):
                continue
            if any(k.kind in ("TEMPLATE_OPEN", "TEMPLATE_CLOSE")
                   for k in inner):
                continue
            if self._is_compound(inner):
                continue
            order_idx = self._interior_order(inner)
            if order_idx is None:
                continue
            if self._meaningful_tail(inner, order_idx):
                continue
            start = pref[inner[order_idx].start]
            end = pref[tokens[i].start]
            while end > start and raw[end - 1] in " \t\r\n":
                end -= 1
            while start > 0 and raw[start - 1] in " \t\r\n":
                start -= 1
            out.append(RewriteCandidate(
                rule_id=self.id, tier=self.tier, stage=stage.name,
                span=Span(start, end), before=raw[start:end], after="",
                reason="subquery ORDER BY has no effect without "
                       "LIMIT/OFFSET; dropping it"))
        return out

    def _interior_order(self, inner: list) -> int | None:
        """Index of the interior-level `ORDER BY` clause, if any.

        Depth 0 relative to the subquery interior — deeper ORDER BYs belong
        to windows/aggregates/nested subqueries and are never matched here.
        """
        lvl = 0
        for k, tk in enumerate(inner):
            if tk.kind == "OP" and tk.value == "(":
                lvl += 1
            elif tk.kind == "OP" and tk.value == ")":
                lvl -= 1
            elif lvl == 0 and tk.kind == "WORD" \
                    and tk.value.upper() == "ORDER":
                nxt = inner[k + 1] if k + 1 < len(inner) else None
                if nxt is not None and nxt.kind == "WORD" \
                        and nxt.value.upper() == "BY":
                    return k
        return None

    def _is_compound(self, inner: list) -> bool:
        """True when the subquery is a set operation (UNION/INTERSECT/...).

        A trailing ORDER BY then applies to the whole compound; the rule
        cannot prove which SELECT it decorates, so it never fires.
        """
        lvl = 0
        for tk in inner:
            if tk.kind == "OP" and tk.value == "(":
                lvl += 1
            elif tk.kind == "OP" and tk.value == ")":
                lvl -= 1
            elif lvl == 0 and tk.kind == "WORD" and \
                    tk.value.upper() in ("UNION", "INTERSECT", "EXCEPT",
                                         "MINUS"):
                return True
        return False

    def _meaningful_tail(self, inner: list, order_idx: int) -> bool:
        """True when something after the ORDER BY keeps it significant."""
        lvl = 0
        for tk in inner[order_idx + 1:]:
            if tk.kind == "OP" and tk.value == "(":
                lvl += 1
            elif tk.kind == "OP" and tk.value == ")":
                lvl -= 1
            elif lvl == 0 and tk.kind == "WORD" and \
                    tk.value.upper() in self._MEANINGFUL_AFTER:
                return True
        return False


def _comma_before(text: str, pos: int) -> int | None:
    """Index of the comma preceding `pos` (skipping whitespace), if any."""
    i = pos - 1
    while i >= 0 and text[i] in " \t\r\n":
        i -= 1
    if i >= 0 and text[i] == ",":
        return i
    return None


def _split_projection_item(text: str) -> tuple[str | None, str | None]:
    """(normalized_expr, alias) for dedupe; None expr if unparseable."""
    stripped = text.strip()
    if not stripped:
        return None, None
    as_idx = re.search(r"\bAS\s+([A-Za-z_][A-Za-z0-9_$]*)", stripped,
                       re.IGNORECASE)
    if as_idx:
        expr = stripped[:as_idx.start()]
        return su.normalize_expr(expr), as_idx.group(1).lower()
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_$]*", stripped)
    if len(words) == 2 and not su.is_keyword(words[1]) and \
            not su.is_keyword(words[0]):
        return su.normalize_expr(words[0]), words[1].lower()
    return su.normalize_expr(stripped), None


RULES: list = [DropDeadCte(), DedupeProjection(), InlineSingleUseCte(),
               QuoteNormalize(), StarExpand(), DeadAlias(),
               DropSubqueryOrderBy()]
RULES_BY_ID = {r.id: r for r in RULES}