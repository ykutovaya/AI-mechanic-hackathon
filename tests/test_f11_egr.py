"""Проверяет первые ответы по СРОГ и РОГ и доступность подтверждённых работ."""

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


ZMZ = "ЗМЗ-5234.10"
YAMZ = "ЯМЗ-536"
Z_DOC, Y_DOC = "zmz-5234-10-rk-2025", "yamz-536-re-2016"
ROD = ("Для проверки работоспособности СРОГ необходимо увеличить на прогретом двигателе частоту вращения "
       "коленчатого вала с минимальных оборотов холостого хода до 1700 мин-1 и наблюдать визуально за "
       "перемещением штока 9, клапана рециркуляции 8. В случае отсутствия перемещения штока проверить "
       "наличие разрежения в шланге 6 от термовакуумного включателя к клапану рециркуляции 8.")
FLAP = ["3.15 Затруднена подвижность заслонки системы рециркуляции отработавших газов (РОГ)",
        "Устранить заедание, при необходимости, заменить заслонку РОГ"]
BOOST = ["3.12 Перегрев наддувочного воздуха и/или падение давления наддува", "Проверить состояние ОНВ"]
CARB = ["Двигатель неустойчиво работает на холостом ходу, подсасывание воздуха между фланцами карбюратора",
        "Подтянуть гайки крепления или заменить прокладку"]


class EgrTests(unittest.TestCase):
    cases = {ZMZ: ("M851TA178", "S-0122", Z_DOC, 53, "Неровная работа на частичных нагрузках, изменился тон впуска"),
             YAMZ: ("T976XB47", "S-0075", Y_DOC, 201, "Изменился тон впуска, дымность на переходных режимах")}

    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        raw = self.root / "data/raw"
        raw.mkdir(parents=True)
        annotations = self.root / "data/annotations"
        annotations.mkdir()
        manuals, vehicles, diagnoses = {}, [], {}
        for engine, (machine, check, doc, count, symptoms) in self.cases.items():
            path = raw / f"{doc}.pdf"
            with pymupdf.open() as pdf:
                for _ in range(count):
                    pdf.new_page()
                if engine == ZMZ:
                    page = pdf[49]
                    page.insert_font(fontname="manual", fontbuffer=pymupdf.Font("helv").buffer)
                    page.insert_textbox(page.rect + (30, 30, -30, -30), ROD, fontname="manual", fontsize=10)
                pdf.save(path)
            manuals[engine] = dict(document_id=doc, title=doc, edition="1", engine_models=[engine],
                                   path=f"data/raw/{path.name}", source_sha256=sha256(path.read_bytes()).hexdigest(),
                                   page_count=count)
            vehicles.append(dict(vehicle_id=machine, model="Машина", engine_model=engine,
                                 sessions=[dict(session_id=check, vehicle_id=machine, status="anomaly")]))
            diagnoses[machine] = dict(vehicle_id=machine, session_id=check, status="anomaly", anomaly_type="F11",
                                     anomaly_title="Рециркуляция отработавших газов", description=symptoms,
                                     signal_quality="good", confidence=.8, anomaly_score=.7, model_version="test")
        (raw / "vehicles.json").write_text(json.dumps({"vehicles": vehicles}), encoding="utf-8")
        (annotations / "manuals.json").write_text(json.dumps(list(manuals.values())), encoding="utf-8")

        def table(engine, page, rows):
            return dict(manuals[engine], page=page, kind="table", applicability=[engine + "|all"],
                        tables=[dict(rows=[["Причины неисправности", "Способ устранения"], *rows])],
                        text="\n".join(" | ".join(row) for row in rows))

        self.hits = {ZMZ: [table(ZMZ, 53, [CARB])],
                     YAMZ: [table(YAMZ, 200, [BOOST]), table(YAMZ, 201, [FLAP, BOOST])]}
        self.entries = [dict(manuals[ZMZ], row=551, source_pages=[50], level="reading", applicability=[ZMZ+"|all"],
                             section="Система рециркуляции отработавших газов — проверка")]
        fixture = self

        class Backend:
            def search(self, engine, query, *, variant, limit):
                return fixture.hits[engine]

            def search_fault(self, engine, query, *, code, variant, limit):
                return fixture.hits[engine]

            def reference_entries(self, engine, code, *, variant):
                return fixture.entries if engine == ZMZ and code == "F11" else []

        self.enterContext(patch.object(web, "fetch_diagnosis", side_effect=lambda url, vehicle, check: diagnoses[vehicle]))
        self.url = self.enterContext(running(web.make_server(("127.0.0.1", 0), root=self.root, search=Backend(),
                                                            orders_db=self.root / "orders.sqlite")))

    def post(self, path, body):
        req = Request(self.url+path, data=json.dumps(body).encode(), headers={"Content-Type":"application/json"})
        with urlopen(req, timeout=8) as resp:
            return json.load(resp)

    def diagnose(self, engine):
        machine, check, *_ = self.cases[engine]
        return self.post("/api/diagnose", dict(machine_id=machine, check_id=check))

    def assert_draft(self, engine, reply, page):
        self.assertEqual([ref["page"] for ref in reply["sources"]], [page])
        draft = self.post("/api/orders/draft", dict(context_id=reply["context_id"]))["payload"]
        self.assertEqual(draft["parts"], [])
        self.assertEqual([ref["page"] for ref in draft["sources"]], [page])
        self.assertEqual(len(draft["works"]), 1)
        work = draft["works"][0]
        machine, check, *_ = self.cases[engine]
        repeated = self.post("/api/ask", dict(machine_id=machine, check_id=check, question=work))
        self.assertEqual([ref["page"] for ref in repeated["sources"]], [page])
        return work

    def test_zmz_uses_native_procedure_from_expert_reference_when_missing_in_index(self):
        reply = self.diagnose(ZMZ)
        for phrase in ("прогрет", "холостого хода", "1700", "шток", "разрежени"):
            self.assertIn(phrase, reply["text"].lower())
        self.assertNotIn("карбюратор", reply["text"].lower())
        self.assertIn("шток", self.assert_draft(ZMZ, reply, 50).lower())

    def test_yamz_uses_flap_row_independently_of_neighbouring_boost_rows(self):
        reply = self.diagnose(YAMZ)
        self.assertIn("3.15", reply["text"])
        self.assertNotIn("наддув", reply["text"].lower())
        work = self.assert_draft(YAMZ, reply, 201).lower()
        self.assertIn("подвижность заслонки", work)
        self.assertNotIn("замен", work)

    def test_unavailable_or_inapplicable_sources_cannot_support_an_order(self):
        self.hits[YAMZ] = self.hits[YAMZ][:1]
        entry = self.entries[0]
        for engine, entries in [(YAMZ, []), (ZMZ, []), (ZMZ, [dict(entry, source_sha256="0"*64)]),
                                (ZMZ, [dict(entry, applicability=[ZMZ+"|другая комплектация"])]),
                                (ZMZ, [dict(entry, source_pages=[51])])]:
            with self.subTest(engine=engine, entries=entries):
                self.entries = entries
                reply = self.diagnose(engine)
                self.assertFalse(reply.get("context_id"))
                self.assertEqual(reply["sources"], [])
        self.entries = [entry]
        (self.root / entry["path"]).unlink()
        reply = self.diagnose(ZMZ)
        self.assertFalse(reply.get("context_id"))
        self.assertEqual(reply["sources"], [])
