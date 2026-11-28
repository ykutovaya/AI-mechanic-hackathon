"""Проверяет диалог о лампе, стуке и ремонте ЗМЗ-5234.10."""

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


ENGINE = "ЗМЗ-5234.10"
DOCUMENT = "zmz-5234-10-rk-2025"
CASE = {"machine_id": "H275AA47", "check_id": "S-0139"}
LEVEL_WORK = "Проверить уровень масла по руководству."
REPAIR_WORK = "Выполнить ремонт двигателя"
BEARING_WEAR = "Сильный износ вкладышей коренных подшипников коленчатого вала"
QUESTION = ("На каких оборотах загорелся сигнализатор — на минимальном холостом ходу, "
            "на средних или на высоких? Глухой стук был слышен одновременно с сигналом или отдельно?")
LAMP_TEXT = (
    "Сигнализатор аварийного давления масла, расположенный в комбинации приборов автобуса, загорается по сигналу "
    "датчика аварийного давления масла при падении давления ниже 40...80 кПа (0,4...0,8 кгс/см2). "
    "Допускается загорание сигнализатора на минимальной частоте вращения холостого хода. "
    "В случае исправности системы смазки двигателя при повышении частоты вращения коленчатого вала лампа "
    "сигнализатора должна погаснуть. В случае загорания сигнализатора на средней или большой частотах вращения "
    "коленчатого вала двигателя необходимо немедленно заглушить двигатель и до устранения неисправности "
    "двигатель не заводить."
)
LEVEL_TEXT = (
    "Проверять уровень масла необходимо ежедневно перед первым запуском двигателя. При этом автобус должен "
    "стоять на ровной горизонтальной площадке. Для проверки уровня масла после работы двигателя или после "
    "доливки масла необходимо подождать не менее 10 минут, чтобы масло успело стечь в масляный картер. "
    "Уровень масла необходимо поддерживать между метками «0» и «П» стержневого указателя уровня масла (рис.17), "
    "рекомендуется ближе к метке «П», не превышая её."
)
TABLE_56 = [
    ["Причина неисправности", "Метод устранения"],
    ["Низкое давление масла", None],
    ["Износ шестерен и перегородки масляного насоса", "Заменить шестерни и перегородку масляного насоса"],
    ["Чрезмерный износ подшипников коленчатого или распределительного вала",
     "Заменить вкладыши подшипников коленчатого вала или втулки подшипников распределительного вала"],
    ["Стуки в двигателе", None],
    ["Сильный износ вкладышей коренных или шатунных подшипников коленчатого вала", "Произвести ремонт двигателя"],
    ["Сильный износ поршней, цилиндров, втулок шатунов, поршневых пальцев", "Произвести ремонт двигателя"],
    ["Слишком большой зазор между клапанами и коромыслами", "Отрегулировать зазоры между клапанами и коромыслами"],
]
TABLE_57 = [
    ["Причина неисправности", "Метод устранения"],
    ["Задир стержня клапана в направляющей втулке", "Произвести ремонт двигателя"],
    ["Ослабло крепление шкива коленчатого вала", "Подтянуть храповик коленчатого вала"],
    ["Неисправен подшипник водяного насоса", "Произвести ремонт водяного насоса или заменить насос"],
    ["Неисправен подшипник генератора", "Заменить подшипник"],
    ["Детонационное сгорание", None],
    ["Раннее зажигание", "Установить правильно момент зажигания"],
    ["Перегрев двигателя", "Устранить причину перегрева"],
]


def table_text(rows):
    return "\n".join(" | ".join(cell for cell in row if cell) for row in rows)


