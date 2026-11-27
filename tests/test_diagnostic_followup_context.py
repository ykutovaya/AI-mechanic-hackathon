"""Проверяет ответы веб-чата на уточнения по текущему заключению ML."""

import json
import unittest

import test_cooling_inspection as cooling
import test_f08_draft as diesel
import test_yamz_oil_pressure as oil


PRESSURE = "Как проверить давление масла и какие предупреждения даны для этого двигателя?"
COOLING = "Какие проверки нужны при перегреве и подозрении на охлаждающую жидкость в масле?"
FUEL = "Как проверить дизельную топливоподачу по результату этой диагностики?"


class FollowupContextTests(unittest.TestCase):
    def fixture(self, case_type):
        fixture = case_type()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        return fixture

    def assert_followups(self, server, case, questions, pages, terms, *, draft_expected=True):
        first = server.post("/api/diagnose", case)
        draft = None
        if draft_expected:
            self.assertTrue(first["context_id"])
            draft = server.post("/api/orders/draft", {"context_id": first["context_id"]})
            self.assertTrue(draft["payload"]["works"])
        else:
            self.assertFalse(first.get("context_id"))
        for i, question in enumerate(questions):
            with self.subTest(check=case["check_id"], question=question):
                body = case | {"question": question}
                # Контрольный прогон передаёт только пару идентификаторов, браузер также передаёт тему ответа.
                if i:
                    body["topic"] = first["topic"]
                    if first.get("context_id"):
                        body["context_id"] = first["context_id"]
                reply = server.post("/api/ask", body)
                if not draft_expected:
                    self.assertFalse(reply.get("context_id"))
                sources = {s["page"] for s in reply["sources"]}
                self.assertEqual(sources, pages)
                for term in terms:
                    self.assertIn(term.casefold(), reply["text"].casefold())
                for source in reply["sources"]:
                    self.assertTrue(source["url"].endswith(f"#page={source['page']}"))
        if draft:
            saved = server.post("/api/orders/draft", {"context_id": first["context_id"]})
            self.assertEqual(saved["revision"], draft["revision"])
            self.assertEqual(saved["payload"], draft["payload"])
        return first

    def test_low_oil_pressure_sources_and_unanswered_specific_norm(self):
        server = self.fixture(oil.YamzOilPressureFlowTests)
        case = server.selection("ЯМЗ-236М2")
        first = self.assert_followups(
            server, case, (PRESSURE, "Что проверить при низком давлении масла?", "Что проверить в первую очередь?"),
            {4, 53}, ("300 кПа", "номинальных оборотах", "подозрении на негерметичность", "соединения маслопроводов"),
            draft_expected=False)
        combined = server.post("/api/ask", case | {"topic": first["topic"], "question": PRESSURE})
        # Предупреждение с порогом и проверка герметичности сами по себе не описывают измерение.
        limit = next((paragraph.casefold() for paragraph in combined["text"].split("\n\n")
                      if any(word in paragraph.casefold() for word in ("замер", "измерен"))
                      and any(word in paragraph.casefold() for word in ("не найден", "отсутств", "не описан",
                                                                        "не подтвержд", "не привед", "нет "))), "")
        self.assertTrue(limit, "Чат должен сказать, что не нашёл в источниках порядок измерения давления.")
        narrow = server.post("/api/ask", case | {"topic": first["topic"],
                             "question": "Какой норматив давления масла на прогретом двигателе?"})
        self.assertNotIn("при подозрении на негерметичность", narrow["text"].casefold())
        self.assertNotIn(4, {s["page"] for s in narrow["sources"]})

    def test_overheating_questions_preserve_belt_and_fan_for_each_engine(self):
        server = self.fixture(cooling.CoolingInspectionFlowTests)
        questions = (COOLING, "Что проверить, если двигатель перегревается?", "Что проверить в первую очередь?")
        self.assert_followups(server, server.selection(server.ZMZ), questions, {91, 98},
                              ("немедленно остановить двигатель", "подшипники натяжного и направляющих роликов"))
        self.assert_followups(server, server.selection(server.YAMZ), questions, {220},
                              ("Проверить работу вентилятора", "при наличии муфты"))

    def test_diesel_questions_preserve_engine_specific_procedure(self):
        server = self.fixture(diesel.DieselOrderFlowTests)
        questions = (FUEL, "Что проверить в системе подачи дизельного топлива?", "Что проверить в первую очередь?")
        for check, pages, terms in (
                ("S-0005", {30, 31, 53}, ("совмещения меток", "муфты опережения")),
                ("S-0090", {241, 242}, ("тест форсунок", "аскан-10", "холост"))):
            case = {"machine_id": diesel.CASES[check][0], "check_id": check}
            first = self.assert_followups(server, case, questions, pages, terms)
            changed = server.post("/api/ask", case | {
                "topic": first["topic"], "question": "Как заменить топливный фильтр?"})
            self.assertNotRegex(changed["text"].casefold(), r"по меткам|аскан-10")
            narrow = server.post("/api/ask", case | {
                "topic": first["topic"], "question": "Как измерить давление топлива в рампе манометром?"})
            self.assertNotRegex(narrow["text"].casefold(), r"по меткам|аскан-10")

    def test_previous_answer_does_not_replace_a_missing_source(self):
        server = self.fixture(cooling.CoolingInspectionFlowTests)
        case = server.selection(server.YAMZ)
        first = server.post("/api/diagnose", case)
        self.assertEqual([s["page"] for s in first["sources"]], [220])
        server.hits[server.YAMZ].clear()
        server.fault_hits[server.YAMZ].clear()
        reply = server.post("/api/ask", case | {"topic": first["topic"], "question": COOLING})
        self.assertFalse(reply["sources"])
        self.assertNotIn("при наличии муфты", reply["text"].casefold())

    def test_topic_for_another_check_does_not_supply_ml_symptoms(self):
        server = self.fixture(diesel.DieselOrderFlowTests)
        case = {"machine_id": diesel.CASES["S-0090"][0], "check_id": "S-0090"}
        first = server.post("/api/diagnose", case)
        other = case | {"check_id": "S-0091", "question": "Что проверить дальше?"}
        for topic in (first["topic"], json.dumps([case["machine_id"], "S-0091", "injector-test"])):
            with self.subTest(topic=topic):
                reply = server.post("/api/ask", other | {"topic": topic})
                self.assertIsNone(reply["diagnosis"])
                self.assertNotIn("аскан-10", reply["text"].casefold())
                self.assertFalse(reply.get("context_id"))
