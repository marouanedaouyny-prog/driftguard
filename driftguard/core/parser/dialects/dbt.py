"""dbt-style project parser — the Phase 1 dialect (ADR-001).

Project discovery: recursive `*.sql` under the root (the seed contract;
`dbt_project.yml` presence is recorded but not required, so the parser also
works on bare SQL trees). `ref()/source()` resolution is the static Jinja
subset from `core/parser/jinja.py`; `sources.yml` is parsed by a tiny
key:value reader (documented limitation — YAML-free by design).

Fail-loud contract (ARCHITECTURE §4.1): `parse_pipeline` returns a Pipeline
whose stages carry structured `Diagnostic`s. Files that cannot be tokenized
at all get an error-kind diagnostic; refactor commands treat those as a hard
failure (exit 2), inspect/drift surface them as warnings.
"""
from __future__ import annotations

import re
from pathlib import Path

from driftguard.core.ir.model import Diagnostic, Pipeline, Stage
from driftguard.core.ir.serialize import (
    pipeline_fingerprint,
    stage_fingerprint,
)
from driftguard.core.parser.jinja import (
    extract_refs_and_sources,
    template_hints,
)
from driftguard.core.parser.sql import (
    SQLParser,
    collect_bare_froms,
)
from driftguard.core.parser.tokenizer import unquote


class DbtParser:
    """Parses dbt-style projects (and bare SQL trees) into IR."""

    def parse_file(self, path: Path) -> Stage | None:
        if path.suffix.lower() != ".sql" or not path.is_file():
            return None
        raw = path.read_text(encoding="utf-8", errors="replace")
        sp = SQLParser(raw, file=str(path))
        create_name, ctes, columns, diags = sp.parse()

        name = path.stem.lower()
        if create_name:
            name = create_name

        refs, sources = extract_refs_and_sources(sp.tokens)
        if not refs:
            bare = collect_bare_froms(sp.tokens)
            if bare:
                refs = _bare_edges(bare)

        hints = template_hints(sp.tokens)
        for hint in hints:
            line, col = 1, 1
            if sp.tokens:
                first = next((t for t in sp.tokens
                              if t.kind not in ("EOF", "TEMPLATE_OPEN",
                                                "TEMPLATE_CLOSE",
                                                "TEMPLATE_TAG_OPEN",
                                                "TEMPLATE_TAG_CLOSE")), None)
                if first is not None:
                    line, col = first.line, first.col
            diags.append(Diagnostic(str(path), line, col, hint, kind="warning"))

        stage = Stage(
            name=name,
            path=path,
            kind="model",
            raw=raw,
            columns=columns,
            refs=refs,
            sources=sources,
            ctes=ctes,
            create_name=create_name,
            dialect_hints=hints,
            diagnostics=diags,
        )
        stage.fingerprint = stage_fingerprint(stage, path.parent)
        return stage

    def parse_project(self, root: Path) -> Pipeline:
        stages: list[Stage] = []
        for path in sorted(root.rglob("*.sql")):
            stage = self.parse_file(path)
            if stage is not None:
                stages.append(stage)
        pipeline = Pipeline(root=root, stages=stages)
        pipeline.fingerprint = pipeline_fingerprint(pipeline)
        return pipeline


def _bare_edges(names: list[str]):
    from driftguard.core.ir.model import RefEdge

    return [RefEdge(n, "bare") for n in names]


parse_sql_file = DbtParser().parse_file
parse_pipeline = DbtParser().parse_project


# ---- sources.yml (tiny YAML-free subset, documented limitation) ---------------

_SOURCE_ENTRY_RE = re.compile(r"^(\s*)- name:\s*(.+?)\s*$")
_SOURCE_TABLES_RE = re.compile(r"^\s*tables:\s*$")
# In the documented subset sources are indented 2 spaces and tables 6
# (`- name:` under `tables:`). Table entries are simply the deeper ones.
_TABLE_INDENT = 5


def parse_sources_yml(path: Path) -> list[dict]:
    """Parse the tiny sources.yml subset: `- name:` / `tables:` / `- name:`.

    Returns [{"source": str, "tables": [str, ...]}, ...]. A `- name:` line
    indented >= 5 spaces is a table entry; anything shallower is a new
    source. Unknown keys are ignored; malformed lines are skipped silently
    (documented limitation — this is a heuristic, not a YAML parser).
    """
    if not path.is_file():
        return []
    sources: list[dict] = []
    current: dict | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _SOURCE_ENTRY_RE.match(line)
        if m:
            indent = len(m.group(1))
            name = unquote(m.group(2)).strip().lower()
            if indent >= _TABLE_INDENT and current is not None:
                current["tables"].append(name)
            else:
                current = {"source": name, "tables": []}
                sources.append(current)
            continue
        if _SOURCE_TABLES_RE.match(line):
            continue
    return sources


def find_sources(root: Path) -> set[str]:
    """Collect qualified `source.table` names from every `sources.yml` in the
    project tree (ARCH §4.1: sources.yml for source definitions; dbt allows
    them anywhere). Unresolvable `source()` refs become lineage missing refs.
    """
    qualified: set[str] = set()
    for path in sorted(root.rglob("sources.yml")):
        for entry in parse_sources_yml(path):
            for table in entry["tables"]:
                qualified.add(f"{entry['source']}.{table}")
    return qualified