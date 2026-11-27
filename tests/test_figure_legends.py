"""Проверяет выбор позиций из подписи к конкретному рисунку руководства."""

import unittest

from ai_mechanic.knowledge.search import retrieve


class FigureIndex:
    def __init__(self, rows):
        self.rows = rows

    def search(self, engine, query, *, variant, limit):
        return [dict(document_id="guide", section="Общий вид", page=page, text=text,
                     engine_models=[engine], kind="ocr", applicability=[engine + "|all"])
                for page, text in self.rows]


class FigureLegendTests(unittest.TestCase):
    def answer(self, rows, query):
        return retrieve(FigureIndex(rows), "ЗМЗ-40904.10", query)

    def test_reports_only_requested_positions_with_source_page(self):
        rows = [
            (13, "Рис.9. Вид спереди двигателя:\n4 – шкив генератора; 14 – датчик температуры."),
            (6, "Рис.2. Вид спереди двигателя ЗМЗ-40904.10:\n1 – шкив; 4 – генератор; "
                "13 – кронштейн; 14 – ремень\nпривода генератора и водяного насоса; 15 – отверстие."),
        ]
        result = self.answer(rows, "Что обозначают позиции 4 и 14 на виде спереди двигателя, рисунок 2?")
        self.assertEqual([(e["page"], e["text"]) for e in result["evidence"]],
                         [(6, "4 — генератор."), (6, "14 — ремень привода генератора и водяного насоса.")])

    def test_finds_lettered_figure_without_taking_labels_from_the_next_figure(self):
        rows = [
            (119, "Рисунок 24 – Сервисный модуль спереди\n4 — канал подвода.\n"
                  "Рисунок 24а – Сервисный модуль сзади\n3 — канал подвода масла из блока в ЖМТ.\n"
                  "4 — канал отвода масла из масляного фильтра в блок.\n5 — канал отвода масла в картер."),
        ]
        result = self.answer(rows, "На виде сзади сервисного модуля куда отводится масло по каналу 4 на рисунке 24а?")
        self.assertEqual([(e["page"], e["text"]) for e in result["evidence"]],
                         [(119, "4 — канал отвода масла из масляного фильтра в блок.")])

    def test_colliding_figure_numbers_require_clarification(self):
        rows = [(2, "Рисунок 1 — Генератор:\n1 – ротор; 2 – крышка."),
                (12, "Рисунок 1 — Стартер:\n1 – шестерня; 2 – обмотка.")]
        result = self.answer(rows, "Что обозначает позиция 1 на рисунке 1?")
        self.assertEqual(result["evidence"], [])
        self.assertTrue(result["missing"])

    def test_keeps_known_label_and_names_the_absent_one(self):
        result = self.answer([(1, "Рисунок 5 — Схема:\n2 и 3 — каналы подачи; 4 — штуцер.")],
                             "Что означают позиции 2 и 12 на рисунке 5?")
        self.assertEqual([(e["page"], e["text"]) for e in result["evidence"]], [(1, "2 — каналы подачи.")])
        self.assertIn("12", result["missing"])

    def test_shared_callout_keeps_all_requested_numbers_and_the_source_marking(self):
        result = self.answer([(8, "Рис.3. Схема:\n2, 3, 8 – каналы подачи; 4 – штуцер.")],
                             "Что означают позиции 2 и 3 на рисунке 3?")
        self.assertEqual([(e["text"], e["excerpt"]) for e in result["evidence"]],
                         [("2, 3 — каналы подачи.", "2, 3, 8 – каналы подачи")])
