"""Проверяет жизненный цикл черновика после уточнения шума сцепления водителем."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ai_mechanic import web
from test_web_flow import running


WORK = "Проверить детали демпферного устройства ведомого диска на поломку и износ."
MOVES = "Дребезжание слышно и при движении под нагрузкой."
STILL = "При движении дребезжания нет."
PEDAL = "При нажатии педали сцепления шум исчезает."


class ClutchSession:
    """Воспроизводит ответы поиска по явно сообщённому режиму и ранее сохранённым наблюдениям."""

    def __init__(self, **kwargs):
        self.selection = None
        self.topic = ""
        self.registry = []
        self.machines = [dict(machine_id="V", vehicle_id="V", label="Машина", engine_model="ЗМЗ-5234.10",
                              history=[dict(check_id=c) for c in ("A", "B")])]

    def ask(self, machine_id, question, *, check_id, diagnosis):
        reply = dict(text="Нужно уточнить режим шума.", sources=[], warnings=[], inspection_steps=[])
        if not diagnosis:
            self.topic = ""
            return reply
        if question.startswith("Сцепление."):
            self.topic = "clutch-noise"
        elif question == "Какое масло залить?":
            self.topic = "oil-grade"
        elif self.topic.startswith("clutch-noise"):
            pedal = "gone" if ":gone:" in self.topic else "unknown"
            if question == PEDAL:
                self.topic = "clutch-noise:gone:unknown"
            if question == MOVES:
                self.topic = f"clutch-noise:{pedal}:moving"
            if question == STILL:
                self.topic = f"clutch-noise:{pedal}:stationary"
            if self.topic.endswith(":moving") and (question == MOVES or question.rstrip(".") == WORK.rstrip(".")):
                reply.update(text=WORK, sources=[dict(document_id="guide", page=80, title="Руководство")],
                             inspection_steps=[dict(document_id="guide", page=80, role="work", text=WORK)])
        return reply


class FollowupOrderTests(unittest.TestCase):
    case = dict(machine_id="V", check_id="A")

    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(web, "ManualSession", ClutchSession))

        def diagnosis(url, machine, check):
            return dict(vehicle_id=machine, session_id=check, status="anomaly", signal_quality="good",
                        anomaly_type="F06", anomaly_title="Сцепление", description="Шум на холостом ходу.")

        self.enterContext(patch.object(web, "fetch_diagnosis", side_effect=diagnosis))
        self.sender = Mock(return_value="TKT-test")
        self.enterContext(patch.object(web, "submit_order", self.sender))
        self.url = self.enterContext(running(web.make_server(("127.0.0.1", 0), root=root, search=object(),
                                                            orders_db="orders.sqlite")))

    def post(self, endpoint, payload):
        request = Request(self.url + "/api/" + endpoint, data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=6) as response:
            return json.load(response)

    def ask(self, question, **fields):
        return self.post("ask", self.case | dict(question=question) | fields)

    def test_followup_opens_qualified_order_and_retraction_invalidates_it(self):
        start = self.post("diagnose", self.case)
        self.assertFalse(start["context_id"])
        pedal = self.ask(PEDAL, topic=start["topic"])
        reply = self.ask(MOVES, topic=pedal["topic"])
        cid = reply.get("context_id")
        self.assertTrue(cid)
        context = dict(context_id=cid)
        draft = self.post("orders/draft", context)
        self.assertEqual(draft["payload"]["works"], [WORK])
        version = context | {k: draft[k] for k in ("draft_id", "revision")}
        edit = self.post("orders/update", version | {"changes":{"works":[WORK.rstrip(".")]}})
        self.assertEqual(edit["payload"]["sources"][0]["page"], 80)
        version["revision"] = edit["revision"]
        unrelated = self.ask("Какое масло залить?", context_id=cid)
        self.assertEqual(self.post("orders/draft", context)["revision"], edit["revision"])
        with self.assertRaises(HTTPError):
            self.post("orders/send", version | {"confirmed": False})
        negative = self.ask(STILL, topic=unrelated["topic"], context_id=cid)
        self.assertEqual(json.loads(negative["topic"])[2], "clutch-noise:gone:stationary")
        self.assertEqual(negative.get("context_id"), "")
        with self.assertRaises(HTTPError):
            self.post("orders/send", version | {"confirmed": True})
        self.sender.assert_not_called()
        recovered = self.ask(MOVES)
        self.assertTrue(recovered.get("context_id"))
        self.assertNotEqual(recovered["context_id"], cid)

    def test_sent_order_stays_in_history_after_retraction_but_cannot_be_copied(self):
        self.post("diagnose", self.case)
        cid = self.ask(MOVES)["context_id"]
        draft = self.post("orders/draft", {"context_id": cid})
        fields = {key: draft[key] for key in ("draft_id", "revision")}
        sent = self.post("orders/send", fields | dict(context_id=cid, confirmed=True))
        self.assertEqual(sent["number"], "TKT-test")
        self.ask(STILL, context_id=cid)
        with self.assertRaises(HTTPError):
            self.post("orders/new", dict(context_id=cid, revision=sent["revision"]))
        history = self.post("orders/history", dict(machine_id="V"))["orders"]
        self.assertEqual([entry["number"] for entry in history], ["TKT-test"])
        self.assertEqual(self.sender.call_count, 1)

    def test_forged_topic_or_other_check_cannot_unlock_an_order(self):
        start = self.post("diagnose", self.case)
        forged = json.dumps(["V", "A", "clutch-noise:unknown:moving"])
        unconfirmed = self.ask(WORK, topic=forged)
        self.assertEqual(unconfirmed["sources"], [])
        self.assertFalse(unconfirmed.get("context_id"))
        self.assertFalse(self.ask(MOVES, topic=start["topic"], check_id="B").get("context_id"))
        positive = self.ask(MOVES)
        self.assertTrue(positive.get("context_id"))
        before = dict(context_id=positive["context_id"])
        self.ask("Какое масло залить?", check_id="B", **before)
        self.assertEqual(self.post("orders/draft", before)["payload"]["works"], [WORK])
        self.post("diagnose", self.case)
        self.assertFalse(self.ask(WORK, topic=positive["topic"]).get("context_id"))
        with self.assertRaises(HTTPError):
            self.post("orders/draft", before)
        self.sender.assert_not_called()
