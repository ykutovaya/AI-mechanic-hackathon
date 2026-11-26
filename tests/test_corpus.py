"""Проверки обновления корпуса при последовательной обработке PDF."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ai_mechanic.knowledge.pdf import process_manual


class CorpusTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.output = self.root / "processed" / "pages.jsonl"
        self.registry = self.root / "manuals.json"
        self.notes = self.root / "notes.json"
        self.manuals = []
        for name in ("А", "Б"):
            path = self.root / f"{name}.pdf"
            path.write_bytes(name.encode())
            self.manuals.append(dict(document_id=name, title=f"Руководство {name}",
                                     path=str(path), engine_models=["М1", "М2"],
                                     edition="Редакция 2", page_count=3))
        self.registry.write_text(json.dumps(self.manuals), encoding="utf-8")

    def extract(self, name="А", texts=("", "Таблица 1\nА   Б", "Продолжение таблицы 1"), **options):
        def reader(pdf_path, **settings):
            for i, text in enumerate(texts, 1):
                yield dict(page=i, section="Регулировка", text=text)
        return process_manual(self.root / f"{name}.pdf", self.registry, self.output,
                              reader=reader, **options)

    def rows(self):
        return [json.loads(line) for line in self.output.read_text(encoding="utf-8").splitlines()]

    def test_incremental_replacement_keeps_other_manual_and_page_numbers(self):
        self.assertEqual(self.extract(), 3)
        self.extract("Б")
        self.extract(texts=("", "Новая таблица", "Конец"))
        rows = self.rows()
        self.assertEqual(len(rows), 6)
        a = [r for r in rows if r["document_id"] == "А"]
        self.assertEqual([r["page"] for r in a], [1, 2, 3])
        self.assertEqual(a[1]["text"], "Новая таблица")
        self.assertEqual(a[1]["edition"], "Редакция 2")
        self.assertEqual(a[1]["engine_models"], ["М1", "М2"])
        self.assertEqual(a[1]["path"], self.manuals[0]["path"])
        self.assertEqual(a[1]["title"], "Руководство А")
        self.assertEqual(a[1]["section"], "Регулировка")

    def test_link_to_both_parts_and_applicability_of_fragment(self):
        self.notes.write_text(json.dumps([dict(
            document_id="А", page=2, section="Подшипники",
            fragments=[dict(start="ТАБЛИЦА 1", end="А Б", engine_models=["М2"],
                            condition="Только с датчиком")])]), encoding="utf-8")
        self.assertEqual(self.extract(notes=self.notes), 3)
        _, first, second = self.rows()
        self.assertEqual(first["related_pages"], [3])
        self.assertEqual(second["related_pages"], [2])
        self.assertEqual(first["section"], "Подшипники")
        self.assertEqual(first["fragments"][0]["engine_models"], ["М2"])
        self.assertEqual(first["fragments"][0]["condition"], "Только с датчиком")
        self.assertEqual(first["fragments"][0]["text"], "Таблица 1\nА   Б")

    def test_wrong_registry_page_count_leaves_previous_corpus_intact(self):
        self.assertEqual(self.extract(), 3)
        before = self.output.read_bytes()
        with self.assertRaises(ValueError):
            self.extract(texts=("только одна страница",))
        self.assertEqual(self.output.read_bytes(), before)

    def test_stale_fragment_marker_aborts_update(self):
        self.assertEqual(self.extract(), 3)
        before = self.output.read_bytes()
        self.notes.write_text(json.dumps([dict(document_id="А", page=2,
            fragments=[dict(start="Нет такого пункта", engine_models=["М1"])])]), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.extract(notes=self.notes)
        self.assertEqual(self.output.read_bytes(), before)

    def test_rerun_without_notes_keeps_existing_fragment_scopes(self):
        self.notes.write_text(json.dumps([dict(
            document_id="А", page=2, section="Подшипники", related_pages=[1],
            fragments=[dict(start="Таблица 1", end="А Б", engine_models=["М2"])])]),
            encoding="utf-8")
        self.extract(notes=self.notes)
        self.extract()
        first, middle, last = self.rows()
        self.assertEqual(middle["section"], "Подшипники")
        self.assertEqual(middle["fragments"][0]["engine_models"], ["М2"])
        self.assertIn(1, middle["related_pages"])
        self.assertIn(2, first["related_pages"])

    def test_verified_ocr_survives_rerun_only_for_same_source(self):
        def reader(pdf_path, **settings):
            for n in range(1, 4):
                page = dict(page=n, text="Подпись", section="Схема")
                if n in settings["ocr_pages"]:
                    page["ocr"] = dict(status="pending", text="черновик", language="rus", rotate=90)
                yield page
        doc = self.root / "А.pdf"
        args = doc, self.registry, self.output
        process_manual(*args, reader=reader, ocr_pages=[2])
        sha = self.rows()[1]["source_sha256"]
        self.assertEqual(self.rows()[1]["ocr"]["status"], "pending")
        self.notes.write_text(json.dumps([dict(document_id="А", page=2, source_sha256=sha,
            verified_ocr=dict(text="сверенный текст", engine_models=["М2"]))]), encoding="utf-8")
        process_manual(*args, reader=reader, notes=self.notes)
        process_manual(*args, reader=reader)
        ocr = self.rows()[1]["ocr"]
        self.assertEqual(ocr["status"], "verified")
        self.assertEqual(ocr["raw_text"], "черновик")
        self.assertEqual(ocr["rotate"], 90)
        self.assertEqual(ocr["engine_models"], ["М2"])
        doc.write_bytes(b"other edition")
        with self.assertRaisesRegex(ValueError, "PDF"):
            process_manual(*args, reader=reader, notes=self.notes)
