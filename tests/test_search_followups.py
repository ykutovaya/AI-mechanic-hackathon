"""Проверяет дополнительные запросы по действиям и причинам из руководства."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from ai_mechanic.knowledge._qdrant_store import QdrantSearch


class RetrievalQueriesTest(unittest.TestCase):
    def search(self, question, hits, lookup=None):
        reader = QdrantSearch(client=Mock(), encoder=SimpleNamespace(signature="test"))
        reader._physical_collection = Mock(return_value="accepted-collection")
        def ranked(physical, encoder, query, allowed, limit):
            texts = [hits] if query == question else (lookup or {}).get(query, [])
            return [dict(id=text, text=text) for text in texts]
        reader._ranked_chunks = Mock(side_effect=ranked)
        response = reader.search_chunks("двигатель", question, variant="А")
        requests = reader._ranked_chunks.call_args_list
        for call in requests:
            self.assertEqual("accepted-collection", call.args[0])
            self.assertEqual({"двигатель|all", "двигатель|А"}, set(call.args[3].must[0].match.any))
        return response, [call.args[2] for call in requests]

    def test_disassembly_preserves_named_component_in_the_manual_query(self):
        for name in ["масляный насос", "водяной насос", "корпус топливного фильтра"]:
            with self.subTest(name=name):
                q = f"Как разобрать {name}?"
                hits, queries = self.search(q, "Общее описание.", {"Разборка " + name: ["Порядок работ"]})
                self.assertIn("Разборка " + name, queries)
                self.assertIn("Порядок работ", [h["text"] for h in hits])
        _, queries = self.search("Можно ли не разбирать топливный насос?", "Снять крышку.")
        self.assertEqual(1, len(queries))

    def test_oil_wear_queries_depend_on_applicable_manual_cause(self):
        q = "Давление масла упало. Что проверить?"
        cause = "Износ шестерен и перегородки масляного насоса"
        _, queries = self.search(q, "Описание.", {"низкое давление масла возможные причины неисправности": [cause]})
        evidence = [query for query in queries if "износ" in query.casefold()]
        self.assertTrue(evidence)
        _, unrelated = self.search(q, "Снять масляный насос.", {"низкое давление масла возможные причины неисправности":
                                   ["Износ подшипника генератора. Заменить прокладку масляного насоса."]})
        self.assertFalse(any("износ" in query.casefold() for query in unrelated))