class Zmz5234OilKnockFlowTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        raw = root / "data/raw"
        raw.mkdir(parents=True)
        notes = root / "data/annotations"
        notes.mkdir()
        pages = {23: LAMP_TEXT, 28: LEVEL_TEXT, 56: table_text(TABLE_56), 57: table_text(TABLE_57)}
        pdf = raw / "zmz-5234.pdf"
        with pymupdf.open() as guide:
            for page in range(1, 58):
                sheet = guide.new_page()
                if page in pages:
                    sheet.insert_htmlbox(sheet.rect, "<p>" + pages[page] + "</p>")
            guide.save(pdf)
        manual = dict(document_id=DOCUMENT, title="Руководство ЗМЗ-5234.10", path="data/raw/zmz-5234.pdf",
                      edition="2025", source_sha256=sha256(pdf.read_bytes()).hexdigest(), page_count=57,
                      engine_models=[ENGINE])
        vehicle = dict(vehicle_id=CASE["machine_id"], model="Автобус", engine_model=ENGINE,
                       sessions=[dict(session_id=CASE["check_id"], vehicle_id=CASE["machine_id"], status="anomaly")])
        diagnostic = dict(vehicle_id=CASE["machine_id"], session_id=CASE["check_id"], status="anomaly",
                          anomaly_type="F02", anomaly_title="Низкое давление масла", signal_quality="good",
                          confidence=.74, anomaly_score=.8, model_version="test",
                          description="Загорается сигнализатор аварийного давления масла, слышен глухой стук "
                                      "на оборотах выше холостых")
        (raw / "vehicles.json").write_text(json.dumps({"vehicles": [vehicle]}), encoding="utf8")
        (notes / "manuals.json").write_text(json.dumps([manual]), encoding="utf8")

        hits = [dict(manual, page=page, text=body, applicability=[ENGINE + "|all"],
                     section="Стуки в двигателе" if page in (56, 57) else "Система смазки",
                     kind="table" if page in (56, 57) else "text",
                     tables=[dict(rows=TABLE_56 if page == 56 else TABLE_57)] if page in (56, 57) else [])
                for page, body in pages.items()]

        class Backend:
            def search(self, engine, query, *, limit, variant):
                return hits if engine == ENGINE else []

        self.sender = Mock(return_value="TKT-s0139")
        self.model = Mock()
        self.model.analyze_turn.return_value = None
        self.enterContext(patch.object(web, "fetch_diagnosis", return_value=diagnostic))
        self.enterContext(patch.object(web, "submit_order", self.sender))
        self.url = self.enterContext(running(web.make_server(("127.0.0.1", 0), root=root, search=Backend(),
                                                             orders_db=root / "orders.sqlite", answer_model=self.model)))

    def post(self, path, body):
        request = Request(self.url + path, data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def diagnose(self):
        return self.post("/api/diagnose", CASE)

    def ask(self, question, previous):
        fields = {name: previous[name] for name in ("topic", "context_id", "dialogue_id") if previous.get(name)}
        return self.post("/api/ask", CASE | {"question": question} | fields)

    def draft(self, response):
        self.assertTrue(response.get("context_id"), response["text"])
        return self.post("/api/orders/draft", {"context_id": response["context_id"]})

    @staticmethod
    def version(draft):
        return {key: draft[key] for key in ("context_id", "draft_id", "revision")}

    def test_model_adds_only_supported_knock_details_to_the_dialogue(self):
        high = self.ask("на высоких оборотах", self.diagnose())
        separate = self.ask("Глухой стук был слышен отдельно", high)
        quote = "в районе клапанной крышки"
        rpm = "мотор еле крутился"
        self.model.analyze_turn.return_value = ({
            "query": "",
            "knock": [
                {"field": "where", "value": "top", "quote": quote},
                {"field": "rpm", "value": "idle", "quote": rpm},
                {"field": "load", "value": "loaded", "quote": "этого в реплике нет"},
            ],
        }, {"total_tokens": 50})
        reply = self.ask(f"Грохот {quote}, когда {rpm}.", separate)
        self.assertTrue(self.model.analyze_turn.call_args.kwargs["oil_knock"])
        self.assertIn("сверху", reply["text"].casefold())
        self.assertIn("холост", reply["text"].casefold())
        self.assertIn("При какой нагрузке", reply["text"])
        self.assertTrue({56, 57} <= {row["page"] for row in reply["sources"]})
        self.assertEqual(self.draft(reply)["payload"]["works"], [LEVEL_WORK])
        self.assertEqual(reply["analysis_usage"], {"total_tokens": 50})

    def test_model_cannot_use_lamp_speed_as_knock_speed(self):
        initial = self.diagnose()
        self.model.analyze_turn.return_value = ({
            "query": "",
            "knock": [{"field": "rpm", "value": "high", "quote": "на высоких оборотах"}],
        }, {})
        high = self.ask("на высоких оборотах", initial)
        context = json.loads(high["topic"])[2]
        self.assertIn("danger", context)
        self.assertNotIn("rpm=", context)

    def test_initial_dialog_distinguishes_lamp_speed_and_knock(self):
        response = self.diagnose()
        self.assertIn(QUESTION, response["text"])
        self.assertTrue({23, 28} <= {source["page"] for source in response["sources"]})
        draft = self.draft(response)
        self.assertEqual(draft["payload"]["works"], [LEVEL_WORK])
        self.assertNotIn(REPAIR_WORK.lower(), response["text"].lower())

    def test_confirmed_lamp_at_speed_requires_stop_and_keeps_initial_check(self):
        for observation in ("Лампа загорелась на средних оборотах. Стук был одновременно.",
                            "На высоких оборотах загорелся сигнализатор, стук был одновременно."):
            with self.subTest(observation=observation):
                initial = self.diagnose()
                response = self.ask(observation, initial)
                text = response["text"].casefold()
                self.assertRegex(text, r"(?:немедленно заглушите|заглушите двигатель немедленно)")
                self.assertRegex(text, r"до устранения|после устранения")
                self.assertTrue({23, 28} <= {source["page"] for source in response["sources"]})
                self.assertNotRegex(text, r"увеличьте обороты|поднимите обороты|повышайте обороты")
                current = response if response.get("context_id") else initial
                self.assertEqual(self.draft(current)["payload"]["works"], [LEVEL_WORK])

    def test_short_speed_answer_continues_lamp_dialog(self):
        initial = self.diagnose()
        response = self.ask("на высоких оборотах", initial)
        text = response["text"].casefold()
        self.assertIn("немедленно заглушите двигатель", text)
        self.assertIn("глухой стук был слышен одновременно", text)
        self.assertNotIn("на каких оборотах загорелся сигнализатор", text)
        self.assertIn(23, {source["page"] for source in response["sources"]})
        self.assertEqual(self.draft(response)["payload"]["works"], [LEVEL_WORK])

    def test_short_speed_after_knock_question_preserves_lamp_observation(self):
        initial = self.diagnose()
        knock = self.ask("На средних и высоких оборотах сигнализатор не загорался. Глухой стук сохраняется.",
                         initial)
        response = self.ask("На высоких оборотах.", knock)
        text = response["text"].casefold()
        self.assertIn("уточните, откуда идёт стук и при какой нагрузке он слышен", text)
        self.assertNotIn("немедленно заглушите двигатель", text)
        self.assertTrue({56, 57} <= {source["page"] for source in response["sources"]})

    def test_short_speed_without_current_lamp_question_does_not_confirm_lamp(self):
        initial = self.diagnose()
        other_topic = json.dumps([CASE["machine_id"], CASE["check_id"], "oil-grade"])
        for previous in ({}, initial | {"topic": other_topic}, initial | {"context_id": "другой контекст"}):
            with self.subTest(previous=bool(previous)):
                response = self.ask("на высоких оборотах", previous)
                self.assertNotIn("немедленно заглушите двигатель", response["text"].casefold())
                self.assertIn(QUESTION, response["text"])

    def test_separate_knock_after_high_lamp_continues_with_past_observations(self):
        high = self.ask("на высоких оборотах", self.diagnose())
        separate = self.ask("Глухой стук был слышен отдельно", high)
        text = separate["text"]
        self.assertIn("Стук был слышен отдельно от сигнала", text)
        self.assertIn("Повторный пуск — после устранения неисправности", text)
        self.assertIn("Откуда тогда шёл стук?", text)
        self.assertIn("На каких оборотах его слышали?", text)
        self.assertNotIn("Глухой стук был слышен одновременно с сигналом или отдельно?", text)
        self.assertTrue({23, 56, 57} <= {source["page"] for source in separate["sources"]})
        self.assertEqual(self.draft(separate)["payload"]["works"], [LEVEL_WORK])

        details = self.ask("стук идет из верхней части двигателя на холостых оборотах", separate)
        self.assertIn("из верхней части двигателя, на холостых оборотах", details["text"].casefold())
        self.assertIn("При какой нагрузке его слышали?", details["text"])
        self.assertNotIn("Откуда тогда шёл стук?", details["text"])
        self.assertNotIn("На каких оборотах его слышали?", details["text"])

        observations = self.ask("под нагрузкой", details)
        text = observations["text"].casefold()
        self.assertIn("стук был слышен из верхней части двигателя, на холостых оборотах, под нагрузкой", text)
        self.assertIn("повторный пуск — после устранения неисправности", text)
        self.assertNotIn("откуда тогда шёл стук", text)
        self.assertNotIn("на каких оборотах его слышали", text)
        self.assertNotIn("при какой нагрузке его слышали", text)
        self.assertEqual(self.draft(observations)["payload"]["works"], [LEVEL_WORK])

    def test_short_knock_timing_before_and_after_lamp_speed(self):
        for wording, expected in (("одновременно", "стук совпал по времени с сигналом"),
                                  ("отдельно", "стук был слышен отдельно"),
                                  ("стука не было", "стука не было")):
            for speed_first in (True, False):
                with self.subTest(wording=wording, speed_first=speed_first):
                    initial = self.diagnose()
                    phrases = ["на высоких оборотах", wording] if speed_first else [wording, "на высоких оборотах"]
                    part = self.ask(phrases[0], initial)
                    response = self.ask(phrases[1], part)
                    self.assertIn(expected, (response if speed_first else part)["text"].casefold())
                    text = response["text"].casefold()
                    self.assertIn("немедленно заглушите двигатель", text)
                    self.assertNotIn("глухой стук был слышен одновременно с сигналом или отдельно?", text)
                    self.assertNotIn("на каких оборотах загорелся сигнализатор", text)
                    self.assertEqual(self.draft(response)["payload"]["works"], [LEVEL_WORK])

    def test_short_combined_observations_answer_both_questions(self):
        for message in ("На высоких оборотах, одновременно.", "Отдельно. На средних оборотах.",
                        "На высоких оборотах. Стука не было."):
            with self.subTest(message=message):
                answer = self.ask(message, self.diagnose())
                text = answer["text"].casefold()
                self.assertIn("немедленно заглушите двигатель", text)
                self.assertNotIn("глухой стук был слышен одновременно с сигналом или отдельно?", text)
                self.assertNotIn("на каких оборотах загорелся сигнализатор", text)

    def test_idle_answer_requests_lamp_state_at_higher_speed(self):
        initial = self.diagnose()
        idle = self.ask("На холостом ходу, стука не было.", initial)
        self.assertIn("Горел ли сигнализатор на средних или высоких оборотах?", idle["text"])
        self.assertNotIn("Немедленно заглушите двигатель", idle["text"])
        self.assertNotIn("Глухой стук был слышен одновременно с сигналом или отдельно?", idle["text"])
        normal = self.ask("нет", idle)
        self.assertNotIn("Горел ли сигнализатор на средних или высоких оборотах?", normal["text"])
        self.assertNotIn("Немедленно заглушите двигатель", normal["text"])
        new_idle = self.ask("На холостом ходу, стука не было.", self.diagnose())
        danger = self.ask("да", new_idle)
        self.assertIn("Немедленно заглушите двигатель", danger["text"])

    def test_knock_location_and_idle_speed_in_one_sentence(self):
        initial = self.diagnose()
        knock = self.ask("На средних и высоких оборотах сигнализатор не загорался. Глухой стук сохраняется.",
                         initial)
        answer = self.ask("стук идет из верхней части двигателя на холостых оборотах", knock)
        text = answer["text"].casefold()
        self.assertIn("из верхней части двигателя, на холостых оборотах", text)
        self.assertIn("при какой нагрузке он слышен?", text)
        self.assertNotIn("откуда идёт стук", text)
        self.assertNotIn("при каких оборотах он слышен?", text)
        self.assertNotIn("немедленно заглушите двигатель", text)
        self.assertTrue({56, 57} <= {source["page"] for source in answer["sources"]})

    def test_knock_details_only_request_missing_observations(self):
        initial = self.diagnose()
        knock = self.ask("На средних и высоких оборотах сигнализатор не загорался. Глухой стук сохраняется.",
                         initial)
        speed = self.ask("На высоких оборотах", knock)
        place = self.ask("Спереди", speed)
        self.assertIn("При какой нагрузке", place["text"])
        self.assertNotIn("Откуда идёт стук", place["text"])
        load = self.ask("Под нагрузкой", place)
        text = load["text"].casefold()
        self.assertIn("спереди", text)
        self.assertIn("под нагрузкой", text)
        self.assertNotIn("уточните, откуда", text)
        self.assertNotIn("при какой нагрузке", text)
        self.assertTrue({56, 57} <= {source["page"] for source in load["sources"]})
        self.assertEqual(self.draft(load)["payload"]["works"], [LEVEL_WORK])

    def test_knock_without_lamp_at_speed_reads_only_the_knock_group(self):
        initial = self.diagnose()
        response = self.ask("Лампа загоралась только на минимальном холостом ходу. "
                            "На средних и высоких оборотах сигнализатор не загорался. Глухой стук сохраняется.", initial)
        text = response["text"].casefold()
        self.assertRegex(text, r"(?:откуда|где|источник|локализ)")
        self.assertRegex(text, r"оборот")
        self.assertTrue({56, 57} <= {source["page"] for source in response["sources"]}, response["text"])
        self.assertIn("ослабло крепление шкива", text)
        self.assertNotRegex(text, r"раннее зажигание|износ шестерен|произвести ремонт|заменить подшипник")
        self.assertEqual(self.draft(response if response.get("context_id") else initial)["payload"]["works"], [LEVEL_WORK])

    def test_confirmed_bearing_wear_sources_repair_and_withdrawal_blocks_old_draft(self):
        initial = self.diagnose()
        hypothetical = self.ask("Что делать, если " + BEARING_WEAR.lower() + " подтвердится?", initial)
        self.assertEqual(self.draft(hypothetical if hypothetical.get("context_id") else initial)["payload"]["works"],
                         [LEVEL_WORK])
        confirmed = self.ask(BEARING_WEAR + " подтверждён при дефектовке.", initial)
        self.assertTrue(any(source["url"].endswith(".pdf#page=56") for source in confirmed["sources"]))
        repair = self.draft(confirmed)
        self.assertIn(REPAIR_WORK, [work.rstrip(".") for work in repair["payload"]["works"]])
        self.assertIn(56, {source["page"] for source in repair["payload"]["sources"]})
        revoked = self.ask(BEARING_WEAR + " не подтвердился.", confirmed)
        if revoked.get("context_id"):
            self.assertNotIn(REPAIR_WORK, [work.rstrip(".") for work in self.draft(revoked)["payload"]["works"]])
        with self.assertRaises(HTTPError) as error:
            self.post("/api/orders/send", self.version(repair) | {"confirmed": True})
        self.assertIn(error.exception.code, (400, 409))
        self.sender.assert_not_called()

    def test_manual_repair_does_not_confirm_engine_wear(self):
        draft = self.draft(self.diagnose())
        self.assertNotIn(56, {source["page"] for source in draft["payload"]["sources"]})
        self.assertEqual(draft["payload"]["works"], [LEVEL_WORK])

        edited = self.post("/api/orders/update", self.version(draft) | {"changes": {"works": [REPAIR_WORK]}})
        self.assertGreater(edited["revision"], draft["revision"])
        self.assertEqual(edited["payload"]["works"], [REPAIR_WORK])
        self.assertEqual(edited["manual_works"], [REPAIR_WORK])
        self.assertEqual(edited["payload"]["diagnosis"], draft["payload"]["diagnosis"])
        self.assertEqual(edited["payload"]["sources"], draft["payload"]["sources"])
        self.assertEqual(self.post("/api/orders/draft", {"context_id": draft["context_id"]}), edited)
        self.sender.assert_not_called()

    def test_confirmed_repair_can_be_selected_and_submitted(self):
        initial = self.diagnose()
        response = self.ask(BEARING_WEAR + " подтверждён при дефектовке.", initial)
        version = self.version(self.draft(response))
        edited = self.post("/api/orders/update", version | {"changes": {"works": [REPAIR_WORK]}})
        self.assertGreater(edited["revision"], version["revision"])
        self.assertEqual(edited["payload"]["works"], [REPAIR_WORK])
        self.assertEqual(edited["manual_works"], [])
        pages = {source["page"] for source in edited["payload"]["sources"]}
        self.assertIn(56, pages)
        self.assertNotIn(28, pages)

        with self.assertRaises(HTTPError) as stale:
            self.post("/api/orders/send", version | {"confirmed": True})
        self.assertEqual(stale.exception.code, 409)
        self.sender.assert_not_called()
        sent = self.post("/api/orders/send", self.version(edited) | {"confirmed": True})
        self.assertEqual(sent["number"], "TKT-s0139")
        self.sender.assert_called_once()
