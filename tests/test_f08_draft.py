"""Проверяет оформление заявки по подтверждённым проверкам дизельной топливоподачи."""

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


MECHANICAL = "ЯМЗ-236М2"
ELECTRONIC = "ЯМЗ-536"
CASES = {
    "S-0005": ("M333YK47", MECHANICAL, "Жёсткая работа с резким стуком в цилиндрах, дымный выхлоп", 0.73),
    "S-0090": ("C935KO47", ELECTRONIC, "Неравномерная работа с жёстким стуком в одном цилиндре на холостом ходу", 0.94),
    "S-0091": ("C935KO47", ELECTRONIC, "Неравномерная работа с жёстким стуком в одном цилиндре на холостом ходу", 0.91),
}


class DieselOrderFlowTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        data = root / "data"
        (data / "raw").mkdir(parents=True)
        (data / "annotations").mkdir()
        cards = {}
        for check, (machine, engine, _, _) in CASES.items():
            card = cards.setdefault(machine, dict(vehicle_id=machine, model=engine, engine_model=engine,
                                                  fuel="дизель", sessions=[]))
            card["sessions"].append(dict(session_id=check, vehicle_id=machine, status="anomaly"))
        (data / "raw" / "vehicles.json").write_text(json.dumps({"vehicles": list(cards.values())}), encoding="utf-8")

        guides = {}
        for engine, document, pages in [(MECHANICAL, "yamz-236m2-re-2010", 53), (ELECTRONIC, "yamz-536-re-2016", 242)]:
            path = data / "raw" / f"{document}.pdf"
            with pymupdf.open() as pdf:
                for _ in range(pages):
                    pdf.new_page()
                pdf.save(path)
            guides[engine] = dict(document_id=document, title=f"Руководство по эксплуатации {engine}",
                                  edition="2010" if engine == MECHANICAL else "2016", page_count=pages,
                                  path=path.relative_to(root).as_posix(), engine_models=[engine],
                                  source_sha256=sha256(path.read_bytes()).hexdigest())
        (data / "annotations" / "manuals.json").write_text(json.dumps(list(guides.values())), encoding="utf-8")

        def hit(engine, page, section, text, **extra):
            return dict(guides[engine], page=page, section=section, text=text,
                        applicability=[f"{engine}|all"], **extra)

        advance = "ПРОВЕРКА И РЕГУЛИРОВКА УГЛА ОПЕРЕЖЕНИЯ ВПРЫСКИВАНИЯ ТОПЛИВА"
        injector_test = "№10 ТЕСТ ФОРСУНОК"
        table = [["Причина", "Способ устранения"], ["Двигатель стучит", None],
                 ["Ранний впрыск топлива в цилиндры", "Отрегулировать угол опережения впрыскивания топлива"],
                 ["Нарушена регулировка клапанного механизма", "Отрегулировать тепловые зазоры"]]
        fragments = {
            MECHANICAL: [
                hit(MECHANICAL, 53, "Возможные неисправности",
                    "\n".join(" | ".join(cell or "" for cell in row) for row in table),
                    tables=[{"rows": table}], kind="table"),
                hit(MECHANICAL, 30, advance, advance + "\n"
                    "Вращать коленчатый вал двигателя по часовой стрелке (если смотреть со стороны вентилятора) "
                    "до совмещения меток на шкиве коленчатого вала и крышке шестерен распределения или на маховике "
                    "с указателем. Установочный угол 15º. Для бокового указателя букве «Б» соответствует 15º."),
                hit(MECHANICAL, 31, advance,
                    "Для двигателя ЯМЗ-238КМ2 установочный угол опережения впрыскивания равен 10º.\n"
                    "В момент совмещения меток, показанных на рис. 15 и 16, должны совместиться метка на торце "
                    "муфты опережения впрыскивания с риской на указателе 8. Если метки не совместились, "
                    "отвернуть две гайки и поворотом муфты совместить метки.", kind="text"),
            ],
            ELECTRONIC: [
                hit(ELECTRONIC, 241, "Перечень диагностических работ", injector_test + "\n"
                    "1. Подключить диагностический прибор и проверить наличие ошибок в ЭСУ.\n"
                    "2. Провести тест форсунок при частоте вращения холостого хода 1000 мин-1 (АСКАН-10) "
                    "или в режиме минимального холостого хода (KTS).", kind="text"),
                hit(ELECTRONIC, 242, "Перечень диагностических работ",
                    "3. Вычислить разницу между полученными значениями частоты вращения для каждого цилиндра "
                    "до их отключения и после.\n"
                    "4. Определить среднее значение этой разницы для цилиндров, в которых она находится "
                    "примерно на одном уровне.\n"
                    "5. Отклонение от среднего значения не должно превышать ±15 мин-1.\n"
                    "6. При необходимости, заменить распылители или форсунки.\n"
                    "7. После устранения неисправности удалить ошибки.\n"
                    "№11 ТЕСТ КОМПРЕССИИ (ТОЛЬКО АСКАН-10)\n"
                    "1. Подключить диагностический прибор и проверить наличие ошибок в ЭСУ.", kind="text"),
            ],
        }

        class Search:
            def search(self, engine, query, *, variant, limit):
                return fragments.get(engine, [])

            def search_fault(self, engine, query, *, code, variant, limit):
                if code != "F08":
                    return []
                return [dict(item, fault_code=code) for item in fragments.get(engine, [])]

        def diagnose(_url, vehicle, check):
            machine, _engine, symptoms, confidence = CASES[check]
            self.assertEqual(machine, vehicle)
            return dict(vehicle_id=vehicle, session_id=check, status="anomaly", anomaly_type="F08",
                        anomaly_title="Дизельная топливоподача", signal_quality="good",
                        description=symptoms, anomaly_score=0.8, confidence=confidence, model_version="test")

        self.tickets = []
        tickets = self.tickets

        class TestToir(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path != "/tickets":
                    self.send_error(404)
                    return
                tickets.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                answer = json.dumps({"ticket_id": "TKT-F08-TEST", "status": "created"}).encode()
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
        request = Request(self.url + path, data=json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def test_f08_checks_use_physical_pdf_pages_and_require_confirmed_send(self):
        for check, (vehicle, engine, _, _) in CASES.items():
            with self.subTest(check=check):
                reply = self.post("/api/diagnose", {"machine_id": vehicle, "check_id": check})
                case_pages = {30, 31, 53} if engine == MECHANICAL else {241, 242}
                draft_pages = {30, 31} if engine == MECHANICAL else case_pages
                self.assertTrue(reply["context_id"], "В источнике есть проверка, но черновик недоступен.")
                self.assertEqual({source["page"] for source in reply["sources"]}, case_pages)
                explanation = reply["text"].casefold()
                if engine == MECHANICAL:
                    for term in ("ранний впрыск", "муфт"):
                        self.assertIn(term, explanation)
                    self.assertRegex(explanation, r"\b15\s*[º°]")
                    self.assertNotRegex(explanation, r"\b10\s*[º°]")
                    operation = r"проверить угол опережения впрыск"
                else:
                    for term in ("аскан-10", "1000", "kts", "холост", "частот", "цилиндр", "средн"):
                        self.assertIn(term, explanation)
                    self.assertRegex(explanation, r"±\s*15")
                    self.assertNotIn("тест компрессии", explanation)
                    operation = r"провести тест форсунок №\s*10"

                context = {"context_id": reply["context_id"]}
                draft = self.post("/api/orders/draft", context)
                payload = draft["payload"]
                works = payload["works"]
                work_text = " ".join(works).casefold()
                self.assertRegex(work_text, operation)
                self.assertNotRegex(work_text, r"\b(?:замен|отрегулиров)")
                self.assertEqual({source["page"] for source in payload["sources"]}, draft_pages)
                self.assertEqual(payload["parts"], [])

                version = context | {key: draft[key] for key in ("draft_id", "revision")}
                previous_count = len(self.tickets)
                with self.assertRaises(HTTPError) as declined:
                    self.post("/api/orders/send", version | {"confirmed": False})
                self.assertEqual(declined.exception.code, 400)
                self.assertEqual(len(self.tickets), previous_count)
                edited_works = [work[:1].lower() + work[1:] for work in works]
                edited = self.post("/api/orders/update", version | {"changes": {"works": edited_works}})
                self.assertEqual(edited["payload"]["works"], edited_works)
                self.assertGreater(edited["revision"], version["revision"])
                self.assertEqual({source["page"] for source in edited["payload"]["sources"]}, draft_pages)
                with self.assertRaises(HTTPError) as stale:
                    self.post("/api/orders/send", version | {"confirmed": True})
                self.assertEqual(stale.exception.code, 409)
                self.assertEqual(len(self.tickets), previous_count)
                version["revision"] = edited["revision"]
                sent = self.post("/api/orders/send", version | {"confirmed": True})
                self.assertEqual(sent["number"], "TKT-F08-TEST")
                self.assertEqual(len(self.tickets), previous_count + 1)
                ticket = self.tickets[-1]
                self.assertEqual((ticket["vehicle_id"], ticket["session_id"], ticket["anomaly_type"]),
                                 (vehicle, check, "F08"))
                self.assertEqual(ticket["operations"], edited_works)
                self.assertEqual({ref["pdf_page"] for ref in ticket["references"]}, draft_pages)
