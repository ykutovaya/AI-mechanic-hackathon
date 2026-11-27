"""Проверяет первый ответ и заявку по пропускам зажигания в одном цилиндре."""

from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pymupdf

from ai_mechanic.web import Workshop


class IgnitionFlowTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        raw = root / "data/raw"
        raw.mkdir(parents=True)
        engine = "ЗМЗ-40904.10"
        self.car = dict(machine_id="E622KM47", check_id="S-0195")
        card = dict(vehicle_id="E622KM47", model="УАЗ", engine_model=engine, fuel="бензин",
                    sessions=[dict(session_id="S-0195")])
        (raw / "vehicles.json").write_text(json.dumps({"vehicles": [card]}), encoding="utf-8")
        pdf = raw / "manual.pdf"
        with pymupdf.open() as guide:
            for _ in range(103):
                guide.new_page()
            guide.save(pdf)
        manual = dict(document_id="zmz-40904-40905-rk-2025", title="Руководство ЗМЗ", edition="2025",
                      path="data/raw/manual.pdf", engine_models=[engine], page_count=103,
                      source_sha256=sha256(pdf.read_bytes()).hexdigest())
        notes = root / "data/annotations"
        notes.mkdir()
        (notes / "manuals.json").write_text(json.dumps([manual]), encoding="utf-8")
        self.marker = "Посторонняя пометка для редактора"
        self.symptoms = "Пропуски воспламенения в одном цилиндре, неровный холостой ход"
        self.diagnostic = dict(vehicle_id="E622KM47", session_id="S-0195", anomaly_type="F12",
                               anomaly_title="Зажигание", status="anomaly", signal_quality="good",
                               confidence=0.72, description=self.symptoms + ". " + self.marker + ".")
        def fragment(section, cause, action):
            return dict(manual, page=103, applicability=[engine + "|all"], section=section,
                        text=f"{section}\n{cause} | {action}", tables=[{"rows": [
                            ["Вероятная причина", "Способ устранения"], [cause, action]]}])
        section = "2.1. Перебои или отказ в работе одного из цилиндров двигателя"
        self.hits = [
            fragment("3. Повышенная частота вращения", "нарушение контакта дроссельного модуля",
                     "проверить разъем, заменить дроссельный модуль"),
            fragment(section, "в) не работает свеча зажигания", "заменить неисправную свечу"),
            fragment(section, "а) нарушение или загрязнение контактов в системе зажигания;",
                     "устранить загрязнение или неплотности контактов"),
        ]
        self.queries = []
        fixture = self

        class Search:
            def search(self, engine, query, *, variant, limit):
                fixture.queries.append(query)
                return fixture.hits

            def search_fault(self, engine, query, *, code, variant, limit):
                return self.search(engine, query, variant=variant, limit=limit)

        self.enterContext(patch("ai_mechanic.web.fetch_diagnosis", return_value=self.diagnostic))
        self.workshop = Workshop(root, "data", Search(), None, None, "orders.sqlite")

    def test_show_only_reported_symptoms_in_first_answer_search_and_followup(self):
        initial = self.workshop.answer("/api/diagnose", self.car)
        followup = self.workshop.answer("/api/ask", self.car | {"question": "Что проверить?"})
        for reply in (initial, followup):
            self.assertNotIn(self.marker, json.dumps(reply, ensure_ascii=False))
            self.assertIn("пропуски воспламенения в одном цилиндре", reply["text"].casefold())
            self.assertIn("неровный холостой ход", reply["text"].casefold())
        self.assertTrue(all(self.marker not in query for query in self.queries))

    def test_draft_links_to_contacts_in_21_and_excludes_conditional_replacement(self):
        reply = self.workshop.answer("/api/diagnose", self.car)
        self.assertEqual([s["page"] for s in reply["sources"]], [103])
        self.assertIn("2.1", reply["text"])
        self.assertNotRegex(reply["text"].lower(), r"заменить|дроссельн")
        self.assertTrue(reply["context_id"])
        payload = self.workshop.order("/api/orders/draft", {"context_id": reply["context_id"]})["payload"]
        self.assertEqual(payload["parts"], [])
        work, = payload["works"]
        self.assertRegex(work, r"Проверить.*контакт.*зажигания")
        self.assertEqual([s["page"] for s in payload["sources"]], [103])
        self.assertNotIn(self.marker, json.dumps(payload, ensure_ascii=False))
        self.assertEqual([s["page"] for s in self.workshop._verify_works(
            "E622KM47", "S-0195", [work], payload["sources"])], [103])

    def test_cylinder_number_and_idle_are_preserved_when_ml_adds_a_service_note(self):
        self.diagnostic["description"] = "Пропуски зажигания в цилиндре № 3; неустойчивый холостой ход. " + self.marker
        reply = self.workshop.answer("/api/diagnose", self.car)
        self.assertIn("цилиндре № 3", reply["diagnosis"]["description"])
        self.assertIn("неустойчивый холостой ход", reply["text"])
        self.assertNotIn(self.marker, json.dumps(reply, ensure_ascii=False))
        self.assertTrue(reply["context_id"])

    def test_followup_about_ignition_uses_the_reported_misfires(self):
        self.workshop.answer("/api/diagnose", self.car)
        reply = self.workshop.answer("/api/ask", self.car | {
            "question": "Как проверить систему зажигания по результату этой диагностики?"})
        self.assertEqual([s["page"] for s in reply["sources"]], [103])
        self.assertIn("2.1", reply["text"])

    def test_neighboring_row_and_unconfirmed_symptoms_do_not_authorize_a_draft(self):
        contacts = self.hits.pop()
        reply = self.workshop.answer("/api/diagnose", self.car)
        self.assertFalse(reply["context_id"])
        self.assertFalse(reply["sources"])
        self.hits.append(contacts)
        self.diagnostic["description"] = "Пропусков в одном цилиндре не обнаружено. " + self.marker
        reply = self.workshop.answer("/api/diagnose", self.car)
        self.assertNotIn(self.marker, json.dumps(reply, ensure_ascii=False))
        self.assertNotIn("Пропуски воспламенения в одном цилиндре", reply["text"])
        self.assertFalse(reply["context_id"])
