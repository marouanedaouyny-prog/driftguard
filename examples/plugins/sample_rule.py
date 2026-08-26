"""Example Rule-protocol plugin (ARCHITECTURE §2.1, Phase 5).

Drop a ``*.py`` file like this into a directory and point ``refactor
analyze|plan --rules-dir DIR`` at it. Loading plugin code is a deliberate
trust decision — only load rules you wrote or audited.

A rule object only needs the fields the protocol requires (id, version,
tier, description) plus an ``analyze(stage, ctx) -> list[RewriteCandidate]``
method. Any module-level object with those attributes is registered.
"""

from driftguard.core.ir.model import Span
from driftguard.core.refactor.model import RewriteCandidate


class UpperCaseSelect:
    """Trivial sample rule: uppercase a lowercase `select` keyword.

    Solely a plugin-seam demo — it does not improve SQL and is not a
    real refactoring suggestion.
    """

    id = "PLUG-001"
    version = 1
    tier = "suggested"
    description = "Demo plugin: uppercase a lowercase `select` keyword"

    def analyze(self, stage, ctx):
        start = stage.raw.find("select")
        if start < 0:
            return []
        return [RewriteCandidate(
            rule_id=self.id, tier=self.tier, stage=stage.name,
            span=Span(start, start + len("select")), before="select",
            after="SELECT",
            reason="demo plugin rule (uppercase select)")]


PLUG_001 = UpperCaseSelect()