"""Проверяет ответ веб-чата о сигнализаторе низкого давления масла."""

from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

import pymupdf

from ai_mechanic import web
from test_web_flow import running


class OilPressureWarningTests(unittest.TestCase):
    def test_diagnosis_and_follow_up_keep_the_full_warning_from_page_91(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        engine = "ЗМЗ-40904.10"
        machine, check = "M199CH98", "S-0189"
        document = "zmz-40904-40905-rk-2025"
        warning = (
            "Не допускается эксплуатация двигателя с горящим сигнализатором аварийного давления масла. "
            "Это приведет к задиру подшипников коленчатого вала, распределительных валов и выходу из строя "
            "газораспределительного механизма. Загорание сигнализатора допускается при работе двигателя "
            "на минимальной частоте вращения в режиме холостого хода и при резком торможении."
        )
        vehicles = {"vehicles": [{"vehicle_id": machine, "model": "Машина", "engine_model": engine,
                                  "sessions": [{"session_id": check, "vehicle_id": machine, "status": "anomaly"}]}]}
        raw = root / "data" / "raw"
        raw.mkdir(parents=True)
        (raw / "vehicles.json").write_text(json.dumps(vehicles, ensure_ascii=False), encoding="utf-8")
        pdf = raw / "manual.pdf"
        with pymupdf.open() as guide:
            for _ in range(91):
                guide.new_page()
            guide.save(pdf)
        manual = dict(document_id=document, title="Руководство по ремонту ЗМЗ-40904.10 и ЗМЗ-40905.10",
                      edition="2025", path="data/raw/manual.pdf", engine_models=[engine, "ЗМЗ-40905.10"],
                      page_count=91, source_sha256=sha256(pdf.read_bytes()).hexdigest())
        annotations = root / "data" / "annotations"
        annotations.mkdir()
        (annotations / "manuals.json").write_text(json.dumps([manual], ensure_ascii=False), encoding="utf-8")
        fragment = dict(manual, page=91, kind="paragraph", section="Эксплуатация двигателя. Предупреждения",
                        text=warning, applicability=[engine + "|all"], fault_code="F02", expert_rows=[149])

        class Search:
            def search(self, engine, query, *, variant, limit):
                return []

            def search_fault(self, engine, query, *, code, variant, limit):
                if code == fragment["fault_code"] and (engine + "|all") in fragment["applicability"]:
                    return [fragment]
                return []

        diagnosis = dict(vehicle_id=machine, session_id=check, status="anomaly", anomaly_type="F02",
                         anomaly_title="Низкое давление масла", anomaly_score=0.8, confidence=0.91,
                         signal_quality="good", model_version="test",
                         description="Сигнализатор аварийного давления масла горит на прогретом двигателе, "
                                     "шум привода клапанов усилился")
        fetch = self.enterContext(patch.object(web, "fetch_diagnosis", return_value=diagnosis))
        url = self.enterContext(running(web.make_server(
            ("127.0.0.1", 0), root=root, search=Search(), orders_db=root / "orders.sqlite")))

        def request(route, body):
            post = Request(url + route, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                           headers={"Content-Type": "application/json"})
            with urlopen(post, timeout=5) as response:
                return json.load(response)

        selection = dict(machine_id=machine, check_id=check)
        initial = request("/api/diagnose", selection)
        follow_up = request("/api/ask", selection | {"question": "Что проверить в первую очередь?",
                                                       "topic": initial["topic"]})
        fetch.assert_called_once()
        for reply in (initial, follow_up):
            with self.subTest(queries=reply["queries"]):
                answer = reply["text"].lower()
                self.assertIn("горит на прогретом двигателе", answer)
                self.assertIn("не допускается эксплуатация двигателя", answer)
                self.assertIn("задир", answer)
                self.assertIn("минимальной частоте вращения", answer)
                self.assertIn("холостого хода", answer)
                self.assertIn("при резком торможении", answer)
                self.assertRegex(answer, r"(?s)фактическ\w* давлени\w*.{0,100}"
                                          r"(?:неизвест|не измер|не указ|определить нельзя|не подтверж)")
                self.assertRegex(answer, r"уточн\w*[^.\n]*?(?:режим|оборот|холост)")
                source = next((source for source in reply["sources"]
                               if source["document_id"] == document and source["page"] == 91), None)
                self.assertIsNotNone(source)
                self.assertEqual(source["fault_code"], "F02")
                self.assertEqual(source["expert_rows"], [149])
                self.assertEqual(source["url"], f"/manuals/{document}.pdf#page=91")
                with urlopen(url + source["page_url"], timeout=5) as page:
                    self.assertIn("Страница 91 в PDF", page.read().decode("utf-8"))
        self.assertFalse(initial["context_id"])
