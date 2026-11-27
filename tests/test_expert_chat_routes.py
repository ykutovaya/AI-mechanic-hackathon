"""Проверяет поиск по экспертным ссылкам на применимые страницы PDF."""

import unittest
from contextlib import closing
from tempfile import TemporaryDirectory

from qdrant_client import QdrantClient

from ai_mechanic.knowledge._qdrant_store import QdrantSearch, replace_index
from ai_mechanic.knowledge.search import retrieve
from test_qdrant_storage import Vectors, fragment


class RoutedManualTests(unittest.TestCase):
    def test_binding_code_variant_and_page_must_belong_to_same_reference(self):
        def route(code, variant, pages, row):
            return dict(code=code, applicability="Д|" + variant, source_pages=pages, rows=[row])
        first = fragment("Д|all", 3)
        first.update(source_pages=[3, 4], tables=[{"rows": [["Таблица с другой страницы"]]}], page_parts=[dict(page=3, text="Обзор"), dict(page=4, text="Насос")],
                     fault_routes=[route("F13", "A", [3], 29), route("F12", "all", [3], 44)],
                     fault_bindings=[dict(route("F13", "all", [4], 18), excerpt="Насос")])
        other = dict(fragment("Е|all", 9), fault_routes=[route("F13", "all", [9], 100)])
        encoder = Vectors()
        with TemporaryDirectory() as directory, closing(QdrantClient(":memory:")) as client:
            replace_index([first, other], client=client, collection="manuals", lock_dir=directory, encoder=encoder)
            search = QdrantSearch(client=client, collection="manuals", encoder=encoder)
            for variant, expected in [(None, [(4,[18])]), ("A", [(3,[29]),(4,[18])])]:
                with self.subTest(variant=variant):
                    hits = search.search_fault("Д", "подача бензина", code="F13", variant=variant)
                    self.assertEqual([(h["page"],h["expert_rows"]) for h in hits], expected)
                    self.assertTrue(all(h["fault_code"]=="F13" for h in hits))
                    self.assertTrue(all(not h.get("tables") for h in hits))

    def test_procedure_continuation_does_not_inherit_another_pages_excel_row(self):
        from ai_mechanic.knowledge.search import _Search

        class EmptyBackend:
            def search(self, engine, query, **kwargs):
                return []
        search = _Search(EmptyBackend(), "Д", None, "Проверка насоса")
        source = dict(document_id="manual", page=3, expert_rows=[18], fault_code="F13",
                      page_parts=[dict(page=3, text="Подготовить насос."), dict(page=4, text="Проверить зазор.")])
        search.add_procedure(source)
        self.assertEqual([(item["page"], item.get("expert_rows")) for item in search.evidence],
                         [(3, [18]), (4, None)])

    def test_diagnosis_finds_checked_pdf_row_and_preserves_its_condition(self):
        group = "При резком открытии дроссельных заслонок двигатель чихает в карбюратор"
        cause, action = "Неправильная работа ускорительного насоса", "Проверить клапаны ускорительного насоса. При заедании устранить причину."
        table = [["Причина неисправности","Метод устранения"], [group,None], [cause,action]]
        body = dict(document_id="manual", page=54, text="\n".join(" | ".join(c or "" for c in row) for row in table),
                    section="Возможные неисправности", kind="table", engine_models=["Д"], applicability=["Д|all"],
                    tables=[{"rows":table}], title="Руководство", expert_rows=[689], fault_code="F13")
        class Backend:
            def search(self, engine, query, **kwargs):
                return []
            def search_fault(self, engine, query, *, code, variant, limit):
                return [body]
        diagnosis = {"status":"anomaly","signal_quality":"good","anomaly_type":"F13","anomaly_title":"Подача бензина",
                     "description":"При резком открытии дросселя чихает в карбюратор."}
        for question in ["Что проверить на двигателе?", "Что сначала проверить по результату диагностики?",
                         "Какие работы предложить для проверки?", "Что проверить при резком открытии дросселя?"]:
            result = retrieve(Backend(), "Д", question, diagnosis)
            self.assertTrue(result["evidence"], question)
            item = result["evidence"][0]
            self.assertEqual((item["page"], item["expert_rows"]), (54,[689]))
            self.assertIn(cause, item["text"])
            self.assertIn(action, item["text"])
        for question in ["Какая температура воды в радиаторе?", "Подача бензина. Что проверить на стартере?"]:
            other = retrieve(Backend(), "Д", question, diagnosis)
            self.assertFalse(any(item.get("expert_rows") for item in other["evidence"]), question)
