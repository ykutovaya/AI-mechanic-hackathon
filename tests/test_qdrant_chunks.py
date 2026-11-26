"""Применимость, физические страницы и состав фрагментов руководства."""

import unittest
from importlib.util import find_spec


def page(number, text, **values):
    return dict(document_id="d", title="Руководство", edition="1", source_sha256="a"*64,
                path="d.pdf", engine_models=["Д-1"], page=number, section="Насос", text=text, **values)


class ChunksTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(find_spec("ai_mechanic.knowledge.qdrant_chunks"), "Нужен загрузчик фрагментов")
        from ai_mechanic.knowledge.qdrant_chunks import make_chunks
        self.make = make_chunks

    def test_join_and_consistent_identity(self):
        pages = [page(1, "Проверить насос.", related_pages=[2,3]),page(2,"Снять крышку."),page(3,"Иное.")]
        note = dict(document_id="d",page=1,source_sha256="a"*64,passages=[{"parts":[
            {"page":1,"start":"Проверить насос."},{"page":2,"start":"Снять крышку."}]}])
        result = self.make(pages,[note])
        joined = next(c for c in result if c["source_pages"] == [1,2])
        self.assertEqual([p["text"] for p in joined["page_parts"]], ["Проверить насос.","Снять крышку."])
        self.assertEqual(result,self.make(pages,[note]))
        with self.assertRaises(ValueError):
            self.make(pages,[dict(note,source_sha256="b"*64)])
        with self.assertRaises(ValueError):
            self.make([pages[0],dict(pages[1],engine_models=["Д-2"]),pages[2]],[note])

    def test_no_unverified_ocr_or_variant_leak(self):
        r = page(1,"Неразмеченная инструкция.",fragments=[
            dict(text="Судовой насос.", engine_models=["Д-1"], applicability=["Д-1|судовое"])],
            ocr=dict(text="Неверный норматив",status="pending",engine_models=["Д-1"]))
        chunks = self.make([r])
        self.assertEqual([c["applicability"] for c in chunks], [["Д-1|судовое"]])
        self.assertNotIn("Неверный", str(chunks))
        self.assertNotIn("Неразмеченная", str(chunks))

    def test_table_units_and_merged_cell(self):
        r = page(3,"Таблица",tables=[{"rows":[["Датчик","Давление, кПа","R, Ом"],
                        ["23.3829","0","10"],[None,"1,5","8"]]}])
        c = next(c for c in self.make([r]) if c["kind"]=="table")
        self.assertEqual(c["page_parts"][0]["tables"][0]["rows"][-1], ["23.3829","1,5","8"])
        self.assertIn("R, Ом",c["text"])

    def test_unchecked_multi_model_page_is_excluded_but_reviewed_table_is_searchable(self):
        rows = [dict(page(1,"Величины для разных моделей."),engine_models=["Д-1","Д-2"]),
                dict(page(2,"A B",tables=[{"rows":[["Причина","Действие"],["Износ","Осмотреть"],
                                                            ["Для Д-1", "Другой допуск"]]}]),engine_models=["Д-1","Д-2"])]
        note = dict(document_id="d",page=2,source_sha256="a"*64,common_tables={
            "last_page":2,"headers":[["Причина","Действие"]],"section":"Таблица неисправностей"})
        chunks = self.make(rows,[note])
        self.assertTrue(chunks)
        self.assertTrue(all(c["applicability"] for c in chunks))
        self.assertEqual({tuple(c["source_pages"]) for c in chunks},{(2,)})
        self.assertNotIn("Другой допуск",str(chunks))
        self.assertIn("Износ",str(chunks))

    def test_reviewed_section_inherits_scope_and_skips_conditional_columns(self):
        pages = [dict(page(i,body),engine_models=["Д-1","Д-2"]) for i,body in enumerate([
            "Проверить уровень на ровной площадке.", "На части двигателей установлен клапан.",
            "После этого затянуть крышку.", "Общая операция.", "Д-1           Д-2\n100           50",
            "Для этого исполнения установлен привод."],1)]
        note = dict(document_id="d",page=1,source_sha256="a"*64,sections=[
            {"first":1,"last":5,"title":"Проверка уровня"},
            {"first":6,"last":6,"title":"Судовой привод","engine_models":["Д-2"],"applicability":["Д-2|судовое"]}])
        c = self.make(pages,[note])
        seen = {r["source_pages"][0]:r for r in c}
        self.assertEqual(set(seen),{1,4,6})
        self.assertEqual(seen[6]["applicability"],["Д-2|судовое"])
        self.assertEqual(seen[1]["applicability"],["Д-1|all","Д-2|all"])

    def test_fresh_manual_note_overrides_old_extraction(self):
        r=page(1,"Начало. Только для судового исполнения. Конец.",fragments=[
            {"text":"Только для судового исполнения.","engine_models":["Д-1"]}])
        n=dict(document_id="d",source_sha256="a"*64,page=1,fragments=[
            {"start":"Только для судового","end":"исполнения.","engine_models":["Д-1"],
             "applicability":["Д-1|судовое"]}])
        chunks=self.make([r],[n])
        self.assertEqual([c["applicability"] for c in chunks],[["Д-1|судовое"]])
        self.assertEqual(chunks[0]["text"],"Только для судового исполнения.")

    def test_registry_without_page_count_uses_pdf(self):
        import json
        import hashlib
        import tempfile
        from pathlib import Path
        import pymupdf
        from ai_mechanic.knowledge.qdrant_index import load_chunks
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "manual.pdf"
            with pymupdf.open() as pdf:
                pdf.new_page()
                pdf.save(pdf_path)
            r=page(1,"Проверить привод.")
            r.update(path=str(pdf_path),source_sha256=hashlib.sha256(pdf_path.read_bytes()).hexdigest())
            (root/"registry").write_text(json.dumps([r]),encoding="utf-8")
            (root/"pages").write_text(json.dumps(r),encoding="utf-8")
            (root/"notes").write_text("[]",encoding="utf-8")
            self.assertEqual(len(load_chunks(root/"pages",root/"notes",root/"registry")),1)

    def test_combined_manual_ocr_requires_confirmed_scope(self):
        text = "Данные из изображения."
        row = dict(page(1, "Руководство."), engine_models=["Д-1", "Д-2"],
                   ocr=dict(text=text, status="verified", engine_models=["Д-1", "Д-2"]))
        self.assertEqual(self.make([row]), [])
        note = dict(document_id="d", page=1, source_sha256="a"*64,
                    verified_ocr=dict(text=text, engine_models=["Д-1", "Д-2"],
                                      applicability=["Д-1|судовое"]))
        self.assertEqual([chunk["applicability"] for chunk in self.make([row], [note])],
                         [["Д-1|судовое"]])

    def test_variant_specific_text_is_not_assumed_common(self):
        row = page(1, "На части двигателей установлен дополнительный насос.")
        self.assertEqual(self.make([row]), [])

    def test_keeps_extracted_links_alongside_note_links(self):
        row = page(1, "Проверить насос.", related_pages=[2, 3])
        note = dict(document_id="d", page=1, source_sha256="a"*64, related_pages=[2])
        self.assertEqual(self.make([row], [note])[0]["related_pages"], [2, 3])

    def test_numbered_table_reconstructs_group_without_repeated_header(self):
        pages=[page(1,"Перечень разделов",tables=[dict(rows=[["Раздел","Проявление"],["12","ВИБРАЦИЯ"],["13","ПЕРЕГРЕВ"]])]),
               page(2,"12 ВИБРАЦИЯ. 12.1 Дисбаланс. 13 ПЕРЕГРЕВ. 13.1 Утечка.",tables=[
                    dict(rows=[["12.1 Дисбаланс","Балансировать."],["12.2 Износ","Осмотреть."]]),
                    dict(rows=[["13.1 Утечка","Испытать."]])])]
        notes=[dict(document_id="d",page=2,source_sha256="a"*64,numbered_table=dict(last_page=2,
                    contents_pages=[1],headers=["Причины","Работы"],section="Таблица"))]
        result=self.make(pages,notes)
        data=next((r for r in result if r.get("entry_number")=="12.1"),None)
        self.assertIsNotNone(data)
        self.assertEqual(data["source_pages"],[2])
        self.assertIn("ВИБРАЦИЯ",data["text"])
        self.assertNotIn("Утечка",data["text"])

    def test_pdf_extraction_preserves_exact_variant(self):
        from ai_mechanic.knowledge._notes import apply_notes
        r = page(1, "Осмотреть привод.")
        notes = [{"page":1, "fragments":[dict(start="Осмотреть", engine_models=["Д-1"],
                                              applicability=["Д-1|судовое"])]}]
        apply_notes([r],notes)
        self.assertEqual(r["fragments"][0].get("applicability"), ["Д-1|судовое"])
