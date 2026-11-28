"""Проверки разбора реплики YandexGPT с подменой HTTP-транспорта."""

import json
import unittest
from unittest.mock import patch
from urllib.error import URLError

from ai_mechanic.yandex_gpt import YandexGPT


class AnalyzeTurnTests(unittest.TestCase):
    def setUp(self):
        self.client = YandexGPT("test-folder", "Bearer test-token")
        self.open = self.enterContext(patch("ai_mechanic.yandex_gpt.urlopen"))

    def provide(self, content, *, finish_reason="stop"):
        response = {
            "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
            "usage": {"prompt_tokens": 45, "completion_tokens": 13, "total_tokens": 58},
        }
        self.open.return_value.__enter__.return_value.read.return_value = json.dumps(response)

    def test_extracts_search_query_from_a_followup(self):
        self.provide(json.dumps({"query": "Проверка датчика давления масла"}))

        parsed, usage = self.client.analyze_turn(
            "А как его проверить?", "Где датчик давления масла?", "Укажите исполнение двигателя.",
        )

        self.assertEqual(parsed, {"query": "Проверка датчика давления масла"})
        self.assertEqual(usage, {"prompt_tokens": 45, "completion_tokens": 13, "total_tokens": 58})
        request = self.open.call_args.args[0]
        self.assertEqual(self.open.call_args.kwargs["timeout"], 6)
        self.assertEqual(request.headers["X-data-logging-enabled"], "false")
        body = json.loads(request.data)
        self.assertEqual(body["model"], "gpt://test-folder/yandexgpt-5.1")
        self.assertEqual(body["response_format"]["json_schema"]["schema"]["required"], ["query"])
        self.assertEqual(json.loads(body["messages"][-1]["content"]), {
            "question": "А как его проверить?",
            "previous_question": "Где датчик давления масла?",
            "previous_clarification": "Укажите исполнение двигателя.",
        })

    def test_returns_only_known_knock_fields_with_verbatim_evidence(self):
        self.provide(json.dumps({
            "query": "",
            "knock": [
                {"field": "where", "value": "bottom", "quote": "снизу блока"},
                {"field": "load", "value": "above", "quote": "снизу блока"},
                {"field": "rpm", "value": "idle", "quote": "на холостых"},
                {"field": "lamp", "value": "on", "quote": "лампа горела"},
            ],
        }))
        parsed, _ = self.client.analyze_turn(
            "Стук снизу блока, лампа горела.",
            previous_clarification="Откуда шёл стук?", oil_knock=True,
        )
        self.assertEqual(parsed, {"query": "", "knock": [
            {"field": "where", "value": "bottom", "quote": "снизу блока"},
        ]})
        self.assertIn("knock", json.loads(self.open.call_args.args[0].data)
                      ["response_format"]["json_schema"]["schema"]["required"])

    def test_ignores_conflicting_or_empty_evidence(self):
        self.provide(json.dumps({
            "query": "Проверка стука",
            "knock": [
                {"field": "where", "value": "top", "quote": "сверху"},
                {"field": "where", "value": "bottom", "quote": "снизу"},
                {"field": "load", "value": "loaded", "quote": " "},
                {"field": "rpm", "value": "idle", "quote": ""},
            ],
        }))
        parsed, _ = self.client.analyze_turn("Сверху? Возможно, сверху или снизу.", oil_knock=True)
        self.assertEqual(parsed, {"query": "Проверка стука", "knock": []})

    def test_invalid_model_response_or_unavailable_service_is_ignored(self):
        for content, reason in (
            ("not JSON", "stop"), ('{"query": ""}', "length"),
            ('{"query": 10}', "stop"), ('[""]', "stop"),
        ):
            with self.subTest(content=content, reason=reason):
                self.provide(content, finish_reason=reason)
                self.assertIsNone(self.client.analyze_turn("А где?", "О датчике"))
        self.open.side_effect = URLError("таймаут")
        self.assertIsNone(self.client.analyze_turn("А где?", "О датчике"))
