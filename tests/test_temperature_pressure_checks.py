"""Проверяет источники первичных работ при низком давлении масла и перегреве."""

import json
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pymupdf

from ai_mechanic import web
from test_web_flow import running


ZMZ = "ЗМЗ-40904.10"
YAMZ = "ЯМЗ-236М2"
BUS = "ЗМЗ-5234.10"
CASES = {
    ZMZ: ("M199CH98", "S-0189", "F02", "zmz-40904-40905-rk-2025", 143,
          "Сигнализатор аварийного давления масла горит на прогретом двигателе, шум привода клапанов усилился"),
    YAMZ: ("A427MA78", "S-0032", "F03", "yamz-236m2-re-2010", 54,
           "Шум вентилятора постоянный, температура охлаждающей жидкости выше нормы"),
    BUS: ("Y040TP178", "S-0125", "F03", "zmz-5234-10-rk-2025", 54,
          "Свист ремня вентилятора при наборе оборотов, температура выше рабочей"),
}

WARNING = ("Не допускается эксплуатация двигателя с горящим сигнализатором аварийного давления масла. "
           "Это приведет к задиру подшипников коленчатого вала. Загорание сигнализатора допускается при работе "
           "двигателя на минимальной частоте вращения в режиме холостого хода и при резком торможении.")
LEVEL = ("Проверку уровня производить при нахождении автомобиля на ровной горизонтальной площадке. "
         "После остановки двигателя или после доливки масла уровень следует проверять не ранее, чем через "
         "5 минут, чтобы масло успело стечь в масляный картер. Для проверки уровня масла: – вынуть указатель "
         "уровня масла; – протереть конец указателя с отметками чистой ветошью; – вставить указатель в трубку "
         "до упора; – снова вынуть указатель и проверить уровень масла на указателе.")
KNOCK_WARNING = (
    "При появлении в работающем двигателе выделяющихся шумов и стуков следует выяснить причину их "
    "возникновения и до устранения неисправности двигатель не эксплуатировать. "
    "В холодном двигателе после запуска возможно появление стуков гидротолкателей клапанов и гидронатяжителей. "
    "По мере прогрева двигателя до температуры охлаждающей жидкости +80…+90 °С, но не более чем через 30 минут "
    "после достижения указанной температуры, стуки должны исчезнуть. Если стуки не исчезнут, необходимо "
    "проверить подачу масла к гидротолкателям и гидронатяжителям или заменить неисправные гидротолкатели "
    "или гидронатяжители."
)
ALARM_SENDER = (
    "Датчик аварийного давления масла. Постоянное горение лампы сигнализатора при работе двигателя "
    "(стрелка указателя давления или контрольный манометр показывают давление масла выше 1 кгс/см2) "
    "может быть следствием неисправности датчика и замыкания на массу в цепи от датчика до лампы сигнализатора. "
    "В данном случае следует отсоединить провод от датчика. Если нет замыкания на массу и неисправен датчик, "
    "контрольная лампа на панели приборов должна погаснуть."
)
VALVE_NOISE = {
    142: ("Гидротолкатель. После запуска холодного двигателя возможно появление стука гидротолкателей клапанов, "
          "который должен исчезнуть по мере прогрева двигателя до температуры охлаждающей жидкости плюс 80…90 °С. "
          "Если стук не исчезает более чем через 30 мин после достижения указанной температуры, необходимо "
          "проверить исправность гидротолкателей как указано далее. Для удаления воздуха рекомендуется "
          "выполнить следующие действия: – запустить и прогреть двигатель до рабочей температуры. "
          "На 3…4 мин установить режим работы двигателя на постоянной частоте вращения 2500 об/мин."),
    143: ("В случае, если стук не устранился после 5 циклов плюс 15 мин работы двигателя, необходимо выполнить "
          "следующие работы: – при помощи стетоскопа (или другого прибора, усиливающего звук) локализовать "
          "источник стука; – снять крышку клапанов. Проверить подачу масла к гидротолкателям, приработку на торце "
          "гидротолкателя и вращение в гнезде.")
}
ZMZ_WORKS = ("Проверить уровень масла", "Проверить датчик аварийного давления масла и его цепь")
ZMZ_CONFIRMATION = "Лампа горит постоянно. Контрольный манометр показал 1,2 кгс/см²."
YAMZ_TABLE = ("Повышенная температура в системе охлаждению\nСлабое натяжение или обрыв  Натянуть ремень, если необходимо,\n"
              "ремня водяного насоса   заменить\nЗагрязнение внешней поверхнос-  Очистить сердцевину р адиатора\n"
              "ти сердцевины радиатора\nЗаедание клапана термостата в   Заменить неисправный термостат")
