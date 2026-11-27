"""Проверяет выбор строки карты дефектации для конкретной детали."""

import unittest

from ai_mechanic.knowledge.search import retrieve


def card(code, records, page):
    rows = [["Возможный дефект", "Способ установления дефекта и средства контроля", "Заключение"], *records]
    return {"document_id": "guide", "page": page, "section": "Карта дефектации шестерни",
            "condition": "Обозначение " + code, "text": "\n".join(" | ".join(r) for r in rows),
            "tables": [{"rows": rows}], "engine_models": ["Двигатель"]}


class PartCardTests(unittest.TestCase):
    def answer(self, question, hits):
        class Backend:
            def search(self, engine, query, *, variant=None, limit=12):
                return hits

        return retrieve(Backend(), "Двигатель", question)

    def test_picks_the_matching_part_and_row(self):
        wrong = card("777-1234500-АБ", [["Трещины", "Осмотр", "Заменить"]], 9)
        right = card("777-1234500-А", [["Износ зубьев", "Шаблон", "Обработать"],
                                       ["Трещины или обломы", "Осмотр", "Браковать"]], 40)
        result = self.answer("Шестерня 777-1234500-А, обнаружены трещины. Что предписывает карта дефектации?",
                             [wrong, right])
        self.assertEqual([e["page"] for e in result["evidence"]], [40])
        excerpt = result["evidence"][0]["excerpt"]
        self.assertIn("Трещины или обломы", excerpt)
        self.assertIn("Осмотр", excerpt)
        self.assertIn("Браковать", excerpt)
        self.assertNotIn("Обработать", excerpt)

    def test_ambiguous_crack_locations_do_not_get_a_decision(self):
        hit = card("777-1234500-А", [["Трещины на торце", "Осмотр", "Обработать"],
                                     ["Трещины на ободе", "Осмотр", "Браковать"]], 32)
        result = self.answer("По карте дефектации 777-1234500-А, что делать при трещинах?", [hit])
        self.assertEqual(result["evidence"], [])
        self.assertTrue(result["missing"])

    def test_does_not_apply_a_rejection_rule_to_an_absent_crack(self):
        hit = card("777-1234500-А", [["Трещины или обломы", "Осмотр", "Браковать"]], 32)
        for description in ("трещин нет", "трещины не обнаружены", "нет трещин"):
            with self.subTest(description=description):
                result = self.answer(f"По карте дефектации 777-1234500-А, {description}. Браковать?", [hit])
                self.assertEqual(result["evidence"], [])
                self.assertTrue(result["missing"])

    def test_requires_an_exact_row_code_when_the_table_lists_several_parts(self):
        hit = card("777-1234500-А, 777-1234500-Б", [], 28)
        hit["tables"][0]["rows"] = [["Обозначение", "Возможный дефект", "Заключение"],
                                    ["777-1234500-Б", "Трещины", "Браковать"]]
        result = self.answer("Какое заключение по трещине на детали 777-1234500-А?", [hit])
        self.assertEqual(result["evidence"], [])
        self.assertTrue(result["missing"])
