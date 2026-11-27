"""Проверяет ответ веб-чата для кодов F08, F09, F11, F12 и F13 с учётом карточки машины."""

from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

import pymupdf

from ai_mechanic import ask, web
from test_web_flow import running


class FaultApplicabilityTests(unittest.TestCase):
    CASES = {
        "C933KX98": ("S-0163", "ЗМЗ-40905.10"),
        "C808CM178": ("S-0144", "ЗМЗ-5234.10"),
        "C206CC178": ("S-0059", "ЯМЗ-536"),
        "fixture-vehicle": ("fixture-check", "ЗМЗ-5234.10"),
    }
    EGR_CASES = {
        "T801PO178": ("S-0046", "ЯМЗ-236М2", {"fuel": "дизель"}),
    }
    FUEL_CASES = {
        "M260EH47": ("S-0079", "ЯМЗ-536", {"fuel": "дизель"}),
        "P227BB78": ("S-0118", "ЗМЗ-5234.10", {"fuel": "бензин"}),
        "fixture-yamz-no-fuel": ("fixture-check", "ЯМЗ-536", {}),
        "fixture-yamz-with-gasoline": ("fixture-check", "ЯМЗ-536", {"fuel": "бензин"}),
        "fixture-unknown-fuel": ("fixture-check", "ДВС-ТЕСТ", {}),
    }
    DIESEL_SUPPLY_CASES = {
        "Y652CK98": ("S-0132", "ЗМЗ-5234.10", {"fuel": "бензин"}),
        "fixture-zmz-no-fuel": ("fixture-check", "ЗМЗ-5234.10", {}),
        "M333YK47": ("S-0005", "ЯМЗ-236М2", {"fuel": "дизель"}),
        "C935KO47": ("S-0090", "ЯМЗ-536", {"fuel": "дизель"}),
    }
    IGNITION_CASES = {
        "K354KC98": ("S-0087", "ЯМЗ-536", {"fuel": "дизель"}),
        "fixture-ignition-no-fuel": ("fixture-check", "ЯМЗ-536", {}),
        "fixture-ignition-diesel": ("fixture-check", "ЯМЗ-236М2", {"fuel": "Дизель"}),
        "E622KM47": ("S-0195", "ЗМЗ-40904.10", {"fuel": "бензин"}),
    }
    IGNITION_MESSAGE = (
        "ML выдал код F12 — зажигание. В карточке машины указан дизельный двигатель. "
        "Сверьте модель двигателя и убедитесь, что запись сделана на этой машине. Затем повторите проверку"
    )
    DIESEL_SUPPLY_MESSAGE = (
        "ML выдал код F08 — дизельная топливоподача. В карточке машины указан бензиновый двигатель. "
        "Сверьте модель двигателя и убедитесь, что запись сделана на этой машине. Затем повторите проверку"
    )
    FUEL_MESSAGE = (
        "ML выдал код F13 — подача бензина. В карточке машины указан дизельный двигатель. "
        "Сверьте модель двигателя и убедитесь, что запись сделана на этой машине. Затем повторите проверку"
    )
    MESSAGE = ("ML выдал код F09. По руководству не удалось подтвердить, что этот код применим "
               "к выбранному двигателю. Сверьте модель двигателя и убедитесь, что запись сделана "
               "на этой машине. Затем повторите проверку")

    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        raw = root / "data" / "raw"
        raw.mkdir(parents=True)
        vehicles = [
            dict(vehicle_id=machine, model="Машина", engine_model=engine,
                 sessions=[dict(session_id=check, vehicle_id=machine, status="anomaly"),
                           dict(session_id="bad", vehicle_id=machine, status="anomaly")])
            for machine, (check, engine) in self.CASES.items()
        ]
        vehicles.extend(
            dict(vehicle_id=machine, model="Машина", engine_model=engine, **fields,
                 sessions=[dict(session_id=check, vehicle_id=machine, status="anomaly"),
                           dict(session_id="bad", vehicle_id=machine, status="anomaly")])
            for machine, (check, engine, fields) in (
                self.FUEL_CASES | self.DIESEL_SUPPLY_CASES | self.IGNITION_CASES | self.EGR_CASES
            ).items()
        )
        (raw / "vehicles.json").write_text(json.dumps({"vehicles": vehicles}), encoding="utf-8")
        pdf = raw / "manual.pdf"
        with pymupdf.open() as guide:
            guide.new_page()
            guide.save(pdf)
        manual = dict(document_id="guide", title="Руководство", edition="2025", path="data/raw/manual.pdf",
                      engine_models=list(dict.fromkeys(vehicle["engine_model"] for vehicle in vehicles)), page_count=1,
                      source_sha256=sha256(pdf.read_bytes()).hexdigest())
        annotations = root / "data" / "annotations"
        annotations.mkdir()
        manuals = [manual]
        self.factory_layouts = {}
        for code, engine, document, pages in (
                ("F09", "ЗМЗ-40905.10", "zmz-40904-40905-rk-2025", (64, 65)),
                ("F09", "ЗМЗ-5234.10", "zmz-5234-10-rk-2025", (30, 84)),
                ("F11", "ЯМЗ-236М2", "yamz-236m2-re-2010", (43,)),
                ("F11", "ЯМЗ-236М2", "yamz-236m2-rk-2014", (180, 221))):
            verified = raw / f"{document}.pdf"
            with pymupdf.open() as guide:
                for _ in range(max(pages)):
                    guide.new_page()
                guide.save(verified)
            digest = sha256(verified.read_bytes()).hexdigest()
            self.factory_layouts.setdefault((code, engine), []).append((document, digest, pages))
            manuals.append(dict(document_id=document, title=engine, edition="2025",
                                path=verified.relative_to(root).as_posix(), engine_models=[engine],
                                page_count=max(pages), source_sha256=digest))
        (annotations / "manuals.json").write_text(json.dumps(manuals), encoding="utf-8")

        oil = ("Контроль за давлением масла в системе смазки осуществляется с помощью "
               "датчика указателя давления масла.")
        turbo = ("Проверить крепление турбокомпрессора при свисте и подвывании. "
                 "Осмотреть турбокомпрессор на наличие повреждений.")
        carburetor = ("Проверить дроссель и карбюратор, если двигатель чихает при резком открытии дросселя "
                      "и обороты холостого хода плавают.")
        diesel_supply = "Проверить форсунки при жёстком стуке в цилиндрах и дымном выхлопе."
        self.fault_lookups = []
        fault_lookups = self.fault_lookups

        class Search:
            def search(self, engine, query, *, variant, limit):
                return [dict(manual, page=1, text=oil, applicability=[f"{engine}|all"])]

            def search_fault(self, engine, query, *, code, variant, limit):
                fault_lookups.append((engine, code))
                if engine == "ЯМЗ-536" and code == "F09":
                    return [dict(manual, page=1, text=turbo, applicability=["ЯМЗ-536|all"],
                                 fault_code="F09", expert_rows=[498])]
                if engine == "ЗМЗ-5234.10" and code == "F13":
                    return [dict(manual, page=1, text=carburetor, applicability=[f"{engine}|all"],
                                 fault_code="F13", expert_rows=[689])]
                if engine in {"ЯМЗ-236М2", "ЯМЗ-536"} and code == "F08":
                    return [dict(manual, page=1, text=diesel_supply, applicability=[f"{engine}|all"],
                                 fault_code="F08", expert_rows=[447])]
                return []

            def reference_entries(self, engine, code, *, variant):
                if code == "F09":
                    return [dict(manual, row=517, level="reading", section="Система выпуска",
                                 source_pages=[1], applicability=[f"{engine}|all"])]
                return []

        def diagnose(_url, vehicle, check):
            if vehicle in self.EGR_CASES:
                return dict(vehicle_id=vehicle, session_id=check, status="anomaly", anomaly_type="F11",
                            anomaly_title="Рециркуляция отработавших газов",
                            signal_quality="bad" if check == "bad" else "good",
                            anomaly_score=0.8, confidence=0.82, model_version="test",
                            description="Неровная работа, предполагаемая неисправность системы EGR")
            if vehicle in self.IGNITION_CASES:
                return dict(vehicle_id=vehicle, session_id=check, status="anomaly", anomaly_type="F12",
                            anomaly_title="Зажигание", signal_quality="bad" if check == "bad" else "good",
                            anomaly_score=0.8, confidence=0.82, model_version="test",
                            description="Зафиксированы пропуски зажигания")
            if vehicle in self.DIESEL_SUPPLY_CASES:
                return dict(vehicle_id=vehicle, session_id=check, status="anomaly", anomaly_type="F08",
                            anomaly_title="Дизельная топливоподача",
                            signal_quality="bad" if check == "bad" else "good",
                            anomaly_score=0.8, confidence=0.82, model_version="test",
                            description="Жёсткая работа с резким стуком в цилиндрах, дымный выхлоп")
            if vehicle in self.FUEL_CASES:
                return dict(vehicle_id=vehicle, session_id=check, status="anomaly", anomaly_type="F13",
                            anomaly_title="Подача бензина", signal_quality="bad" if check == "bad" else "good",
                            anomaly_score=0.8, confidence=0.82, model_version="test",
                            description="Двигатель «чихает» в карбюратор при резком открытии дросселя, "
                                        "обороты холостого хода плавают")
            return dict(vehicle_id=vehicle, session_id=check, status="anomaly", anomaly_type="F09",
                        anomaly_title="Турбокомпрессор, наддув", signal_quality="bad" if check == "bad" else "good",
                        anomaly_score=0.8, confidence=0.82, model_version="test",
                        description="Свист и подвывание в районе турбокомпрессора, растёт с оборотами")

        self.enterContext(patch.object(web, "fetch_diagnosis", side_effect=diagnose))
        self.url = self.enterContext(running(web.make_server(
            ("127.0.0.1", 0), root=root, search=Search(), orders_db=root / "orders.sqlite")))

    def request(self, route, body):
        post = Request(self.url + route, data=json.dumps(body).encode("utf-8"),
                       headers={"Content-Type": "application/json"})
        with urlopen(post, timeout=5) as response:
            return json.load(response)

    def test_f09_requires_a_recheck_when_turbo_applicability_is_unconfirmed(self):
        for machine in ("C933KX98", "C808CM178", "fixture-vehicle"):
            with self.subTest(machine=machine):
                check, _ = self.CASES[machine]
                selection = dict(machine_id=machine, check_id=check)
                diagnosis = self.request("/api/diagnose", selection)
                self.assertEqual(diagnosis["text"], self.MESSAGE)
                self.assertEqual(diagnosis["diagnosis"]["anomaly_type"], "F09")
                self.assertEqual(diagnosis["sources"], [])
                self.assertFalse(diagnosis["context_id"])

                followup = self.request("/api/ask", selection | {"question": "Что проверить в первую очередь?"})
                self.assertEqual(followup["text"], self.MESSAGE)
                self.assertEqual(followup["sources"], [])

                sources = self.request("/api/ask", selection | {"question": "Покажи источники"})
                self.assertEqual(sources["reading_references"][0]["sources"][0]["url"], "/manuals/guide.pdf#page=1")

                oil = self.request("/api/ask", selection | {"question": "Как измерить давление масла?"})
                self.assertIn("датчика указателя давления масла", oil["text"])
                self.assertNotIn(self.MESSAGE, oil["text"])
                self.assertEqual(oil["sources"][0]["url"], "/manuals/guide.pdf#page=1")

    def test_f09_links_to_verified_factory_layout_in_the_chat(self):
        with patch.object(ask, "_FACTORY_LAYOUTS", self.factory_layouts, create=True):
            for machine in ("C933KX98", "C808CM178"):
                with self.subTest(machine=machine):
                    check, engine = self.CASES[machine]
                    (document, _, pages), = self.factory_layouts["F09", engine]
                    selection = dict(machine_id=machine, check_id=check)
                    start = self.request("/api/diagnose", selection)
                    followup = self.request("/api/ask", selection | {"question": "Есть ли здесь турбокомпрессор?"})
                    for response in (start, followup):
                        self.assertIn(f"В штатной конструкции двигателя {engine}, описанной в руководстве, "
                                      "турбокомпрессор не предусмотрен.", response["text"])
                        self.assertIn("повторите проверку", response["text"])
                        self.assertEqual([ref["url"] for ref in response["sources"]],
                                         [f"/manuals/{document}.pdf#page={page}" for page in pages])
                        self.assertFalse(response.get("context_id"))

    def test_f11_explains_verified_factory_layout_with_both_manuals(self):
        selection = dict(machine_id="T801PO178", check_id="S-0046")
        fallback = self.request("/api/diagnose", selection)
        self.assertIn("применимость проверки системы рециркуляции отработавших газов не подтверждена",
                      fallback["text"])
        self.assertFalse(fallback["sources"])
        self.assertFalse(fallback["context_id"])
        with patch.object(ask, "_FACTORY_LAYOUTS", self.factory_layouts, create=True):
            start = self.request("/api/diagnose", selection)
            followup = self.request("/api/ask", selection | {"question": "Есть ли у этого двигателя EGR?"})
            for answer in (start, followup):
                self.assertIn("В штатной конструкции двигателя ЯМЗ-236М2, описанной в руководствах, "
                              "система рециркуляции отработавших газов (EGR) не предусмотрена.", answer["text"])
                self.assertIn("повторите проверку", answer["text"])
                self.assertEqual([ref["url"] for ref in answer["sources"]], [
                    "/manuals/yamz-236m2-re-2010.pdf#page=43",
                    "/manuals/yamz-236m2-rk-2014.pdf#page=180",
                    "/manuals/yamz-236m2-rk-2014.pdf#page=221",
                ])
                self.assertFalse(answer.get("context_id"))
        self.assertNotIn(("ЯМЗ-236М2", "F11"), self.fault_lookups)

    def test_f09_searches_yamz_manual_and_bad_recording_takes_precedence(self):
        diagnosis = self.request("/api/diagnose", dict(machine_id="C206CC178", check_id="S-0059"))
        self.assertIn(("ЯМЗ-536", "F09"), self.fault_lookups)
        self.assertNotIn(self.MESSAGE, diagnosis["text"])
        self.assertIn("Проверить крепление турбокомпрессора", diagnosis["text"])
        self.assertEqual(diagnosis["sources"][0]["expert_rows"], [498])

        bad = self.request("/api/diagnose", dict(machine_id="C933KX98", check_id="bad"))
        self.assertIn("Повторите запись", bad["text"])
        self.assertNotIn(self.MESSAGE, bad["text"])
        self.assertFalse(bad["context_id"])


    def test_f13_on_yamz_536_requests_recheck_regardless_of_fuel_field(self):
        for machine in ("M260EH47", "fixture-yamz-no-fuel", "fixture-yamz-with-gasoline"):
            with self.subTest(machine=machine):
                check, _, _ = self.FUEL_CASES[machine]
                selection = dict(machine_id=machine, check_id=check)
                diagnosis = self.request("/api/diagnose", selection)
                self.assertEqual(diagnosis["text"], self.FUEL_MESSAGE)
                self.assertEqual(diagnosis["diagnosis"]["anomaly_type"], "F13")
                self.assertEqual(diagnosis["sources"], [])
                self.assertFalse(diagnosis["context_id"])

                followup = self.request("/api/ask", selection | {"question": "Что проверить в первую очередь?"})
                self.assertEqual(followup["text"], self.FUEL_MESSAGE)
                self.assertEqual(followup["sources"], [])
                self.assertNotIn(("ЯМЗ-536", "F13"), self.fault_lookups)

        bad = self.request("/api/diagnose", dict(machine_id="M260EH47", check_id="bad"))
        self.assertIn("Повторите запись", bad["text"])
        self.assertNotIn(self.FUEL_MESSAGE, bad["text"])
        self.assertFalse(bad["context_id"])

    def test_f13_still_searches_when_gasoline_is_known_or_fuel_is_missing(self):
        gasoline = self.request("/api/diagnose", dict(machine_id="P227BB78", check_id="S-0118"))
        self.assertNotIn(self.FUEL_MESSAGE, gasoline["text"])
        self.assertIn("Проверить дроссель и карбюратор", gasoline["text"])
        self.assertIn(("ЗМЗ-5234.10", "F13"), self.fault_lookups)

        unknown = self.request("/api/diagnose", dict(machine_id="fixture-unknown-fuel", check_id="fixture-check"))
        self.assertNotIn(self.FUEL_MESSAGE, unknown["text"])
        self.assertIn(("ДВС-ТЕСТ", "F13"), self.fault_lookups)


    def test_f08_on_gasoline_requests_recheck_without_order(self):
        for vehicle in ("Y652CK98", "fixture-zmz-no-fuel"):
            with self.subTest(vehicle=vehicle):
                check, engine, _ = self.DIESEL_SUPPLY_CASES[vehicle]
                selection = dict(machine_id=vehicle, check_id=check)
                reply = self.request("/api/diagnose", selection)
                self.assertEqual(reply["text"], self.DIESEL_SUPPLY_MESSAGE)
                self.assertEqual(reply["diagnosis"]["anomaly_type"], "F08")
                self.assertEqual(reply["sources"], [])
                self.assertFalse(reply["context_id"])

                followup = self.request("/api/ask", selection | {"question": "Что проверить в первую очередь?"})
                self.assertEqual(followup["text"], self.DIESEL_SUPPLY_MESSAGE)
                self.assertEqual(followup["sources"], [])
                self.assertNotIn((engine, "F08"), self.fault_lookups)

        bad = self.request("/api/diagnose", dict(machine_id="Y652CK98", check_id="bad"))
        self.assertIn("Повторите запись", bad["text"])
        self.assertNotIn(self.DIESEL_SUPPLY_MESSAGE, bad["text"])
        self.assertFalse(bad["context_id"])

    def test_f08_looks_up_the_manual_for_yamz_diesel_engines(self):
        for vehicle in ("M333YK47", "C935KO47"):
            with self.subTest(vehicle=vehicle):
                check, engine, _ = self.DIESEL_SUPPLY_CASES[vehicle]
                reply = self.request("/api/diagnose", dict(machine_id=vehicle, check_id=check))
                self.assertNotIn(self.DIESEL_SUPPLY_MESSAGE, reply["text"])
                self.assertIn((engine, "F08"), self.fault_lookups)
                self.assertIn("Проверить форсунки", reply["text"])
                self.assertEqual(reply["sources"][0]["url"], "/manuals/guide.pdf#page=1")


    def test_f12_on_diesel_requests_a_recheck_without_order(self):
        for vehicle in ("K354KC98", "fixture-ignition-no-fuel", "fixture-ignition-diesel"):
            with self.subTest(vehicle=vehicle):
                check, engine, _ = self.IGNITION_CASES[vehicle]
                selection = dict(machine_id=vehicle, check_id=check)
                reply = self.request("/api/diagnose", selection)
                self.assertEqual(reply["text"], self.IGNITION_MESSAGE)
                self.assertEqual(reply["diagnosis"]["anomaly_type"], "F12")
                self.assertFalse(reply["context_id"])
                self.assertEqual(reply["sources"], [])

                followup = self.request("/api/ask", selection | {"question": "Что проверить в первую очередь?"})
                self.assertEqual(followup["text"], self.IGNITION_MESSAGE)
                self.assertNotIn((engine, "F12"), self.fault_lookups)

        bad = self.request("/api/diagnose", dict(machine_id="K354KC98", check_id="bad"))
        self.assertIn("Повторите запись", bad["text"])
        self.assertFalse(bad["context_id"])

    def test_f12_searches_manual_for_gasoline_engine(self):
        reply = self.request("/api/diagnose", dict(machine_id="E622KM47", check_id="S-0195"))
        self.assertNotIn(self.IGNITION_MESSAGE, reply["text"])
        self.assertIn(("ЗМЗ-40904.10", "F12"), self.fault_lookups)