BUS_ROWS = [["Двигатель перегревается", None],
            ["Недостаточное количество охлаждающей жидкости в системе охлаждения",
             "Долить жидкость, проверить, нет ли течи в соединениях шлангов, в сальниках водяного насоса, радиатора"],
            ["Неисправен термостат", "Снять термостат и проверить его работу, при необходимости заменить"],
            ["Пробуксовка ремня вентилятора", "Отрегулировать натяжение ремня"]]
SENDER = {
    89: "Датчик указателя давления масла 23.3829010 (рис.78) устанавливается на часть двигателей.",
    110: ("Датчик указателя давления масла. Если при включении зажигания и неработающем, холодном двигателе "
          "(при этом контрольная лампа аварийного давления масла должна гореть) стрелка указателя находится в конце "
          "шкалы или показывает давление больше 0 кгс/см2, возможно произошел выход датчика из строя, или произошло "
          "замыкание на массу в цепи от датчика до указателя. Для установления причины следует отсоединить провод "
          "от датчика. Если неисправен датчик и нет замыкания на массу, стрелка должна"),
    111: ("вернуться в начало шкалы. Если стрелка указателя после запуска и при работе двигателя постоянно находится "
          "в начале шкалы (при этом контрольная лампа аварийного давления масла не горит), то, возможно, вышел датчик "
          "из строя, или произошел обрыв в цепи от датчика до указателя. Для проверки следует отсоединить провод от "
          "датчика и замкнуть его на массу. Если неисправен датчик, стрелка должна переместится в правый конец шкалы.")
}
ZMZ_PDF_TEXT = {91: KNOCK_WARNING + " " + WARNING, 95: LEVEL, **SENDER, **VALVE_NOISE}
ZMZ_PDF_TEXT[111] += " " + ALARM_SENDER


