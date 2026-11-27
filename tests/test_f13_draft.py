"""Проверяет переход от рекомендаций по F13 к черновику заявки и тестовой ТОиР."""

from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pymupdf

from ai_mechanic import web
from test_web_flow import running


class FuelOrderFlowTests(unittest.TestCase):
    CARB_ENGINE = "ЗМЗ-5234.10"
    INJECTION_ENGINE = "ЗМЗ-40904.10"
    VEHICLES = {
        "P227BB78": (CARB_ENGINE, ("S-0117", "S-0118")),
        "M925OX198": (INJECTION_ENGINE, ("S-0184", "S-0185")),
    }

    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        data = root / "data"
        (data / "raw").mkdir(parents=True)
        (data / "annotations").mkdir()
        cards = [
            dict(vehicle_id=machine, model=engine, engine_model=engine, fuel="бензин",
                 sessions=[dict(session_id=check, vehicle_id=machine, status="anomaly") for check in checks])
            for machine, (engine, checks) in self.VEHICLES.items()
        ]
        (data / "raw" / "vehicles.json").write_text(json.dumps({"vehicles": cards}), encoding="utf-8")
        guides = {}
        for engine, document, length in [
            (self.CARB_ENGINE, "carburetor-manual", 54),
            (self.INJECTION_ENGINE, "injection-manual", 102),
        ]:
            path = data / "raw" / f"{document}.pdf"
            with pymupdf.open() as pdf:
                for _ in range(length):
                    pdf.new_page()
                pdf.save(path)
            guides[engine] = dict(document_id=document, title=f"Руководство {engine}", edition="2025",
                                  path=path.relative_to(root).as_posix(), engine_models=[engine], page_count=length,
                                  source_sha256=sha256(path.read_bytes()).hexdigest())
        (data / "annotations" / "manuals.json").write_text(json.dumps(list(guides.values())), encoding="utf-8")

        def fragment(engine, page, title, cause, remedy, row, *, section="Возможные неисправности"):
            table = [["Вероятная причина", "Способ устранения"]]
            if title:
                table.append([title, None])
            table.append([cause, remedy])
            body = "\n".join(" | ".join(cell or "" for cell in line) for line in table)
            return dict(guides[engine], page=page, text=body, tables=[{"rows": table}],
                        kind="table", section=section, applicability=[f"{engine}|all"],
                        fault_code="F13", expert_rows=[row])

        rows_by_engine = {
            self.CARB_ENGINE: [
                fragment(self.CARB_ENGINE, 54,
                         "При резком открытии дроссельных заслонок двигатель чихает в карбюраторе.",
                         "Неправильная работа ускорительного насоса карбюратора.",
                         "Промыть распылитель в бензине и продуть сжатым воздухом. "
                         "Проверить состояние клапанов ускорительного насоса. Неисправные клапаны заменить.", 689),
                fragment(self.CARB_ENGINE, 53,
                         "Двигатель неустойчиво работает в режиме холостого хода.",
                         "Высокий или низкий уровень бензина в поплавковой камере карбюратора.",
                         "Проверить уровень топлива (см. раздел «Обслуживание системы питания»). "
                         "Если уровень топлива не совпадает с меткой, отрегулировать установку поплавка.", 692),
            ],
            self.INJECTION_ENGINE: [
                fragment(self.INJECTION_ENGINE, 102, None,
                         "а) не работает электробензонасос (ЭБН);",
                         "Проверить целостность предохранителя. Проверить исправность и надежность разъемов ЭБН, "
                         "пускового реле (ПР) и реле ЭБН. При включении зажигания должен быть слышен "
                         "характерный звук 2...3 с работы ЭБН.", 630,
                         section="Таблица 5. 1. Двигатель не пускается / 1.1. Нарушение подачи бензина"),
                fragment(self.INJECTION_ENGINE, 102, None,
                         "г) отсутствует контакт в электрической цепи катушек зажигания, блока управления;",
                         "проверить исправность и надежность разъемов. После каждой проверочной операции "
                         "разъема выполнить пробный пуск двигателя", 642,
                         section="Таблица 5. 1. Двигатель не пускается / 1.2. Неисправности "
                                 "в микропроцессорной системе управления двигателем"),
            ],
        }

        self.injection_hits = rows_by_engine[self.INJECTION_ENGINE]
        self.carb_hit = rows_by_engine[self.CARB_ENGINE][0]
        general_hits = self.general_hits = {}

        class Search:
            def search(self, engine, query, *, variant, limit):
                return general_hits.get(engine, [])

            def search_fault(self, engine, query, *, code, variant, limit):
                return rows_by_engine[engine] if code == "F13" else []

        def diagnose(_url, vehicle, check):
            engine = self.VEHICLES[vehicle][0]
            symptoms = ("Двигатель чихает в карбюратор при резком открытии дросселя, обороты холостого хода плавают."
                        if engine == self.CARB_ENGINE else
                        "При включении зажигания не слышно работы электробензонасоса, двигатель не пускается.")
            return dict(vehicle_id=vehicle, session_id=check, status="anomaly", anomaly_type="F13",
                        anomaly_title="Подача бензина", signal_quality="warning" if check == "S-0117" else "good",
                        description=symptoms, anomaly_score=0.8, confidence=0.48 if check == "S-0117" else 0.88,
                        model_version="test")

        self.tickets = []
        tickets = self.tickets

        class TestToir(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path != "/tickets":
                    self.send_error(404)
                    return
                tickets.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                answer = json.dumps({"ticket_id": "TKT-TEST", "status": "created"}).encode()
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(answer)

            def log_message(self, *args):
                pass

        toir = self.enterContext(running(ThreadingHTTPServer(("127.0.0.1", 0), TestToir)))
        self.enterContext(patch.object(web, "fetch_diagnosis", side_effect=diagnose))
        self.url = self.enterContext(running(web.make_server(
            ("127.0.0.1", 0), root=root, search=Search(), toir_url=toir, orders_db=root / "orders.sqlite")))

    def post(self, path, payload):
        req = Request(self.url + path, data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=5) as response:
            return json.load(response)

    def test_f13_draft_contains_checks_with_pdf_pages_and_sends_only_after_confirmation(self):
        examples = [
            ("P227BB78", "S-0117", {53, 54}, ("клапан", "ускорительн", "уровень топлива")),
            ("P227BB78", "S-0118", {53, 54}, ("клапан", "ускорительн", "уровень топлива")),
            ("M925OX198", "S-0184", {102}, ("предохранител", "разъ", "эбн")),
            ("M925OX198", "S-0185", {102}, ("предохранител", "разъ", "эбн")),
        ]
        for vehicle, check, pages, terms in examples:
            with self.subTest(check=check):
                reply = self.post("/api/diagnose", {"machine_id": vehicle, "check_id": check})
                self.assertEqual({source["page"] for source in reply["sources"]}, pages)
                self.assertTrue(reply["context_id"], "Применимые проверки найдены, но черновик недоступен.")
                context = {"context_id": reply["context_id"]}
                draft = self.post("/api/orders/draft", context)
                payload = draft["payload"]
                works = payload["works"]
                work_text = " ".join(works).casefold()
                for term in terms:
                    self.assertIn(term, work_text)
                self.assertTrue(all(work.casefold().startswith("проверить") for work in works), works)
                self.assertNotRegex(work_text, r"замен|регулиров|промы|продуть")
                self.assertEqual({source["page"] for source in payload["sources"]}, pages)
                self.assertEqual(payload["parts"], [])

                version = context | {key: draft[key] for key in ("draft_id", "revision")}
                previous_count = len(self.tickets)
                with self.assertRaises(HTTPError) as declined:
                    self.post("/api/orders/send", version | {"confirmed": False})
                self.assertEqual(declined.exception.code, 400)
                self.assertEqual(len(self.tickets), previous_count)

                sent = self.post("/api/orders/send", version | {"confirmed": True})
                self.assertEqual(sent["number"], "TKT-TEST")
                self.assertEqual(len(self.tickets), previous_count + 1)
                ticket = self.tickets[-1]
                self.assertEqual((ticket["vehicle_id"], ticket["session_id"], ticket["anomaly_type"]),
                                 (vehicle, check, "F13"))
                self.assertEqual(ticket["operations"], works)
                self.assertEqual({ref["pdf_page"] for ref in ticket["references"]}, pages)

    def test_silent_pump_selects_the_fuel_supply_row_only(self):
        case = {"machine_id": "M925OX198", "check_id": "S-0184"}
        reply = self.post("/api/diagnose", case)
        self.assertNotIn("катушек", reply["text"].casefold())
        self.assertIn("предохранителя", reply["text"])
        self.assertNotRegex(reply["text"].casefold(), r";\.|\bа\) не работает")
        self.assertIn("реле ЭБН", reply["text"])
        self.assertEqual([(s["page"], s["expert_rows"]) for s in reply["sources"]], [(102, [630])])
        payload = self.post("/api/orders/draft", {"context_id": reply["context_id"]})["payload"]
        self.assertEqual(payload["works"], [
            "Проверить целостность предохранителя. Проверить исправность и надежность разъемов ЭБН, "
            "пускового реле (ПР) и реле ЭБН."])
        self.assertEqual([s["page"] for s in payload["sources"]], [102])
        self.assertEqual(payload["parts"], [])

    def test_missing_pump_row_does_not_make_ignition_a_pump_check(self):
        self.injection_hits.pop(0)
        reply = self.post("/api/diagnose", {"machine_id": "M925OX198", "check_id": "S-0184"})
        self.assertNotIn("катушек", reply["text"].casefold())
        self.assertFalse(reply["sources"])
        self.assertFalse(reply.get("context_id"))

    def test_fuel_supply_followups_keep_the_checks_for_each_engine(self):
        examples = [
            ("P227BB78", "S-0117", {53, 54}, ("клапан", "ускорительного насоса", "уровень топлива")),
            ("M925OX198", "S-0184", {102}, ("предохранител", "разъемов эбн", "реле эбн", "2...3 с")),
        ]
        for vehicle, check, pages, terms in examples:
            case = {"machine_id": vehicle, "check_id": check}
            first = self.post("/api/diagnose", case)
            context = {"context_id": first["context_id"]}
            before = self.post("/api/orders/draft", context)
            for question in ("Как проверить подачу бензина по результату этой диагностики?",
                             "С чего начать проверку системы питания?",
                             "С чего начать проверку топливоподачи в моём случае?"):
                with self.subTest(check=check, question=question):
                    reply = self.post("/api/ask", case | context | {"question": question, "topic": first["topic"]})
                    self.assertEqual(pages, {source["page"] for source in reply["sources"]})
                    text = reply["text"].casefold()
                    for term in terms:
                        self.assertIn(term, text)
                    self.assertNotRegex(text, r"разборк|диафрагм|заменить|промыть|продуть|отрегулировать|катушек")
            after = self.post("/api/orders/draft", context)
            self.assertEqual((before["payload"], before["revision"]), (after["payload"], after["revision"]))

    def test_fuel_supply_context_does_not_replace_a_specific_question_or_missing_source(self):
        case = {"machine_id": "M925OX198", "check_id": "S-0184"}
        first = self.post("/api/diagnose", case)
        specific = self.post("/api/ask", case | {"topic": first["topic"],
                             "question": "Как измерить давление топлива в рампе манометром?"})
        self.assertNotIn("предохранителя", specific["text"].casefold())
        self.injection_hits.clear()
        missing = self.post("/api/ask", case | {"question": "Как проверить подачу бензина?"})
        self.assertFalse(missing["sources"])
        self.assertIn("подачи бензина", missing["text"].casefold())
        self.assertNotRegex(missing["text"].casefold(), r"разобрать|заменить|проверить предохранитель")

    def test_f13_assessment_does_not_start_pump_disassembly(self):
        case = {"machine_id": "P227BB78", "check_id": "S-0117"}
        first = self.post("/api/diagnose", case)
        context = {"context_id": first["context_id"]}
        self.general_hits[self.CARB_ENGINE] = [dict(self.carb_hit, page=52, section="Разборка бензонасоса",
            kind="procedure", tables=[], text="Разборка бензонасоса\n1. Отвернуть винты крышки.\n2. Снять крышку.")]
        before = self.post("/api/orders/draft", context)
        for question in ("Нужно ли разобрать бензонасос?", "По этим симптомам уже стоит снимать бензонасос?"):
            with self.subTest(question=question):
                reply = self.post("/api/ask", case | context | {"question": question})
                self.assertEqual({53, 54}, {source["page"] for source in reply["sources"]})
                self.assertIn("для разборки", reply["text"].casefold())
                self.assertNotIn("снять крышку", reply["text"].casefold())
        procedure = self.post("/api/ask", case | context | {"question": "Насос уже снят. Как разобрать бензонасос?"})
        self.assertEqual({52}, {source["page"] for source in procedure["sources"]})
        self.assertIn("снять крышку", procedure["text"].casefold())
        self.assertEqual(before["payload"], self.post("/api/orders/draft", context)["payload"])

    def test_f13_edit_keeps_only_the_reference_for_remaining_check(self):
        reply = self.post("/api/diagnose", {"machine_id": "P227BB78", "check_id": "S-0118"})
        context = {"context_id": reply["context_id"]}
        draft = self.post("/api/orders/draft", context)
        work, = [item for item in draft["payload"]["works"] if "уровень топлива" in item.casefold()]
        version = context | {key: draft[key] for key in ("draft_id", "revision")}
        updated = self.post("/api/orders/update", version | {"changes": {"works": [work]}})
        self.assertGreater(updated["revision"], draft["revision"])
        self.assertEqual(updated["payload"]["works"], [work])
        self.assertEqual([source["page"] for source in updated["payload"]["sources"]], [53])
        with self.assertRaises(HTTPError) as stale:
            self.post("/api/orders/send", version | {"confirmed": True})
        self.assertEqual(stale.exception.code, 409)
        self.assertEqual(self.tickets, [])
