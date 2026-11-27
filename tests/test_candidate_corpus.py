"""Приёмка разметки, для которой нужен комплект данных организаторов."""

import json
from pathlib import Path
import unittest

from ai_mechanic.knowledge.qdrant_chunks import make_chunks


class CandidateCorpusTests(unittest.TestCase):
    def test_rotated_pulley_card_retains_both_decisions_on_their_source_page(self):
        pages_path = Path("data/processed/pages.jsonl")
        notes_path = Path("data/annotations/page_notes.json")
        if not pages_path.exists() or not notes_path.exists():
            self.skipTest("Нужны извлечённые страницы и разметка выданного комплекта.")
        key = ("yamz-236m2-rk-2014", 101)
        def matches(row):
            return (row["document_id"], row["page"]) == key

        with pages_path.open(encoding="utf-8") as src:
            page = next(row for row in map(json.loads, src) if matches(row))
        notes = [n for n in json.loads(notes_path.read_text(encoding="utf-8-sig")) if matches(n)]
        chunks = make_chunks([page], notes)
        self.assertTrue(chunks)
        self.assertTrue(all(chunk["source_pages"] == [101] for chunk in chunks))
        tables = [(chunk, table) for chunk in chunks for part in chunk["page_parts"]
                  for table in part.get("tables", [])]
        self.assertTrue(tables, "Карта дефектации шкива потеряла табличные строки")
        decisions = {}
        for chunk, table in tables:
            self.assertIn("236-1005061-Б", chunk["section"] + chunk["condition"])
            header = table["rows"][0]
            for row in table["rows"][1:]:
                defect = row[header.index("Возможный дефект")]
                method = row[header.index("Способ установления дефекта и средства контроля")]
                decisions[defect] = (method, row[header.index("Заключение")])
        self.assertEqual(decisions["Трещины или обломы"], ("Осмотр", "Браковать"))
        method, conclusion = decisions["Износ бортов шкива под ремень"]
        self.assertIn("16,9 мм", method)
        self.assertIn("12,5 мм", method)
        self.assertEqual(conclusion, "Браковать")
