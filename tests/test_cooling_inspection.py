"""Проверяет ответы и черновики F03 при перегреве ЗМЗ-40905.10 и ЯМЗ-536."""

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


class CoolingInspectionFlowTests(unittest.TestCase):
    ZMZ = "ЗМЗ-40905.10"
    YAMZ = "ЯМЗ-536"
    CASES = {
        ZMZ: ("B332BP98", "S-0159", "zmz-40904-40905-rk-2025", 105,
              "Стук подшипника в передней части двигателя, температура растёт на холостом ходу; "
              "свист ремня привода сохраняется"),
        YAMZ: ("O727CA198", "S-0051", "yamz-536-re-2016", 220,
               "Вентилятор не включается при росте температуры, слышен свист ремня"),
    }

    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        raw = root / "data" / "raw"
        raw.mkdir(parents=True)
        cards, manuals, diagnoses = [], {}, {}
        for engine, (vehicle, check, doc, count, symptoms) in self.CASES.items():
            cards.append(dict(vehicle_id=vehicle, model="Машина", engine_model=engine,
                              sessions=[dict(session_id=check, vehicle_id=vehicle, status="anomaly")]))
            pdf = raw / (doc + ".pdf")
            with pymupdf.open() as guide:
                for _ in range(count):
                    guide.new_page()
                guide.save(pdf)
            manuals[engine] = dict(
                document_id=doc, title=f"Руководство для {engine}", edition="2025",
                path=f"data/raw/{pdf.name}", engine_models=[engine], page_count=count,
                source_sha256=sha256(pdf.read_bytes()).hexdigest())
            diagnoses[vehicle] = dict(
                vehicle_id=vehicle, session_id=check, status="anomaly", anomaly_type="F03",
                anomaly_title="Перегрев, охлаждающая жидкость в масле", description=symptoms,
                signal_quality="warning" if engine == self.YAMZ else "good",
                anomaly_score=0.82, confidence=0.65 if engine == self.YAMZ else 0.88, model_version="test")
        (raw / "vehicles.json").write_text(json.dumps({"vehicles": cards}, ensure_ascii=False), encoding="utf-8")
        annotations = root / "data" / "annotations"
        annotations.mkdir()
        (annotations / "manuals.json").write_text(json.dumps(list(manuals.values()), ensure_ascii=False), encoding="utf-8")

        def fragment(engine, page, text, **fields):
            return dict(manuals[engine], page=page, text=text, applicability=[engine + "|all"], **fields)

        def table(engine, page, rows):
            text = "\n".join(" | ".join(cell for cell in row if cell) for row in rows)
            return fragment(engine, page, text, kind="table", tables=[{"rows": rows}],
                            fault_code="F03", expert_rows=[page])

        warning = fragment(
            self.ZMZ, 91, "Не допускайте перегрева двигателя. При загорании сигнализатора перегрева "
                          "охлаждающей жидкости следует немедленно остановить двигатель, выявить и устранить причину перегрева.")
        belt = fragment(self.ZMZ, 98,
                        "Проверку состояния ремней привода вспомогательных агрегатов и подшипников натяжного и "
                        "направляющих роликов производить при каждом техническом обслуживании. Состояние ремней "
                        "проверять визуально. При растрескивании, расслоении, обнажении корда и других дефектах ремень подлежит замене.",
                        fault_code="F03", expert_rows=[232, 256])
        replacement = table(
            self.ZMZ, 105, [
                ["Причина", "Способ устранения"], ["Двигатель перегревается", None],
                ["Проскальзывание ремня привода агрегатов вследствие предельной вытяжки ремня или поломки "
                 "автоматического механизма натяжения", "Заменить ремень или автоматический механизм натяжения ремня"],
            ])
        fan = table(
            self.YAMZ, 220, [
                ["16.4 Вытянулся или порвался ремень привода вентилятора или водяного насоса", "Заменить ремень"],
                ["16.5 Не включается или неисправен вентилятор с муфтой включения (в т.ч. устанавливаемый "
                 "потребителем). Отмечаются подтеки вязкостной жидкости",
                 "Проверить состояние муфты вентилятора, при необходимости, заменить"],
                ["16.6 Неисправна система управления вентилятором при установке на ТС вентилятора с электроуправляемой "
                 "муфтой", "Подключить диагностический прибор и проверить наличие ошибок в ЭСУ."],
            ])
        noise = table(
            self.YAMZ, 215, [
                ["Причины неисправности", "Способ устранения"],
                ["11.5 Неисправны или установлены неправильно: привод вентилятора, муфта вентилятора, генератор. "
                 "Слышен свист ремня", "Проверить исправность и правильность установки агрегата. "
                                      "Снять ремень привода агрегата, заменить отказавший агрегат."],
            ])
        self.hits = {self.ZMZ: [warning, replacement], self.YAMZ: [noise, fan]}
        self.fault_hits = {self.ZMZ: [replacement, belt], self.YAMZ: [noise, fan]}
        fixture = self

        class Search:
            def search(self, engine, query, *, variant, limit):
                return fixture.hits[engine]

            def search_fault(self, engine, query, *, code, variant, limit):
                return fixture.fault_hits[engine] if code == "F03" else []

        self.enterContext(patch.object(web, "fetch_diagnosis",
                                       side_effect=lambda _url, vehicle, _check: diagnoses[vehicle]))
        self.url = self.enterContext(running(web.make_server(
            ("127.0.0.1", 0), root=root, search=Search(), orders_db=root / "orders.sqlite")))

    def post(self, path, body):
        request = Request(self.url + path, data=json.dumps(body, ensure_ascii=False).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def selection(self, engine):
        vehicle, check, *_ = self.CASES[engine]
        return dict(machine_id=vehicle, check_id=check)

    def test_zmz_diagnosis_keeps_overheat_warning_and_checks_belt_and_rollers(self):
        reply = self.post("/api/diagnose", self.selection(self.ZMZ))
        self.assertIn("При загорании сигнализатора перегрева охлаждающей жидкости", reply["text"])
        self.assertIn("немедленно остановить двигатель", reply["text"])
        self.assertIn("Визуально проверить состояние ремней", reply["text"])
        self.assertIn("подшипники натяжного и направляющих роликов", reply["text"])
        self.assertIn("попадание охлаждающей жидкости в масло", reply["text"])
        self.assertNotIn("Заменить ремень", reply["text"])
        self.assertEqual([ref["page"] for ref in reply["sources"]], [91, 98])
        draft = self.post("/api/orders/draft", {"context_id": reply["context_id"]})["payload"]
        self.assertEqual(draft["parts"], [])
        self.assertEqual([ref["page"] for ref in draft["sources"]], [98])
        self.assertIn("подшипники натяжного и направляющих роликов", " ".join(draft["works"]))
        self.assertNotIn("заменить", " ".join(draft["works"]).lower())

    def test_yamz_checks_fan_with_unknown_clutch_type_and_links_page_220(self):
        case = self.selection(self.YAMZ)
        initial = self.post("/api/diagnose", case)
        follow_up = self.post("/api/ask", case | {"question": "Как проверить работу вентилятора?"})
        for reply in (initial, follow_up):
            self.assertFalse(reply.get("engine_variant"))
            self.assertFalse(reply.get("needs_clarification"))
            self.assertIn("Проверить работу вентилятора", reply["text"])
            self.assertIn("При наличии муфты включения проверить её состояние", reply["text"])
            self.assertNotRegex(reply["text"], r"ЭСУ|[Сс]нять ремень|[Зз]аменить")
            self.assertEqual([ref["page"] for ref in reply["sources"]], [220])
            self.assertEqual(reply["sources"][0]["url"], "/manuals/yamz-536-re-2016.pdf#page=220")
        with urlopen(self.url + initial["sources"][0]["page_url"], timeout=5) as page:
            self.assertIn("Страница 220 в PDF", page.read().decode())
        draft = self.post("/api/orders/draft", {"context_id": initial["context_id"]})["payload"]
        self.assertEqual(draft["parts"], [])
        self.assertEqual([ref["page"] for ref in draft["sources"]], [220])
        self.assertIn("Проверить работу вентилятора", " ".join(draft["works"]))
        self.assertNotRegex(" ".join(draft["works"]), r"ЭСУ|[Сс]нять ремень|[Зз]аменить")

    def test_yamz_without_fan_procedure_does_not_offer_unrelated_replacement(self):
        self.hits[self.YAMZ].pop()
        self.fault_hits[self.YAMZ].pop()
        reply = self.post("/api/diagnose", self.selection(self.YAMZ))
        self.assertIn("проверки работы вентилятора", reply["text"])
        self.assertFalse(reply.get("context_id"))
        self.assertFalse(reply["sources"])

