"""Проверяет ответы и черновики F02 для ЯМЗ-236М2 и ЯМЗ-536."""

from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pymupdf

from ai_mechanic import web
from test_web_flow import running


class YamzOilPressureFlowTests(unittest.TestCase):
    CASES = {
        "ЯМЗ-236М2": ("E549YT198", "S-0011", "yamz-236m2-re-2010", 53,
                      "Глухой стук в нижней части блока на фоне сигнала низкого давления масла"),
        "ЯМЗ-536": ("A464EP47", "S-0096", "yamz-536-re-2016", 219,
                    "Низкий гул в нижней части двигателя, сигнал датчика давления масла ниже нормы"),
    }
    WARNING = (
        "При эксплуатации двигателя следить за давлением масла в системе смазки; "
        "падение давления масла ниже 300 кПа (3,0 кгс/см2) при номинальных оборотах свидетельствует о "
        "неисправностях в системе смазки или повышенных износах вкладышей подшипников коленчатого вала."
    )
    PREREQUISITE = ("Прежде чем искать причину неисправности, убедиться в наличии "
                    "достаточного количества масла в картере и охлаждающей жидкости")
    LEAK_ROW = (
        "Пониженное давление масла в системе смазки*\nНегерметичность соединения   Проверить соединения, особенно\n"
        "маслопроводов   прокладки фильтров, отводящих и\nвсасывающей трубок масляного\n"
        "насоса и прокладку фланца фильтра\nцентробежной очистки масла. Если\n"
        "необходимо, подтянуть соединения\nили заменить прокладку\n"
        "Увеличение зазоров в коренных    Направить двигатель в ремонт."
    )
    YAMZ536 = "ЯМЗ-536"
    YAMZ536_MANUAL = "yamz-536-re-2016"
    YAMZ536_WORKS = ["Проверить уровень масла в картере", "Проверить соединения и уплотнения маслопроводов"]
    NOMINAL_PRESSURE = (
        "Давление масла в системе смазки двигателя, прогретого до 80-100ºС, кПа (кгс/см2):\n"
        "- при номинальной частоте вращения 400-635 (4,1-6,5)\n"
        "- при минимальной частоте вращения холостого хода, не менее 100 (1,0)"
    )
    OIL_LEVEL_CAUSE = "14.1 Недостаточно масла в масляном картере (ниже нижней метки маслощупа)"
    OIL_LEVEL_CHECK = "Долить масло до нормы"
    OIL_JOINT_CAUSE = "14.7 Негерметичны соединения маслопроводов"
    OIL_JOINT_CHECK = (
        "Проверить соединения и уплотнения, при необходимости, подтянуть соединения или заменить уплотнения"
    )
    SENSOR_CAUSE = "14.2 Неисправен датчик или указатель давления (дублирующие приборы)"
    SENSOR_CHECK = ("Проверить давление масла эталонным манометром или диагностическим прибором, "
                    "при необходимости, заменить датчик или указатель давления")

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
                document_id=doc, title=f"Руководство по эксплуатации {engine}", edition="РЭ",
                path=f"data/raw/{pdf.name}", engine_models=[engine], page_count=count,
                source_sha256=sha256(pdf.read_bytes()).hexdigest())
            diagnoses[vehicle] = dict(vehicle_id=vehicle, session_id=check, status="anomaly", anomaly_type="F02",
                                      anomaly_title="Низкое давление масла", signal_quality="good",
                                      description=symptoms, anomaly_score=0.85, confidence=0.84, model_version="test")
        (raw / "vehicles.json").write_text(json.dumps({"vehicles": cards}, ensure_ascii=False), encoding="utf-8")
        annotations = root / "data" / "annotations"
        annotations.mkdir()
        (annotations / "manuals.json").write_text(json.dumps(list(manuals.values()), ensure_ascii=False), encoding="utf-8")

        def fragment(engine, page, text, **fields):
            return dict(manuals[engine], page=page, text=text, applicability=[engine + "|all"], **fields)

        preliminary = fragment("ЯМЗ-236М2", 53, "* " + self.PREREQUISITE)
        warning = fragment("ЯМЗ-236М2", 4, self.WARNING, fault_code="F02", expert_rows=[95])
        connections = fragment("ЯМЗ-236М2", 53, self.LEAK_ROW, fault_code="F02", expert_rows=[96])
        nominal = fragment(self.YAMZ536, 49, self.NOMINAL_PRESSURE)
        sensor = fragment(self.YAMZ536, 218,
                          "14 ДАВЛЕНИЕ МАСЛА НИЖЕ ДОПУСТИМОГО\n"
                          + self.OIL_LEVEL_CAUSE + " | " + self.OIL_LEVEL_CHECK + "\n"
                          + self.SENSOR_CAUSE + " | " + self.SENSOR_CHECK,
                          section="12 ПОВЫШЕННАЯ ВИБРАЦИЯ ДВИГАТЕЛЯ",
                          tables=[{"rows": [[self.OIL_LEVEL_CAUSE, self.OIL_LEVEL_CHECK],
                                            [self.SENSOR_CAUSE, self.SENSOR_CHECK]]}])
        joints = fragment(self.YAMZ536, 219, self.OIL_JOINT_CAUSE + " | " + self.OIL_JOINT_CHECK,
                          tables=[{"rows": [[self.OIL_JOINT_CAUSE, self.OIL_JOINT_CHECK]]}])
        pump = fragment(self.YAMZ536, 219, "14.8 Неисправен масляный насос | Заменить масляный насос")
        self.hits = {"ЯМЗ-236М2": [preliminary], self.YAMZ536: [pump, nominal, joints, sensor]}
        self.fault_hits = {"ЯМЗ-236М2": [connections, warning], self.YAMZ536: [pump]}
        fixture = self

        class Search:
            def search(self, engine, query, *, variant, limit):
                return fixture.hits[engine]

            def search_fault(self, engine, query, *, code, variant, limit):
                return fixture.fault_hits[engine] if code == "F02" else []

        self.enterContext(patch.object(web, "fetch_diagnosis",
                                       side_effect=lambda _url, vehicle, _check: diagnoses[vehicle]))
        self.sender = Mock(return_value="TKT-oil")
        self.enterContext(patch.object(web, "submit_order", self.sender))
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

    def ask(self, question, previous, *, engine="ЯМЗ-236М2"):
        body = self.selection(engine) | {"question": question}
        for field in ("topic", "context_id"):
            if previous.get(field):
                body[field] = previous[field]
        return self.post("/api/ask", body)

    def assert_yamz236_sources(self, reply):
        self.assertEqual({source["page"] for source in reply["sources"]}, {4, 53})
        for source in reply["sources"]:
            page = source["page"]
            self.assertEqual(source["url"], f"/manuals/yamz-236m2-re-2010.pdf#page={page}")

    def test_yamz236_asks_for_measurement_and_keeps_manual_checks(self):
        initial = self.post("/api/diagnose", self.selection("ЯМЗ-236М2"))
        for term in ("ниже 300 кПа", "номинальных оборотах", "фактическое давление масла",
                     "масла в картере и охлаждающей жидкости"):
            self.assertIn(term.casefold(), initial["text"].casefold())
        self.assertRegex(initial["text"].casefold(), r"при каких оборотах")
        self.assertIn("каким прибором", initial["text"].casefold())
        self.assertIn("при какой температуре двигателя", initial["text"].casefold())
        self.assertNotIn("направить двигатель в ремонт", initial["text"].casefold())
        self.assertFalse(initial.get("context_id"))
        self.assert_yamz236_sources(initial)

        reply = self.ask("Фактическое давление масла ещё не измеряли.", initial)
        self.assertFalse(reply.get("context_id"))
        self.assert_yamz236_sources(reply)
        self.assertRegex(reply["text"].casefold(), r"контрольн\w*\s+замер")
        self.assertIn("масла в картере и охлаждающей жидкости", reply["text"].casefold())
        self.sender.assert_not_called()

    def test_yamz236_pressure_limit_requires_numeric_measurement_at_nominal_speed(self):
        initial = self.post("/api/diagnose", self.selection("ЯМЗ-236М2"))
        for observation in (
            "Измерили давление масла. На номинальных оборотах 300 кПа.",
            "Измерили давление масла. На номинальных оборотах 350 кПа.",
            "Давление масла измерили на холостых оборотах, результат 250 кПа.",
            "Измерили давление масла, получили 250 кПа.",
            "Если при номинальных оборотах давление масла 250 кПа, что делать?",
            "По сигналу ML давление масла 250 кПа на номинальных оборотах.",
            "На холостых измерили 180 кПа. На номинальных оборотах измерили 320 кПа.",
        ):
            with self.subTest(observation=observation):
                self.assertFalse(self.ask(observation, initial).get("context_id"))
        self.sender.assert_not_called()

    def test_yamz236_confirmed_low_pressure_draft_and_submission(self):
        initial = self.post("/api/diagnose", self.selection("ЯМЗ-236М2"))
        reply = self.ask("Измерили давление масла на номинальных оборотах, получили 299,9 кПа.", initial)
        self.assert_yamz236_sources(reply)
        text = reply["text"].casefold()
        self.assertIn("масла в картере и охлаждающей жидкости", text)
        self.assertNotIn("направить двигатель в ремонт", text)

        draft = self.post("/api/orders/draft", {"context_id": reply["context_id"]})
        self.assertEqual(draft["payload"]["priority"], "medium")
        self.assertEqual([work.rstrip(".") for work in draft["payload"]["works"]], ["Проверить систему смазки"])
        self.assertEqual({(source["document_id"], source["page"]) for source in draft["payload"]["sources"]},
                         {("yamz-236m2-re-2010", 4), ("yamz-236m2-re-2010", 53)})
        order = {key: draft[key] for key in ("context_id", "draft_id", "revision")}
        with self.assertRaises(HTTPError):
            self.post("/api/orders/send", order | {"confirmed": False})
        self.sender.assert_not_called()
        sent = self.post("/api/orders/send", order | {"confirmed": True})
        self.assertEqual(sent["number"], "TKT-oil")
        self.sender.assert_called_once()

    def test_lamp_at_idle_does_not_change_measurement_at_nominal_speed(self):
        for engine, pressure, temperature in (("ЯМЗ-236М2", 260, ""), (self.YAMZ536, 350, " при 90 °C")):
            for conjunction in (", а", " и"):
                with self.subTest(engine=engine, conjunction=conjunction):
                    initial = self.post("/api/diagnose", self.selection(engine))
                    reply = self.ask(
                        f"Лампа загорелась на холостых{conjunction} на номинальных оборотах"
                        f"{temperature} контрольный манометр показал давление масла {pressure} кПа.",
                        initial, engine=engine,
                    )
                    self.assertTrue(reply.get("context_id"), reply["text"])
                    draft = self.post("/api/orders/draft", {"context_id": reply["context_id"]})
                    self.assertEqual(draft["payload"]["priority"], "medium")

    def test_yamz236_does_not_propose_work_without_both_manual_pages(self):
        for missing_page in (4, 53):
            with self.subTest(missing_page=missing_page):
                groups = (self.fault_hits, self.hits)
                backups = [group["ЯМЗ-236М2"] for group in groups]
                try:
                    for group, hits in zip(groups, backups):
                        group["ЯМЗ-236М2"] = [hit for hit in hits if hit["page"] != missing_page]
                    initial = self.post("/api/diagnose", self.selection("ЯМЗ-236М2"))
                    reply = self.ask("Измерили давление масла на номинальных оборотах. Результат 250 кПа.", initial)
                    self.assertFalse(reply.get("context_id"))
                finally:
                    for group, hits in zip(groups, backups):
                        group["ЯМЗ-236М2"] = hits
        self.sender.assert_not_called()

    def test_yamz236_correction_invalidates_old_draft(self):
        initial = self.post("/api/diagnose", self.selection("ЯМЗ-236М2"))
        reply = self.ask("Фактическое давление масла на номинальных оборотах 250 кПа.", initial)
        draft = self.post("/api/orders/draft", {"context_id": reply["context_id"]})
        corrected = self.ask("Уточнение: 250 кПа измерили на холостом ходу.", reply)
        self.assertFalse(corrected.get("context_id"))
        order = {key: draft[key] for key in ("context_id", "draft_id", "revision")}
        with self.assertRaises(HTTPError):
            self.post("/api/orders/send", order | {"confirmed": True})
        self.sender.assert_not_called()

    def assert_yamz536_source_links(self, reply, expected):
        self.assertEqual({source["page"] for source in reply["sources"]}, set(expected))
        for source in reply["sources"]:
            self.assertEqual(source["url"], f"/manuals/{self.YAMZ536_MANUAL}.pdf#page={source['page']}")

    def test_yamz536_asks_for_measurement_and_keeps_instrument_guidance(self):
        initial = self.post("/api/diagnose", self.selection(self.YAMZ536))
        self.assertFalse(initial.get("context_id"))
        self.assertIn("фактическое давление масла", initial["text"].casefold())
        self.assertRegex(initial["text"].casefold(), r"при каких оборотах")
        self.assertIn("температур", initial["text"].casefold())
        self.assertIn("каким прибором", initial["text"].casefold())
        for question in ("Фактическое давление масла ещё не измеряли.", "Как проверить фактическое давление масла?"):
            with self.subTest(question=question):
                reply = self.ask(question, initial, engine=self.YAMZ536)
                self.assertIn("эталонным манометром или диагностическим прибором", reply["text"])
                self.assertNotIn("Заменить масляный насос", reply["text"])
                self.assertFalse(reply.get("context_id"))
                source = next(source for source in reply["sources"] if source["page"] == 218)
                self.assertEqual(source["url"], f"/manuals/{self.YAMZ536_MANUAL}.pdf#page=218")
                with urlopen(self.url + source["page_url"], timeout=5) as page:
                    self.assertIn("Страница 218 в PDF", page.read().decode())
        self.sender.assert_not_called()

    def test_yamz536_requires_nominal_speed_and_operating_temperature(self):
        initial = self.post("/api/diagnose", self.selection(self.YAMZ536))
        for observation in (
            "Измерили давление масла на номинальных оборотах при 90 °C, результат 400 кПа.",
            "Измерили давление масла на номинальных оборотах при 90 °C, результат 410 кПа.",
            "Измерили давление масла на номинальных оборотах при 79,9 °C, результат 380 кПа.",
            "Измерили давление масла на номинальных оборотах при 100,1 °C, результат 380 кПа.",
            "Измерили давление масла на номинальных оборотах, результат 380 кПа.",
            "Измерили давление масла на холостом ходу при 90 °C, результат 380 кПа.",
            "Измерили давление масла при 90 °C, результат 380 кПа.",
            "По сигналу ML при 90 °C на номинальных оборотах давление масла 380 кПа.",
            "Что делать при давлении масла 390 кПа на номинальных оборотах при 90 °С",
        ):
            with self.subTest(observation=observation):
                self.assertFalse(self.ask(observation, initial, engine=self.YAMZ536).get("context_id"))
        self.sender.assert_not_called()

    def test_yamz536_confirmed_low_pressure_work_on_temperature_boundaries(self):
        initial = self.post("/api/diagnose", self.selection(self.YAMZ536))
        draft = None
        for temperature in (80, 100):
            with self.subTest(temperature=temperature):
                observation = f"Измерили давление масла на номинальных оборотах при {temperature} °C, результат 399,9 кПа."
                reply = self.ask(observation, initial, engine=self.YAMZ536)
                self.assert_yamz536_source_links(reply, {49, 218, 219})
                self.assertIn("399,9 кпа", reply["text"].casefold())
                draft = self.post("/api/orders/draft", {"context_id": reply["context_id"]})
                self.assertEqual(draft["payload"]["priority"], "medium")
                self.assertCountEqual([work.rstrip(".") for work in draft["payload"]["works"]], self.YAMZ536_WORKS)
                self.assertEqual({(source["document_id"], source["page"]) for source in draft["payload"]["sources"]},
                                 {(self.YAMZ536_MANUAL, page) for page in (49, 218, 219)})
                self.assertEqual(draft["payload"]["parts"], [])
        self.assertIsNotNone(draft)
        order = {key: draft[key] for key in ("context_id", "draft_id", "revision")}
        with self.assertRaises(HTTPError):
            self.post("/api/orders/send", order | {"confirmed": False})
        self.sender.assert_not_called()
        sent = self.post("/api/orders/send", order | {"confirmed": True})
        self.assertEqual(sent["number"], "TKT-oil")
        self.sender.assert_called_once()

    def test_yamz536_draft_edit_keeps_only_sources_of_remaining_work(self):
        initial = self.post("/api/diagnose", self.selection(self.YAMZ536))
        reply = self.ask("На номинальных оборотах измерили давление масла 370 кПа при 90 °C.",
                         initial, engine=self.YAMZ536)
        self.assertTrue(reply.get("context_id"), reply["text"])
        draft = self.post("/api/orders/draft", {"context_id": reply["context_id"]})
        self.assertCountEqual([work.rstrip(".") for work in draft["payload"]["works"]], self.YAMZ536_WORKS)

        for work, page in zip(reversed(self.YAMZ536_WORKS), (219, 218)):
            with self.subTest(work=work):
                version = {key: draft[key] for key in ("context_id", "draft_id", "revision")}
                draft = self.post("/api/orders/update", version | {"changes": {"works": [work]}})
                self.assertGreater(draft["revision"], version["revision"])
                self.assertEqual(draft["payload"]["works"], [work])
                self.assertEqual({(source["document_id"], source["page"]) for source in draft["payload"]["sources"]},
                                 {(self.YAMZ536_MANUAL, 49), (self.YAMZ536_MANUAL, page)})
                with self.assertRaises(HTTPError) as error:
                    self.post("/api/orders/send", version | {"confirmed": True})
                self.assertEqual(error.exception.code, 409)
                self.sender.assert_not_called()
        latest = {key: draft[key] for key in ("context_id", "draft_id", "revision")}
        sent = self.post("/api/orders/send", latest | {"confirmed": True})
        self.assertEqual(sent["number"], "TKT-oil")
        self.sender.assert_called_once()

    def test_yamz536_withdrawal_invalidates_old_draft(self):
        for withdrawal in ("Уточнение: давление масла ещё не измеряли.", "Температуру замера не знаю"):
            with self.subTest(withdrawal=withdrawal):
                initial = self.post("/api/diagnose", self.selection(self.YAMZ536))
                reply = self.ask("На номинальных оборотах измерили давление масла 360 кПа при 90 °C.",
                                 initial, engine=self.YAMZ536)
                self.assertTrue(reply.get("context_id"), reply["text"])
                draft = self.post("/api/orders/draft", {"context_id": reply["context_id"]})
                self.ask("Нужно ли измерить давление масла", reply, engine=self.YAMZ536)
                after_question = self.post("/api/orders/draft", {"context_id": reply["context_id"]})
                self.assertEqual(after_question["revision"], draft["revision"])
                self.assertEqual(after_question["payload"], draft["payload"])

                corrected = self.ask(withdrawal, reply, engine=self.YAMZ536)
                self.assertFalse(corrected.get("context_id"))
                order = {key: draft[key] for key in ("context_id", "draft_id", "revision")}
                with self.assertRaises(HTTPError):
                    self.post("/api/orders/send", order | {"confirmed": True})
                self.sender.assert_not_called()

    def test_yamz536_requires_all_three_physical_pages_for_a_draft(self):
        groups = (self.fault_hits, self.hits)
        backups = [group[self.YAMZ536] for group in groups]
        for missing_page in (49, 218, 219):
            with self.subTest(missing_page=missing_page):
                try:
                    for group, hits in zip(groups, backups):
                        group[self.YAMZ536] = [hit for hit in hits if hit["page"] != missing_page]
                    initial = self.post("/api/diagnose", self.selection(self.YAMZ536))
                    reply = self.ask("На номинальных оборотах измерили давление масла 380 кПа при 90 °C.",
                                     initial, engine=self.YAMZ536)
                    self.assertFalse(reply.get("context_id"))
                finally:
                    for group, hits in zip(groups, backups):
                        group[self.YAMZ536] = hits
        self.sender.assert_not_called()
