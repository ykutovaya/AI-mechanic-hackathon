"""Проверяет диалог и заявку по подшипнику генератора ЗМЗ-5234.10."""

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


DOCUMENT = "zmz-5234-10-rk-2025"
ENGINE = "ЗМЗ-5234.10"
ROW = "Неисправен подшипник генератора | Заменить подшипник"
QUESTION = "Удалось ли подтвердить, что шум идёт от подшипника генератора и подшипник неисправен?"
CONFIRMED = "Да, при проверке подтвердили неисправность подшипника генератора."
RUBBING = "При проверке обнаружили задевание ротора."
NO_RUBBING = "Задевания ротора нет."
UNCONFIRMED = "Неисправность подшипника генератора не подтверждена."
WORK = "Заменить подшипник генератора"


class GeneratorBearingDialogueTests(unittest.TestCase):
    selection = dict(machine_id="E069PP47", check_id="S-0136")

    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        data = root / "data"
        (data / "raw").mkdir(parents=True)
        (data / "annotations").mkdir()
        vehicle = dict(vehicle_id=self.selection["machine_id"], model="Автобус", engine_model=ENGINE,
                       sessions=[dict(session_id=self.selection["check_id"],
                                      vehicle_id=self.selection["machine_id"], status="anomaly")])
        (data / "raw/vehicles.json").write_text(json.dumps([vehicle]), encoding="utf-8")
        path = root / "guide.pdf"
        with pymupdf.open() as pdf:
            for _ in range(56):
                pdf.new_page()
            sheet = pdf.new_page()
            sheet.insert_htmlbox(sheet.rect, f"<p>{ROW}</p>")
            pdf.save(path)
        manual = dict(document_id=DOCUMENT, path=path.name, page_count=57, title="Руководство",
                      edition="2025", engine_models=[ENGINE], source_sha256=sha256(path.read_bytes()).hexdigest())
        (data / "annotations/manuals.json").write_text(json.dumps([manual]), encoding="utf-8")
        self.hits = {57: ROW}
        hits = self.hits

        class Search:
            def search(self, engine, query, *, variant, limit):
                return [dict(manual, page=page, text=text, applicability=[f"{ENGINE}|all"])
                        for page, text in hits.items()] if engine == ENGINE else []

        def diagnose(_url, machine, check):
            return dict(vehicle_id=machine, session_id=check, status="anomaly", signal_quality="good",
                        anomaly_type="F05", anomaly_title="Генератор",
                        description="Стук и гул в зоне генератора на холостом ходу")

        self.enterContext(patch.object(web, "fetch_diagnosis", side_effect=diagnose))
        self.sender = Mock(return_value="TKT-bearing")
        self.enterContext(patch.object(web, "submit_order", self.sender))
        self.url = self.enterContext(running(web.make_server(("127.0.0.1", 0), root=root, search=Search(),
                                                              orders_db="orders.sqlite")))

    def post(self, endpoint, **payload):
        request = Request(self.url + "/api/" + endpoint, data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=8) as response:
            return json.load(response)

    def begin(self):
        return self.post("diagnose", **self.selection)

    def ask(self, question, prior):
        fields = {"topic": prior["topic"]} if prior.get("topic") else {}
        if prior.get("context_id"):
            fields["context_id"] = prior["context_id"]
        return self.post("ask", **self.selection, question=question, **fields)

    def checked_draft(self, reply, priority):
        self.assertTrue(reply.get("context_id"), reply["text"])
        self.assertTrue(any(row["document_id"] == DOCUMENT and row["page"] == 57
                            and row["url"].endswith(".pdf#page=57") for row in reply["sources"]))
        draft = self.post("orders/draft", context_id=reply["context_id"])
        self.assertEqual(draft["payload"]["priority"], priority)
        self.assertEqual([work.rstrip(".") for work in draft["payload"]["works"]], [WORK])
        self.assertEqual([(ref["document_id"], ref["page"]) for ref in draft["payload"]["sources"]],
                         [(DOCUMENT, 57)])
        return draft

    @staticmethod
    def version(draft):
        return {key: draft[key] for key in ("context_id", "draft_id", "revision")}

    def test_diagnosis_asks_about_bearing_and_without_confirmation_recommends_generator_check(self):
        start = self.begin()
        self.assertIn("Удалось ли локализовать шум в генераторе?", start["text"])
        self.assertIn(QUESTION, start["text"])
        self.assertFalse(start["context_id"])
        for wording in ("Шум локализовали в генераторе. Подшипник ещё не проверяли.",
                        UNCONFIRMED, RUBBING):
            with self.subTest(wording=wording):
                reply = self.ask(wording, start)
                self.assertFalse(reply.get("context_id"))
                self.assertRegex(reply["text"].casefold(), r"провер\w*.{0,45}генератор")
        self.sender.assert_not_called()

    def test_confirmed_work_with_rotor_observation_and_withdrawal(self):
        medium = self.ask(CONFIRMED, self.begin())
        initial = self.checked_draft(medium, "medium")
        theoretical = self.ask("Может ли ротор задевать?", medium)
        self.assertEqual(self.post("orders/draft", context_id=medium["context_id"]), initial)

        high = self.ask(RUBBING, medium | {"topic": theoretical.get("topic") or medium["topic"]})
        urgent = self.checked_draft(high, "high")
        self.assertIn("задевани", urgent["priority_reason"].casefold())
        self.assertIn("ротор", urgent["priority_reason"].casefold())
        self.assertEqual(self.checked_draft(self.ask(RUBBING, high), "high"), urgent)
        for version, confirmed in ((self.version(initial), True), (self.version(urgent), False)):
            with self.assertRaises(HTTPError) as error:
                self.post("orders/send", **version, confirmed=confirmed)
            self.assertIn(error.exception.code, (400, 409))

        restored = self.ask(NO_RUBBING, high)
        current = self.checked_draft(restored, "medium")
        with self.assertRaises(HTTPError):
            self.post("orders/send", **self.version(urgent), confirmed=True)

        revoked = self.ask(UNCONFIRMED, restored)
        self.assertFalse(revoked.get("context_id"))
        with self.assertRaises(HTTPError):
            self.post("orders/send", **self.version(current), confirmed=True)
        self.sender.assert_not_called()

    def test_combined_report_sends_only_after_confirmation_and_preserves_history(self):
        start = self.begin()
        reply = self.ask(CONFIRMED + " " + RUBBING, start)
        draft = self.checked_draft(reply, "high")
        sent = self.post("orders/send", **self.version(draft), confirmed=True)
        self.assertEqual(sent["number"], "TKT-bearing")
        self.assertEqual(self.post("orders/send", **self.version(draft), confirmed=True), sent)
        self.ask(UNCONFIRMED, reply)
        history = self.post("orders/history", machine_id=self.selection["machine_id"])["orders"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["payload"], sent["payload"])
        self.assertIn("задевани", history[0]["priority_reason"].casefold())
        self.sender.assert_called_once()

    def test_foreign_part_or_uncertain_rotor_contact_does_not_change_draft(self):
        start = self.begin()
        other = self.ask("Подтвердили неисправность подшипника водяного насоса.", start)
        self.assertFalse(other.get("context_id"))
        medium = self.ask(CONFIRMED, start)
        initial = self.checked_draft(medium, "medium")
        for wording in ("Мы не смогли исключить, что ротор задевает.",
                        "Обнаружили задевание ротора другого агрегата.",
                        "Возможно, ротор задевает."):
            with self.subTest(wording=wording):
                reply = self.ask(wording, medium)
                self.assertFalse(reply.get("context_id"))
                self.assertEqual(self.post("orders/draft", context_id=medium["context_id"]), initial)
        self.sender.assert_not_called()

    def test_match_on_another_page_cannot_supply_work(self):
        self.hits.clear()
        self.hits[56] = ROW
        reply = self.ask(CONFIRMED, self.begin())
        self.assertFalse(reply.get("context_id"), reply["text"])
        self.sender.assert_not_called()
