
"""Веб-чат использует разбор реплики без передачи идентификаторов модели."""

import json
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.request import Request, urlopen

from ai_mechanic import web
from ai_mechanic.knowledge.search import retrieve
from test_web_flow import Session, running


class DialogueSession(Session):
    def ask(self, machine_id, question, *, check_id=None, diagnosis=None):
        self.topic = "oil"
        self.clarification = "Какой фильтр установлен?"
        hint = getattr(self, "search_hint", "")
        return {
            "text": f"Реплика: {question}. Дополнительный запрос: {hint}",
            "sources": [], "warnings": [],
        }


class YandexDialogueTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(TemporaryDirectory())
        self.model = Mock()
        self.model.analyze_turn.return_value = ({"query": "замена масляного фильтра"}, {"total_tokens": 38})
        self.enterContext(patch.object(web, "ManualSession", DialogueSession))
        self.url = self.enterContext(running(web.make_server(
            ("127.0.0.1", 0), root=root, search=object(), answer_model=self.model,
        )))

    def ask(self, question, **fields):
        data = json.dumps({"machine_id": "vehicle", "question": question} | fields).encode()
        req = Request(self.url + "/api/ask", data, {"Content-Type": "application/json"})
        with urlopen(req, timeout=5) as response:
            return json.load(response)

    def test_uses_model_for_any_topic_and_remembers_last_question(self):
        first = self.ask("Где фильтр?")
        self.assertIn("Дополнительный запрос: замена масляного фильтра", first["text"])
        self.assertEqual(first["analysis_usage"], {"total_tokens": 38})
        self.model.analyze_turn.assert_called_once_with("Где фильтр?", "", "", oil_knock=False)

        next_reply = self.ask("А как поменять?", dialogue_id=first["dialogue_id"], topic=first["topic"])
        self.assertNotEqual(first["dialogue_id"], next_reply["dialogue_id"])
        self.model.analyze_turn.assert_called_with(
            "А как поменять?", "Где фильтр?", "Какой фильтр установлен?", oil_knock=False,
        )

    def test_discards_dialogue_for_another_check_and_keeps_answer_on_model_failure(self):
        first = self.ask("Где фильтр?")
        self.assertIn("dialogue_id", first)
        self.model.analyze_turn.return_value = None
        reply = self.ask("А как поменять?", check_id="good", dialogue_id=first["dialogue_id"])
        self.model.analyze_turn.assert_called_with("А как поменять?", "", "", oil_knock=False)
        self.assertIn("А как поменять?", reply["text"])
        self.assertNotIn("analysis_usage", reply)


class ModelSearchHintTests(unittest.TestCase):
    def test_adds_verified_evidence_when_the_original_search_has_no_answer(self):
        engine = "ЯМЗ-536"
        hit = {
            "document_id": "guide", "page": 12, "title": "Руководство",
            "text": "Вязкостная муфта вентилятора обеспечивает изменение частоты вращения."
        }
        searches = []

        class Backend:
            def search(self, requested_engine, query, *, variant, limit):
                searches.append((requested_engine, variant, query))
                return [hit] if "муфта вентилятора" in query else []

        question = "А как она устроена?"
        self.assertFalse(retrieve(Backend(), engine, question, variant="X")["evidence"])
        result = retrieve(Backend(), engine, question, variant="X", search_hint="муфта вентилятора")
        self.assertEqual([(row["page"], row["text"]) for row in result["evidence"]], [(12, hit["text"])])
        self.assertTrue(all(pair[:2] == (engine, "X") for pair in searches))
        self.assertTrue(all(not row["work"] for row in result["evidence"]))
