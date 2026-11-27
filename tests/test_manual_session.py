"""Проверки поиска и работы с результатом диагностической сессии."""

from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pymupdf

from ai_mechanic.ask import ManualSession


class ManualSessionTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        vehicle = {
            "vehicle_id": "V1", "model": "Машина", "engine_model": "ДВС-1",
            "sessions": [{"session_id": "S1", "vehicle_id": "V1", "status": "anomaly"}],
        }
        self.write_json("data/raw/vehicles.json", {"vehicles": [vehicle]})
        path = self.root / "data/raw/manuals/manual.pdf"
        path.parent.mkdir()
        with pymupdf.open() as pdf:
            pdf.new_page()
            pdf.new_page()
            pdf.save(path)
        self.manual = {
            "document_id": "manual", "title": "Руководство ДВС-1", "edition": "2026",
            "path": "data/raw/manuals/manual.pdf", "engine_models": ["ДВС-1"],
            "page_count": 2, "source_sha256": sha256(path.read_bytes()).hexdigest(),
        }
        self.write_json("data/annotations/manuals.json", [self.manual])
        self.diagnostic = {
            "vehicle_id": "V1", "session_id": "S1", "status": "anomaly",
            "anomaly_type": "F02", "anomaly_title": "Низкое давление масла",
            "description": "Горит лампа.", "anomaly_score": 0.91, "confidence": 0.47,
            "signal_quality": "warning", "model_version": "mock-v1",
        }
        self.ml_calls = []
        self.search_calls = []
        self.evidence = [
            dict(self.manual, page=1, kind="fact", applicability=["ДВС-1|all"],
                 text="Измерять на холодном двигателе."),
            dict(self.manual, page=2, kind="fact", applicability=["ДВС-1|all"],
                 text="Допустимый зазор 0,3 мм.", work="Измерить зазор."),
        ]
        self.session = ManualSession(
            root=self.root, search=object(), diagnosis_provider=self.fetch, retriever=self.retrieve)

    def write_json(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def fetch(self, vehicle_id, session_id):
        self.ml_calls.append((vehicle_id, session_id))
        return self.diagnostic

    def retrieve(self, search, engine, question, *, diagnosis, topic, variant, procedures):
        self.search_calls.append((engine, diagnosis, topic, variant))
        return {"queries": [question], "evidence": self.evidence, "topic": "зазор"}

    def test_question_keeps_conditions_and_exact_pages_without_calling_ml(self):
        answer = self.session.ask("V1", "Как измерить зазор?", check_id="S1")
        self.assertEqual(self.ml_calls, [])
        self.assertEqual(self.search_calls, [("ДВС-1", None, "", None)])
        self.assertIn("на холодном двигателе", answer["text"])
        self.assertIn("0,3 мм", answer["text"])
        self.assertEqual([source["page"] for source in answer["sources"]], [1, 2])
        self.assertTrue(answer["sources"][1]["url"].endswith("#page=2"))

    def test_explicit_ml_fetch_and_reuse(self):
        self.assertEqual(self.session.machines[0]["history"][0]["check_id"], "S1")
        diagnosis = self.session.diagnose("V1", "S1")
        answer = self.session.ask("V1", "Что проверить?", check_id="S1", diagnosis=diagnosis)
        self.assertEqual(self.ml_calls, [("V1", "S1")])
        self.assertEqual(self.search_calls[0][:2], ("ДВС-1", diagnosis))
        self.assertIn("F02", answer["text"])
        self.assertIn("Горит лампа.", answer["text"])
        self.assertIn("помехи", answer["text"])
        self.assertIn("низкая", answer["text"])
        self.assertEqual(answer["inspection_steps"], [
            {"document_id": "manual", "page": 2, "role": "work", "text": "Измерить зазор."},
        ])

    def test_bad_or_failed_recording_stops_advice_and_offers_retry(self):
        for change in ({"status": "check_failed"}, {"signal_quality": "bad"}):
            with self.subTest(change=change):
                diagnosis = dict(self.diagnostic, **change)
                answer = self.session.ask("V1", "Что проверить?", check_id="S1", diagnosis=diagnosis)
                self.assertIn("Повторите запись", answer["text"])
                self.assertNotIn("Горит лампа", answer["text"])
                self.assertEqual(answer["sources"], [])
                self.assertEqual(answer["inspection_steps"], [])
        self.assertEqual(self.search_calls, [])

    def test_rejects_wrong_session_before_search_and_service(self):
        with self.assertRaises(ValueError):
            self.session.diagnose("V1", "S2")
        with self.assertRaises(ValueError):
            self.session.ask("V1", "Что проверить?", check_id="S1",
                             diagnosis=dict(self.diagnostic, vehicle_id="V2"))
        self.assertEqual(self.search_calls, [])
        self.assertEqual(self.ml_calls, [])

    def test_ignores_other_variant_and_unavailable_physical_page(self):
        self.evidence[0]["applicability"] = ["ДВС-1|other"]
        self.evidence[1]["page"] = 3
        answer = self.session.ask("V1", "Как измерить зазор?")
        self.assertEqual(answer["sources"], [])
        self.assertEqual(answer["inspection_steps"], [])
        self.assertNotIn("0,3 мм", answer["text"])
        self.assertIn("страница 3", " ".join(answer["warnings"]))

    def test_general_diagnosis_question_uses_pump_inspection_from_both_pages(self):
        hits = [
            dict(self.evidence[0], section="Масляный насос", text=(
                "Падение давления в системе смазки может быть вызвано износом деталей "
                "масляного насоса или подшипников коленчатого и распределительного валов. "
                "При значительных износах деталей насоса он начинает работать шумно. "
                "Чтобы выявить неисправности насоса, его необходимо снять с двигателя и разобрать.")),
            dict(self.evidence[1], section="Разборка насоса", work=None, text=(
                "Проверить износ шестерен, замерив их высоту. "
                "Новые шестерни имеют размер высоты 40,075…40,100 мм. "
                "При значительном износе заменить дефектную шестерню.")),
        ]

        class Search:
            def search(self, engine, query, *, variant, limit):
                return hits

        session = ManualSession(root=self.root, search=Search())
        answer = session.ask("V1", "Что проверить и чем опасно отклонение?",
                             check_id="S1", diagnosis=self.diagnostic)
        self.assertIn("износом деталей масляного насоса", answer["text"])
        self.assertIn("40,075…40,100 мм", answer["text"])
        self.assertEqual([source["page"] for source in answer["sources"]], [1, 2])
        self.assertTrue(any(step["role"] == "work" and step["page"] == 2
                            for step in answer["inspection_steps"]))

    def test_valve_clearance_uses_requested_manual_and_one_complete_step(self):
        from ai_mechanic.knowledge.search import retrieve
        from unittest.mock import Mock
        excerpt = ("Вставив щуп в зазор между торцом клапана и носком коромысла, проверить зазоры "
                   "первого цилиндра и отрегулировать их в пределах 0,25 - 0,30 мм.")
        source = dict(self.evidence[0], text=excerpt, section="Регулировка")
        other = dict(source, title="236.01.01 РК", document_id="repair",
                     text=excerpt + " Щуп должен входить с усилием.")
        right = dict(source, title="236-3902150-Б РЭ", document_id="operation")
        result = retrieve(Mock(**{"search.return_value": [other, right]}), "ДВС-1",
                          "По руководству по эксплуатации, куда вставить щуп при регулировке клапанов "
                          "первого цилиндра и какой зазор выставить?")
        self.assertEqual([(v["document_id"], v["excerpt"]) for v in result["evidence"]], [("operation", excerpt)])

    def test_installation_torque_follows_bolt_number_within_same_step(self):
        from ai_mechanic.knowledge.search import retrieve
        from unittest.mock import Mock
        question = ("При установке термостатов какой момент затяжки болтов отводящего патрубка "
                    "на сервисном модуле?")
        operation = ("3. Установить отводящий патрубок (6) на сервисный модуль, предварительно ввернув "
                     "от руки 3 болта (7) в отверстия сервисного модуля.\n"
                     "Окончательно затянуть болты (7) ключом S=13. Момент затяжки Мкр = 20–25 Н·м.")
        hit = dict(self.evidence[0], kind="text", section="Установка термостатов", text=operation)
        wrong = dict(hit, section="Установка сервисного модуля", text=operation.replace("20–25", "60–65"))
        def answer(hits):
            return retrieve(Mock(**{"search.return_value": hits}), "ДВС-1", question)["evidence"]

        evidence = answer([wrong, hit])
        self.assertEqual(len(evidence), 1)
        self.assertIn("20–25 Н·м", evidence[0]["excerpt"])
        self.assertIn("патрубок", evidence[0]["excerpt"])
        self.assertFalse(answer([wrong]))
        self.assertFalse(answer([dict(hit, text=operation.replace("болты (7)", "болты (8)"))]))

    def test_disassembly_keeps_five_steps_under_one_citation(self):
        hit = dict(self.evidence[1], kind="text", section="Разборка двигателя", text=(
            "Разборка насоса\n"
            "1. Отвернуть две гайки крепления и снять насос. Вылить масло из насоса,\n"
            "перевернув его крышкой вверх.\n"
            "2. Закрепить корпус насоса и отвернуть пробку перепускного клапана, вы-\n"
            "нуть пружину и плунжер.\n"
            "3. Отвернуть четыре болта крепления крышки масляного насоса, снять\n"
            "крышку и перегородку с прокладками.\n"
            "4. Вынуть валик насоса с ведущей шестерней в сборе из корпуса.\n"
            "5. Вынуть ведомую шестерню насоса из корпуса.\n"
            "После разборки насоса все его детали должны быть промыты."
        ))

        class Search:
            def search(self, engine, query, *, variant, limit):
                return [hit]

        session = ManualSession(root=self.root, search=Search())
        for question in ("Как разобрать масляный насос?", "Порядок разборки масляного насоса?"):
            with self.subTest(question=question):
                answer = session.ask("V1", question, check_id="S1")
                self.assertEqual([source["page"] for source in answer["sources"]], [2])
                self.assertEqual(len(answer["text"].split("\n\n")), 1)
                steps = answer["text"].removeprefix("По руководству. ").splitlines()
                self.assertEqual([step[0] for step in steps], list("12345"))
                self.assertIn("вынуть пружину и плунжер", steps[1])
                self.assertIn("крышки масляного насоса", steps[2])
                self.assertTrue(steps[-1].endswith("[1]"))
                self.assertNotIn("После разборки", answer["text"])

    def test_disassembly_does_not_infer_a_node_or_a_missing_step(self):
        from ai_mechanic.knowledge.search import retrieve

        hit = dict(self.evidence[1], kind="text", section="Разборка двигателя")

        class Search:
            def search(self, engine, query, *, variant, limit):
                return [hit]

        for case in (
            "Разборка насоса\n1. Снять крышку топливного насоса.\nПосле разборки промыть детали.",
            "Разборка насоса\n1. Снять крышку масляного насоса.\n3. Вынуть вал.\nПосле разборки промыть детали.",
            "Разборка насоса\n1. Снять крышку масляного насоса.\n2. Вынуть вал.",
        ):
            with self.subTest(case=case):
                hit["text"] = case
                result = retrieve(Search(), "ДВС-1", "Как разобрать масляный насос?")
                self.assertNotEqual(result["topic"], "disassembly")