class TemperaturePressureChecks(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        raw = root / "data/raw"
        raw.mkdir(parents=True)
        notes = root / "data/annotations"
        notes.mkdir()
        manuals, vehicles, diagnoses = [], [], {}
        self.hits = {}
        for engine, (vehicle, check, code, doc, count, symptoms) in CASES.items():
            pdf = raw / (doc + ".pdf")
            with pymupdf.open() as guide:
                for page in range(1, count + 1):
                    sheet = guide.new_page()
                    if engine == ZMZ and page in ZMZ_PDF_TEXT:
                        sheet.insert_htmlbox(sheet.rect, "<p>" + ZMZ_PDF_TEXT[page] + "</p>")
                guide.save(pdf)
            m = dict(document_id=doc, title="Руководство " + engine, path="data/raw/" + pdf.name, edition="2025",
                     source_sha256=sha256(pdf.read_bytes()).hexdigest(), page_count=count, engine_models=[engine])
            manuals.append(m)
            vehicles.append(dict(vehicle_id=vehicle, model="Машина", engine_model=engine,
                                 sessions=[dict(session_id=check, vehicle_id=vehicle, status="anomaly")]))
            diagnoses[check] = dict(vehicle_id=vehicle, session_id=check, status="anomaly", anomaly_type=code,
                                    anomaly_title="Низкое давление масла" if code == "F02" else "Перегрев, охлаждающая жидкость в масле",
                                    signal_quality="good", confidence=.8, anomaly_score=.9, description=symptoms, model_version="test")
            if engine == ZMZ:
                pages = [(91, ZMZ_PDF_TEXT[91]), (95, LEVEL), (111, ALARM_SENDER), *VALVE_NOISE.items()]
                self.zmz_pdf = pdf
            elif engine == YAMZ:
                pages = [(54, YAMZ_TABLE)]
            else:
                pages = [(54, "\n".join(" | ".join(c or "" for c in row) for row in BUS_ROWS))]
            self.hits[engine] = [dict(m, page=page, text=txt, applicability=[engine + "|all"],
                                      tables=[dict(rows=BUS_ROWS)] if engine == BUS else []) for page, txt in pages]
        (raw / "vehicles.json").write_text(json.dumps({"vehicles": vehicles}), encoding="utf-8")
        (notes / "manuals.json").write_text(json.dumps(manuals), encoding="utf-8")
        hits = self.hits

        class Backend:
            def search(self, engine, query, *, limit, variant):
                return hits[engine]

        self.enterContext(patch.object(web, "fetch_diagnosis", side_effect=lambda _, v, c: diagnoses[c]))
        self.sender = Mock(return_value="TKT-zmz-oil")
        self.enterContext(patch.object(web, "submit_order", self.sender))
        self.url = self.enterContext(running(web.make_server(("127.0.0.1", 0), root=root, search=Backend(),
                                                           orders_db=root / "orders.sqlite")))

    def post(self, path, body):
        with urlopen(Request(self.url + path, data=json.dumps(body).encode(),
                             headers={"Content-Type": "application/json"}), timeout=5) as response:
            return json.load(response)

    def selection(self, engine):
        return dict(machine_id=CASES[engine][0], check_id=CASES[engine][1])

    def test_zmz40904_diagnosis_asks_for_lamp_regime_and_pressure_before_draft(self):
        reply = self.post("/api/diagnose", self.selection(ZMZ))
        self.assertFalse(reply.get("context_id"))
        self.assertTrue({91, 95} <= {source["page"] for source in reply["sources"]})
        text = reply["text"].lower()
        for expected in (r"оборот", r"стрелочн", r"контрольн\w+ манометр",
                         r"измеряли ли", r"каким прибором", r"при каких оборотах и при какой температуре двигателя"):
            self.assertRegex(text, expected)
        for term in ("горизонтальной площадке", "5 минут"):
            self.assertIn(term, text)

    def ask_zmz(self, question, previous):
        fields = {name: previous[name] for name in ("topic", "context_id") if previous.get(name)}
        return self.post("/api/ask", self.selection(ZMZ) | {"question": question} | fields)

    def checked_zmz_draft(self, reply):
        self.assertTrue(reply.get("context_id"), reply["text"])
        self.assertTrue({95, 111} <= {source["page"] for source in reply["sources"]})
        for source in reply["sources"]:
            if source["page"] in (95, 111):
                self.assertEqual(source["url"], f"/manuals/{CASES[ZMZ][3]}.pdf#page={source['page']}")
        draft = self.post("/api/orders/draft", {"context_id": reply["context_id"]})
        self.assertEqual(draft["payload"]["priority"], "medium")
        self.assertEqual(draft["payload"]["works"], list(ZMZ_WORKS))
        self.assertEqual(draft["payload"]["parts"], [])
        self.assertEqual({(s["document_id"], s["page"]) for s in draft["payload"]["sources"]},
                         {(CASES[ZMZ][3], 95), (CASES[ZMZ][3], 111)})
        return draft

    @staticmethod
    def order_version(draft):
        return {name: draft[name] for name in ("context_id", "draft_id", "revision")}

    def test_zmz40904_confirmed_pressure_and_continuous_lamp_allow_two_checks(self):
        initial = self.post("/api/diagnose", self.selection(ZMZ))
        for pressure in ("Контрольный манометр показал 1,2 кгс/см².",
                         "Стрелочный указатель показывает 1,2 кгс/см²."):
            with self.subTest(pressure=pressure):
                confirmed = self.ask_zmz("Лампа горит постоянно. " + pressure, initial)
                self.checked_zmz_draft(confirmed)

    def test_zmz40904_remembers_mechanic_lamp_and_indicator_in_separate_messages(self):
        start = self.post("/api/diagnose", self.selection(ZMZ))
        lamp = self.ask_zmz("Лампа горит постоянно.", start)
        self.assertFalse(lamp.get("context_id"))
        confirmed = self.ask_zmz("Стрелочный указатель показывает 1,2 кгс/см².", lamp)
        self.checked_zmz_draft(confirmed)

    def test_zmz40904_measurement_speed_does_not_replace_lamp_speed(self):
        for lamp_speed, measure_speed, warning in (
            ("минимальном холостом ходу", "средних оборотах", False),
            ("средних оборотах", "минимальном холостом ходу", True),
        ):
            with self.subTest(lamp_speed=lamp_speed):
                initial = self.post("/api/diagnose", self.selection(ZMZ))
                lamp = self.ask_zmz(f"Лампа горела на {lamp_speed}.", initial)
                reply = self.ask_zmz(
                    f"На {measure_speed} контрольный манометр показал давление масла 1,2 кгс/см².",
                    lamp,
                )
                self.assertEqual("Прекратите эксплуатацию до выяснения причины." in reply["text"], warning)
                self.assertFalse(reply.get("context_id"), reply["text"])
        initial = self.post("/api/diagnose", self.selection(ZMZ))
        reply = self.ask_zmz(
            "Лампа горела на минимальном холостом ходу и на средних оборотах "
            "контрольный манометр показал давление масла 1,2 кгс/см².",
            initial,
        )
        self.assertNotIn("Прекратите эксплуатацию до выяснения причины.", reply["text"])
        self.assertFalse(reply.get("context_id"), reply["text"])
        self.sender.assert_not_called()

    def test_zmz40904_declines_unconfirmed_pressure_and_temporary_lamp(self):
        for statement in (
                "Фактическое давление масла ещё не измеряли.",
                "Лампа горит постоянно. Контрольный манометр показал 1 кгс/см².",
                "Лампа горит постоянно. Давление масла 1,2 кгс/см².",
                "Лампа аварийного давления масла не горит постоянно. Контрольный манометр показывает 1,2 кгс/см².",
                "Лампа перегрева горит постоянно. Контрольный манометр масла показывает 1,2 кгс/см².",
                "Лампа горит постоянно. Контрольный манометр показывает не больше 1,2 кгс/см².",
                "Двигатель заглушён, зажигание включено. Лампа горит постоянно. "
                "Стрелочный указатель показывает 1,2 кгс/см².",
                "Лампа горит постоянно. По сигналу ML давление масла 1,2 кгс/см².",
                "Лампа горела только на минимальном холостом ходу. "
                "Стрелочный указатель показывает 1,2 кгс/см².",
                "Лампа загорелась при резком торможении и погасла. "
                "Контрольный манометр показал 1,2 кгс/см².",
                "Что делать, если лампа горит постоянно, а контрольный манометр показывает 1,2 кгс/см²?",
                "Что делать при постоянно горящей лампе и показании контрольного манометра 1,2 кгс/см²"):
            with self.subTest(statement=statement):
                initial = self.post("/api/diagnose", self.selection(ZMZ))
                reply = self.ask_zmz(statement, initial)
                self.assertFalse(reply.get("context_id"), reply["text"])
        self.sender.assert_not_called()

    def test_zmz40904_dangerous_lamp_or_persistent_knock_show_warning_without_draft(self):
        start = self.post("/api/diagnose", self.selection(ZMZ))
        for subject in ("стук", "клапан"):
            self.assertIn(subject, start["text"].lower())
        lamp = self.ask_zmz("Лампа горит на средних оборотах. Давление ещё не измеряли.", start)
        self.assertFalse(lamp.get("context_id"))
        self.assertIn(91, {source["page"] for source in lamp["sources"]})
        self.assertRegex(lamp["text"].lower(), r"не допускается эксплуатация|прекратите эксплуатацию")
        self.assertIn("резком торможении", lamp["text"].lower())
        self.assertIn("холостого хода", lamp["text"].lower())

        start = self.post("/api/diagnose", self.selection(ZMZ))
        knock = self.ask_zmz("После прогрева отчётливый стук в зоне привода клапанов.", start)
        self.assertFalse(knock.get("context_id"))
        pages = [source["page"] for source in knock["sources"]]
        self.assertTrue({91, 142, 143} <= set(pages), knock["text"])
        self.assertLess(pages.index(91), pages.index(142))
        self.assertLess(pages.index(91), pages.index(143))
        text = knock["text"].lower()
        self.assertRegex(text, r"двигатель не эксплуатировать|прекратите эксплуатацию")
        self.assertRegex(text, r"подач\w* (?:к ним )?масла")
        self.assertIn("гидротолкател", text)
        self.assertIn("стетоскоп", text)
        self.assertRegex(text, r"30\s*мин")
        self.assertNotRegex(text, r"2500 об/мин|увеличьте обороты|снимите крышку|снять крышку клапанов")

    def test_zmz40904_withdrawn_lamp_or_pressure_blocks_the_draft(self):
        for correction in ("Лампа погасла.", "Фактическое давление масла не измеряли."):
            with self.subTest(correction=correction):
                initial = self.post("/api/diagnose", self.selection(ZMZ))
                confirmed = self.ask_zmz(ZMZ_CONFIRMATION, initial)
                version = self.order_version(self.checked_zmz_draft(confirmed))
                revoked = self.ask_zmz(correction, confirmed)
                self.assertFalse(revoked.get("context_id"), revoked["text"])
                with self.assertRaises(HTTPError) as error:
                    self.post("/api/orders/send", version | {"confirmed": True})
                self.assertIn(error.exception.code, (400, 409))
        self.sender.assert_not_called()

    def test_zmz40904_editing_regrounds_remaining_work_and_checks_version_before_sending(self):
        ask = self.post("/api/diagnose", self.selection(ZMZ))
        confirmed = self.ask_zmz(ZMZ_CONFIRMATION, ask)
        draft = self.checked_zmz_draft(confirmed)
        for work, page in zip(reversed(ZMZ_WORKS), (111, 95)):
            with self.subTest(work=work):
                version = self.order_version(draft)
                draft = self.post("/api/orders/update", version | {"changes": {"works": [work]}})
                self.assertGreater(draft["revision"], version["revision"])
                self.assertEqual(draft["payload"]["works"], [work])
                self.assertEqual({source["page"] for source in draft["payload"]["sources"]}, {page})
                with self.assertRaises(HTTPError) as old:
                    self.post("/api/orders/send", version | {"confirmed": True})
                self.assertEqual(old.exception.code, 409)
        latest = self.order_version(draft)
        with self.assertRaises(HTTPError) as unconfirmed:
            self.post("/api/orders/send", latest | {"confirmed": False})
        self.assertEqual(unconfirmed.exception.code, 400)
        self.sender.assert_not_called()
        sent = self.post("/api/orders/send", latest | {"confirmed": True})
        self.assertEqual(sent["number"], "TKT-zmz-oil")
        self.sender.assert_called_once()


    def test_oil_indicator_depending_on_installed_equipment_with_original_pages(self):
        case = self.selection(ZMZ)
        first = self.post("/api/diagnose", case)
        confirmed = self.ask_zmz(ZMZ_CONFIRMATION, first)
        draft = self.checked_zmz_draft(confirmed)
        ask = case | {"question": "Как проверить указатель давления масла и его датчик?",
                      "topic": confirmed["topic"], "context_id": confirmed["context_id"]}
        reply = self.post("/api/ask", ask)
        self.assertEqual({89, 110, 111}, {s["page"] for s in reply["sources"]})
        for term in ("установлен", "холодном", "отсоедин", "начало шкалы", "не горит", "на массу", "правый конец"):
            self.assertIn(term, reply["text"].lower())
        after = self.post("/api/orders/draft", {"context_id": confirmed["context_id"]})
        self.assertEqual(draft, after)
        self.zmz_pdf.write_bytes(b"unrelated PDF")
        invalid = self.post("/api/ask", ask)
        self.assertFalse(invalid["sources"])
        self.assertNotIn("отсоедин", invalid["text"].lower())

    def test_indexed_indicator_procedure_is_used_before_pdf_fallback(self):
        case = self.selection(ZMZ)
        self.post("/api/diagnose", case)
        hit = self.hits[ZMZ][0]
        self.hits[ZMZ].extend(dict(hit, page=p, text=text) for p, text in SENDER.items())
        with patch("ai_mechanic.knowledge.temperature_pressure_checks.verified_pdf_pages",
                   side_effect=AssertionError("Все части уже есть в выдаче")):
            reply = self.post("/api/ask", case | {"question": "Как проверить указатель и датчик давления масла?"})
        self.assertEqual({89, 110, 111}, {s["page"] for s in reply["sources"]})

    def test_combined_pressure_warning_and_measurement_for_both_zmz_engines(self):
        from ai_mechanic.knowledge.search import retrieve
        question = "Как проверить давление масла и какие предупреждения даны для этого двигателя?"
        diagnosis = dict(status="anomaly", anomaly_type="F02", anomaly_title="Низкое давление масла",
                         description="Сигнализатор давления масла горит на прогретом двигателе", signal_quality="good")
        bus_page = self.hits[BUS][0]
        instruments = ("Контроль за давлением масла в системе смазки осуществляется с помощью датчика указателя "
                       "давления масла и датчика сигнализатора аварийного давления масла.")
        warning = ("Сигнализатор аварийного давления масла, расположенный в комбинации приборов автобуса, "
                   "загорается при падении давления ниже 40...80 кПа. В случае загорания на средней или большой "
                   "частотах вращения необходимо немедленно заглушить двигатель и до устранения неисправности "
                   "двигатель не заводить.")
        level = ("Проверять уровень масла необходимо на ровной площадке после остановки двигателя. "
                 "Подождать не менее 10 минут. Поддерживать уровень между метками, не превышая её.")
        hits = {ZMZ: self.hits[ZMZ], BUS: [dict(bus_page, page=23, text=instruments + " " + warning),
                                          dict(bus_page, page=28, text=level)]}

        class Search:
            def search(self, engine, query, *, variant, limit):
                return hits[engine]

        for engine, required, warning_text, boundary in (
                (ZMZ, {91, 95}, "резком торможении", "контрольного замера"),
                (BUS, {23, 28}, "немедленно заглушить двигатель", "контрольного замера отдельным манометром")):
            with self.subTest(engine=engine):
                reply = retrieve(Search(), engine, question, diagnosis=diagnosis)
                self.assertEqual(required, {s["page"] for s in reply["evidence"]})
                supported = " ".join(s["text"].lower() for s in reply["evidence"])
                self.assertIn(warning_text, supported)
                self.assertIn(boundary, supported + " " + reply["missing"].lower())

    def test_oil_gauge_followup_returns_instrument_description_on_page_23(self):
        from ai_mechanic.knowledge.search import retrieve

        instrument = ("Контроль за давлением масла в системе смазки осуществляется с помощью датчика "
                      "указателя давления масла и датчика сигнализатора аварийного давления масла.")
        self.hits[BUS] = [dict(self.hits[BUS][0], page=23, text=instrument, tables=[])]
        hits = self.hits[BUS]

        class Search:
            def search(self, engine, query, *, variant, limit):
                return hits

        for question in (
                "Фактическое давление масла ещё не измеряли. Как его измерить? Есть ли порядок подключения манометра?",
                "Как его измерить манометром?"):
            with self.subTest(question=question):
                reply = retrieve(Search(), BUS, question, topic="oil-low")
                self.assertEqual("oil-measurement", reply["topic"])
                self.assertEqual([23], [s["page"] for s in reply["evidence"]])
                self.assertIn("Порядок контрольного замера отдельным манометром", reply["evidence"][0]["text"])
        other = retrieve(Search(), BUS, "Как измерить давление топлива в рампе манометром?", topic="oil-low")
        self.assertNotEqual("oil-measurement", other["topic"])

    def test_short_component_name_changes_the_topic(self):
        case = self.selection(YAMZ)
        first = self.post("/api/diagnose", case)
        for question in ("Как проверить ГРМ?", "Как проверить грм?"):
            reply = self.post("/api/ask", case | {"question": question, "topic": first["topic"]})
            self.assertNotIn("проверить натяжение и целостность ремня водяного насоса", reply["text"].lower())
            self.assertNotIn("осмотреть наружную поверхность сердцевины радиатора", reply["text"].lower())

    def test_each_overheating_case_gets_its_own_work(self):
        for engine, terms in ((YAMZ, ("натяжение", "целостность", "водяного насоса", "сердцевины радиатора")),
                              (BUS, ("теч", "шлангов", "сальниках водяного насоса", "радиатор"))):
            with self.subTest(engine=engine):
                reply = self.post("/api/diagnose", self.selection(engine))
                self.assertEqual([54], [s["page"] for s in reply["sources"]])
                self.assertIn("нельзя подтвердить", reply["text"])
                draft = self.post("/api/orders/draft", {"context_id": reply["context_id"]})["payload"]
                works = " ".join(draft["works"]).lower()
                for term in terms:
                    self.assertIn(term, works)
                self.assertNotRegex(works, r"замен|отрегулир|промыт|долить")
                self.assertEqual([], draft["parts"])
                self.assertEqual([54], [s["page"] for s in draft["sources"]])
                if engine == BUS:
                    self.assertRegex(reply["text"].lower(), r"если [^.]{0,55}пробуксовк")
