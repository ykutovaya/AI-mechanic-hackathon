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
        self.enterContext(patch.object(web, "fetch_diagnosis", return_value=diagnostic))
        self.enterContext(patch.object(web, "submit_order", self.sender))
        self.url = self.enterContext(running(web.make_server(("127.0.0.1", 0), root=root, search=Backend(),
                                                             orders_db=root / "orders.sqlite")))

    def post(self, path, body):
        request = Request(self.url + path, data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def diagnose(self):
        return self.post("/api/diagnose", CASE)

    def ask(self, question, previous):
        fields = {name: previous[name] for name in ("topic", "context_id") if previous.get(name)}
        return self.post("/api/ask", CASE | {"question": question} | fields)

    def draft(self, response):
        self.assertTrue(response.get("context_id"), response["text"])
        return self.post("/api/orders/draft", {"context_id": response["context_id"]})

    @staticmethod
    def version(draft):
        return {key: draft[key] for key in ("context_id", "draft_id", "revision")}

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

    def test_only_confirmed_repair_can_be_selected_and_submitted(self):
        initial = self.diagnose()
        base = self.version(self.draft(initial))
        with self.assertRaises(HTTPError) as error:
            self.post("/api/orders/update", base | {"changes": {"works": [REPAIR_WORK]}})
        self.assertEqual(error.exception.code, 400)
        response = self.ask(BEARING_WEAR + " подтверждён при дефектовке.", initial)
        version = self.version(self.draft(response))
        edited = self.post("/api/orders/update", version | {"changes": {"works": [REPAIR_WORK]}})
        self.assertGreater(edited["revision"], version["revision"])
        self.assertEqual(edited["payload"]["works"], [REPAIR_WORK])
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
