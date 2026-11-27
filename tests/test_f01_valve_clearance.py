"""Проверяет первый ответ и черновик по клапанным зазорам ЗМЗ-5234.10."""

import json
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

import pymupdf

from ai_mechanic import web
from test_web_flow import running


class ValveClearanceFlowTests(unittest.TestCase):
    ENGINE = "ЗМЗ-5234.10"
    DOCUMENT = "zmz-5234-10-rk-2025"
    SELECTION = {"machine_id": "M739BA198", "check_id": "S-0103"}
    PROCEDURE = (
        "4. Медленно поворачивать коленчатый вал до совпадения риски на шкиве\n"
        "коленчатого вала со средним выступом на крышке распределительных шестерен.\n"
        "При этом положении впускной и выпускной клапаны первого цилиндра полно-\n"
        "стью закрыты.\n"
        "5. Проверить зазор между коромыслом и стержнем клапана с помощью щу-\n"
        "па. Зазор на холодном двигателе должен быть 0,20…0,30 мм. Если на прогретом\n"
        "двигателе наблюдается непрерывный стук клапанов, расположенных по краям го-\n"
        "ловок, то в этом случае у этих клапанов (впускных клапанов первого и восьмого\n"
        "цилиндров, выпускных клапанов четвертого и пятого цилиндров) допускается\n"
        "уменьшить зазор до 0,15…0,20 мм на холодном двигателе."
    )
    REPAIR = "Произвести ремонт двигателя"

    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        raw = root / "data" / "raw"
        raw.mkdir(parents=True)
        vehicle, check = self.SELECTION.values()
        card = dict(vehicle_id=vehicle, model="Машина", engine_model=self.ENGINE, fuel="бензин",
                    sessions=[dict(session_id=check, vehicle_id=vehicle, status="anomaly")])
        (raw / "vehicles.json").write_text(json.dumps({"vehicles": [card]}, ensure_ascii=False), encoding="utf-8")
        pdf = raw / "manual.pdf"
        with pymupdf.open() as guide:
            for _ in range(56):
                guide.new_page()
            guide.save(pdf)
        manual = dict(document_id=self.DOCUMENT, title="Руководство по ремонту ЗМЗ-5234.10", edition="2025",
                      path="data/raw/manual.pdf", engine_models=[self.ENGINE], page_count=56,
                      source_sha256=sha256(pdf.read_bytes()).hexdigest())
        annotations = root / "data" / "annotations"
        annotations.mkdir()
        (annotations / "manuals.json").write_text(json.dumps([manual], ensure_ascii=False), encoding="utf-8")

        clearance = dict(manual, page=21, text=self.PROCEDURE, kind="paragraph",
                         section="Регулировка зазоров между клапанами и коромыслами",
                         applicability=[self.ENGINE + "|all"], expert_rows=[49, 50], fault_code="F01")
        cause = "Сильный износ поршней, цилиндров, втулок шатунов, поршневых пальцев"
        table = [["Причина неисправности", "Метод устранения"], ["Стуки в двигателе", None],
                 [cause, self.REPAIR]]
        wear = dict(manual, page=56, text=f"Стуки в двигателе. {cause}. {self.REPAIR}",
                    kind="table", tables=[{"rows": table}], section="Возможные неисправности",
                    applicability=[self.ENGINE + "|all"], expert_rows=[52], fault_code="F01")
        self.hits = [clearance, wear]
        hits = self.hits

        class Search:
            def search(self, engine, query, *, variant, limit):
                return hits

            def search_fault(self, engine, query, *, code, variant, limit):
                return hits if code == "F01" else []

        diagnosis = dict(vehicle_id=vehicle, session_id=check, status="anomaly", anomaly_type="F01",
                         anomaly_title="Клапанный механизм", signal_quality="warning",
                         description="Равномерный стук клапанов под крышками головок, сильнее у крайних цилиндров",
                         anomaly_score=0.8, confidence=0.48, model_version="test")
        self.enterContext(patch.object(web, "fetch_diagnosis", return_value=diagnosis))
        self.url = self.enterContext(running(web.make_server(
            ("127.0.0.1", 0), root=root, search=Search(), orders_db=root / "orders.sqlite")))

    def post(self, path, body):
        req = Request(self.url + path, data=json.dumps(body, ensure_ascii=False).encode(),
                      headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=5) as response:
            return json.load(response)

    def test_f01_checks_clearance_with_page_21_in_first_answer_and_draft(self):
        initial = self.post("/api/diagnose", self.SELECTION)
        follow_up = self.post("/api/ask", self.SELECTION | {
            "question": "Куда вставить щуп и какой зазор должен быть у клапанов?"})

        for reply in (initial, follow_up):
            with self.subTest(queries=reply["queries"]):
                answer = reply["text"]
                self.assertIn("между коромыслом и стержнем клапана с помощью щупа", answer)
                self.assertIn("на холодном двигателе должен быть 0,20…0,30 мм", answer)
                self.assertIn("клапаны первого цилиндра полностью закрыты", answer)
                self.assertRegex(answer, r"Если на прогретом двигателе наблюдается непрерывный стук.*"
                                         r"впускных клапанов первого и восьмого цилиндров, "
                                         r"выпускных клапанов четвертого и пятого цилиндров\).*0,15…0,20 мм")
                self.assertIn("Уверенность ML в предполагаемой неисправности низкая", answer)
                self.assertNotIn(self.REPAIR, answer)
                source, = reply["sources"]
                self.assertEqual((source["document_id"], source["page"]), (self.DOCUMENT, 21))
                self.assertEqual(source["url"], f"/manuals/{self.DOCUMENT}.pdf#page=21")
        with urlopen(self.url + initial["sources"][0]["page_url"], timeout=5) as source_page:
            self.assertIn("Страница 21 в PDF", source_page.read().decode())

        self.assertTrue(initial.get("context_id"))
        draft = self.post("/api/orders/draft", {"context_id": initial["context_id"]})["payload"]
        work, = draft["works"]
        self.assertIn("проверить тепловые зазоры клапанов", work.lower())
        self.assertIn("на холодном двигателе", work.lower())
        self.assertEqual(draft["parts"], [])
        self.assertEqual([(s["document_id"], s["page"]) for s in draft["sources"]], [(self.DOCUMENT, 21)])

    def test_f01_without_clearance_procedure_gives_no_unsupported_repair_or_draft(self):
        self.hits.pop(0)
        reply = self.post("/api/diagnose", self.SELECTION)
        self.assertNotIn(self.REPAIR, reply["text"])
        self.assertIn("проверки клапанных зазоров", reply["text"])
        self.assertFalse(reply["sources"])
        self.assertFalse(reply.get("context_id"))

