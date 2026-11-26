"""Проверки извлечения PDF, таблиц и результатов сверки OCR."""

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pymupdf

from ai_mechanic.knowledge._extract import iter_pages
from ai_mechanic.knowledge.pdf import main, process_manual


class PageContentTests(unittest.TestCase):
    def test_multiline_warning_cannot_replace_a_section_heading(self):
        from types import SimpleNamespace
        from ai_mechanic.knowledge._extract import _local_heading

        def line(text):
            return {"spans": [{"text": text, "flags": 16}], "bbox": (150, 50, 450, 65)}

        warning = {"lines": [line("Внимание! Перед сборкой"), line("Проверить состояние деталей"),
                             line("и устранить повреждения.")]}
        title = {"lines": [line("Сборка двигателя")]}
        page = SimpleNamespace(cropbox=pymupdf.Rect(0, 0, 600, 800),
                               get_text=lambda *args, **kwargs: {"blocks": [warning, title]})
        self.assertEqual(_local_heading(page), "Сборка двигателя")

    def test_ocr_on_selected_page_remains_separate_from_text_layer(self):
        with TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "две страницы.pdf"
            with pymupdf.open() as doc:
                doc.new_page()
                doc.new_page().insert_text((30, 30), "Figure 2")
                doc.save(pdf)
            with patch("ai_mechanic.knowledge._extract.subprocess.run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = "ПОДПИСЬ".encode()
                rows = list(iter_pages(pdf, ocr_pages=[2], ocr_rotate=90))
            run.assert_called_once()
            self.assertNotIn("ocr", rows[0])
            self.assertNotIn("ПОДПИСЬ", rows[1]["text"])
            self.assertIn("Figure 2", rows[1]["text"])
            self.assertEqual(rows[1]["ocr"]["text"], "ПОДПИСЬ")
            self.assertEqual(rows[1]["ocr"]["status"], "pending")

    def test_cli_ocr_auto_detects_graphics_and_accepts_manual_pages(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "руководство.pdf"
            pix = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, 256, 256), False)
            pix.clear_with(200)
            body = ("The manual describes how to inspect the engine and its components.\n" * 5).strip()
            with pymupdf.open() as doc:
                doc.new_page()
                doc.new_page().insert_text((40, 40), "Manually selected page")
                scan = doc.new_page()
                scan.insert_image(pymupdf.Rect(45, 605, 550, 820), pixmap=pix, keep_proportion=False)
                scan.insert_text((45, 580), "Figure 23")
                scan.set_rotation(90)
                mixed = doc.new_page()
                mixed.insert_text((40, 40), body)
                mixed.insert_image(pymupdf.Rect(45, 300, 550, 700), pixmap=pix, keep_proportion=False)
                searchable = doc.new_page()
                searchable.insert_image(pymupdf.Rect(0, 0, 595, 842), pixmap=pix, keep_proportion=False)
                searchable.insert_text((40, 40), body)
                logo = doc.new_page()
                logo.insert_image(pymupdf.Rect(30, 30, 110, 110), pixmap=pix)
                logo.insert_text((30, 130), "Service manual")
                drawing = doc.new_page()
                for row in range(5):
                    for col in range(5):
                        x, y = 50 + col * 95, 140 + row * 100
                        drawing.draw_rect(pymupdf.Rect(x, y, x + 40, y + 30))
                drawing.insert_text((40, 45), "Figure 24")
                doc.new_page().insert_text((40, 40), body)
                doc.save(pdf)

            registry = root / "manuals.json"
            registry.write_text(json.dumps([dict(document_id="manual", title="Руководство",
                edition="1", engine_models=["Д"], path=str(pdf))]), encoding="utf-8")
            pages = root / "pages.jsonl"
            args = [str(pdf), "--registry", str(registry), "--output", str(pages), "--ocr-page", "auto,2"]
            with patch("ai_mechanic.knowledge._extract._recognize",
                       return_value={"text": "Распознано", "status": "pending"}) as recognize:
                self.assertEqual(main(args), 0)

            rows = [json.loads(line) for line in pages.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([record["page"] for record in rows if "ocr" in record], [2, 3, 4, 7])
            self.assertEqual(recognize.call_count, 4)
            self.assertIn("Figure 23", rows[2]["text"])
            self.assertTrue(all(row["ocr"]["status"] == "pending" for row in rows if "ocr" in row))

    def test_verified_ocr_preserves_original_text_and_applies_to_reviewed_model(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "схема.pdf"
            with pymupdf.open() as doc:
                doc.new_page().insert_text((40, 40), "Figure 23")
                doc.save(pdf)

            registry = root / "manuals.json"
            registry.write_text(json.dumps([{
                "document_id": "diagram",
                "title": "Схема",
                "edition": "1",
                "engine_models": ["А", "Б"],
                "path": str(pdf),
            }]), encoding="utf-8")
            output = root / "pages.jsonl"
            with patch("ai_mechanic.knowledge._extract._recognize", return_value={
                "status": "pending", "text": "исходная подпись", "language": "rus", "rotate": 0,
            }):
                process_manual(pdf, registry, output, ocr_pages=[1])

            page = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(page["ocr"]["status"], "pending")
            self.assertNotIn("исходная подпись", page["text"])
            self.assertEqual(page["source_sha256"], hashlib.sha256(pdf.read_bytes()).hexdigest())

            notes = root / "page_notes.json"
            notes.write_text(json.dumps([{
                "document_id": "diagram",
                "page": 1,
                "source_sha256": page["source_sha256"],
                "verified_ocr": {"text": "Компрессор", "engine_models": ["Б"]},
            }]), encoding="utf-8")

            process_manual(pdf, registry, output, notes=notes)
            reviewed = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(reviewed["text"], page["text"])
            self.assertEqual(reviewed["ocr"]["status"], "verified")
            self.assertEqual(reviewed["ocr"]["raw_text"], "исходная подпись")
            self.assertEqual(reviewed["ocr"]["text"], "Компрессор")
            self.assertEqual(reviewed["ocr"]["engine_models"], ["Б"])

    def test_rotated_page_preserves_table_rows(self):
        with TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "повёрнутая таблица.pdf"
            with pymupdf.open() as source, pymupdf.open() as doc:
                table = source.new_page(width=300, height=170)
                for x in (20, 130, 280):
                    table.draw_line((x, 40), (x, 100))
                for y in (40, 70, 100):
                    table.draw_line((20, y), (280, y))
                table.insert_text((30, 60), "Cause")
                table.insert_text((140, 60), "Action")
                table.insert_text((30, 90), "Loose spring")
                table.insert_text((140, 90), "Replace the oil pump")
                rotated = doc.new_page(width=170, height=300)
                rotated.show_pdf_page(rotated.rect, source, 0, rotate=90)
                rotated.set_rotation(90)
                doc.save(pdf)

            first, = iter_pages(pdf)
            self.assertEqual(first["page"], 1)
            self.assertIn("Loose spring", first["text"])
            self.assertEqual([table["rows"] for table in first.get("tables", [])], [
                [["Cause", "Action"], ["Loose spring", "Replace the oil pump"]],
            ])

    def test_cause_and_action_stay_in_one_table_row(self):
        with TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "таблица.pdf"
            with pymupdf.open() as doc:
                page = doc.new_page()
                for x in (50, 180, 350):
                    page.draw_line((x, 60), (x, 110))
                for y in (60, 85, 110):
                    page.draw_line((50, y), (350, y))
                page.insert_text((60, 76), "Cause")
                page.insert_text((190, 76), "Action")
                page.insert_text((60, 100), "Loose spring")
                page.insert_text((190, 100), "Replace the oil pump")
                doc.save(pdf)

            first, = iter_pages(pdf)
            self.assertEqual(len(first["tables"]), 1)
            self.assertEqual(first["tables"][0]["rows"], [
                ["Cause", "Action"],
                ["Loose spring", "Replace the oil pump"],
            ])
