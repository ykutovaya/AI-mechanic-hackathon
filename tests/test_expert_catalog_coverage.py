"""Регрессии измерения охвата каталога в настоящем чате."""
import unittest

from check_expert_catalog import compare_rows


class CheckReferences(unittest.TestCase):
    def test_wrong_page_duplicate_and_unexpected_row_do_not_count(self):
        expected = {2: ("doc", (5, 6), "route"), 3: ("doc", (9,), "reading")}
        row = {"row": 2, "level": "route", "sources": [{"document_id": "doc", "page": 5}]}
        complete = dict(row, sources=row["sources"] + [{"document_id": "doc", "page": 6}])
        extra = dict(row, row=4)
        self.assertEqual(compare_rows(expected, [complete]), ({2}, ["missing:3"]))
        valid, errors = compare_rows(expected, [row, row, extra])
        self.assertEqual(valid, set())
        self.assertEqual(set(errors), {"missing:3", "duplicate:2", "source_mismatch:2", "unexpected:4"})
