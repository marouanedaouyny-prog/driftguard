import tempfile
import unittest
from pathlib import Path

from driftguard.lineage import build_lineage
from driftguard.parser import parse_sql_file


class LineageTest(unittest.TestCase):

    def _stage(self, name: str, sql: str):
        tmp = Path(tempfile.mkdtemp())
        path = tmp / f"{name}.sql"
        path.write_text(sql, encoding="utf-8")
        return parse_sql_file(path)

    def test_edges_and_consumers(self):
        a = self._stage("a", "SELECT x, y FROM raw")
        b = self._stage("b", "SELECT x FROM {{ ref('a') }}")
        c = self._stage("c", "SELECT x FROM {{ ref('a') }}")
        lineage = build_lineage([a, b, c])
        self.assertEqual(sorted(lineage.edges), [("a", "b"), ("a", "c")])
        self.assertEqual(lineage.consumers("a"), ["b", "c"])
        self.assertEqual(lineage.producers("b"), ["a"])

    def test_missing_ref_tracked(self):
        a = self._stage("a", "SELECT x FROM {{ ref('ghost') }}")
        lineage = build_lineage([a])
        self.assertEqual(lineage.missing, [("ghost", "a")])

    def test_cycle_detected(self):
        a = self._stage("a", "SELECT x FROM {{ ref('b') }}")
        b = self._stage("b", "SELECT x FROM {{ ref('a') }}")
        lineage = build_lineage([a, b])
        self.assertEqual(len(lineage.cycles), 1)

    def test_self_ref_ignored(self):
        a = self._stage("a", "SELECT x FROM {{ ref('a') }}")
        lineage = build_lineage([a])
        self.assertEqual(lineage.edges, [])


if __name__ == "__main__":
    unittest.main()