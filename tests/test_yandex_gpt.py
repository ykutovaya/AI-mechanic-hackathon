"""Проверки справочного ответа с YandexGPT без внешних запросов."""

import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request, urlopen

from ai_mechanic import web
from test_web_flow import Session, running


FIRST = "По руководству. Давление начала впрыскивания указано в таблице. [1]"
SECOND = "По руководству. Проверьте форсунку на стенде. [2]"


class ReferenceSession(Session):
    def ask(self, machine_id, question, *, check_id=None, diagnosis=None):
        self.topic = "injector-reference"
        return {
            "engine_model": "ЯМЗ-236М2",
            "text": f"{FIRST}\n\n{SECOND}",
            "synthesis": "extractive",
            "sources": [{"document_id": "guide", "page": page} for page in (2, 3)],
            "warnings": [],
            "inspection_steps": [],
        }


class ReferenceAnswerTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(TemporaryDirectory())
        self.model = Mock()
        self.model.analyze_turn.return_value = None
        self.enterContext(patch.object(web, "ManualSession", ReferenceSession))
        self.url = self.enterContext(running(web.make_server(
            ("127.0.0.1", 0), root=root, search=object(), answer_model=self.model,
        )))

    def ask(self):
        body = {"machine_id": "vehicle", "question": "Как проверить форсунку?"}
        request = Request(self.url + "/api/ask", json.dumps(body).encode(),
                          {"Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def test_reorders_excerpts_and_preserves_pdf_references(self):
        usage = {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40}
        self.model.order_excerpts.return_value = ([2, 1], usage)
        reply = self.ask()
        self.model.order_excerpts.assert_called_once_with("Как проверить форсунку?", [FIRST, SECOND])
        self.assertEqual(reply["text"], f"{SECOND}\n\n{FIRST}")
        self.assertEqual(reply["synthesis"], "yandexgpt")
        self.assertEqual(reply["generation_usage"], usage)
        self.assertEqual([item["url"] for item in reply["sources"]],
                         ["/manuals/guide.pdf#page=2", "/manuals/guide.pdf#page=3"])
        self.assertIsNone(reply["diagnosis"])
        self.assertNotIn("context_id", reply)

    def test_preserves_excerpts_when_model_omits_or_duplicates_them(self):
        for order in (None, ([2], {}), ([2, 2], {}), ([True, 2], {})):
            with self.subTest(order=order):
                self.model.order_excerpts.return_value = order
                reply = self.ask()
                self.assertEqual(reply["text"], f"{FIRST}\n\n{SECOND}")
                self.assertEqual(reply["synthesis"], "extractive")
