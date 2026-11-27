"""Проверяет границы разделов таблицы причин при низком давлении масла."""

import unittest

from ai_mechanic.knowledge.search import retrieve


class OilPressureTableSectionsTest(unittest.TestCase):
    header = ["Причина неисправности", "Метод устранения"]
    target = ["Низкое давление масла", None]
    noise = ["Стуки в двигателе", None]
    before = [
        ["Разрушение маслоотражательных колпачков", "Заменить маслоотражательные колпачки"],
        ["Унос масла из системы вентиляции из-за загрязнения воздушного фильтра", "Заменить фильтрующий элемент"],
    ]
    during = [
        ["Перегрев двигателя", "Заглушить двигатель"],
        ["Засорение или заедание редукционного клапана в открытом положении", "Отвернуть пробку"],
        ["Ослабление пружины редукционного клапана или ее поломка", "Заменить масляный насос в сборе"],
        ["Износ шестерен и перегородки масляного насоса", "Заменить шестерни и перегородку"],
        ["Чрезмерный износ подшипников коленчатого или распределительного вала", "Заменить вкладыши"],
    ]
    after = [["Сильный износ поршней, цилиндров, втулок шатунов, поршневых пальцев", "Произвести ремонт двигателя"]]

    def hit(self, rows, *, section="Низкое давление масла", preamble=""):
        return dict(document_id="zmz-5234-10-rk-2025", page=56, section=section,
                    engine_models=["ЗМЗ-5234.10"], applicability=["ЗМЗ-5234.10|all"],
                    text=preamble + "\n".join(" | ".join(x or "" for x in row) for row in rows),
                    tables=[{"rows": rows}])

    def cause_text(self, hits):
        class Search:
            def search(self, engine, query, *, variant, limit):
                return hits

        reply = retrieve(Search(), "ЗМЗ-5234.10", "Какие причины низкого давления масла?")
        return " ".join(e["text"] for e in reply["evidence"] if e["kind"] == "cause")

    def verify_rows(self, text):
        for cause, action in self.during:
            self.assertIn(cause, text)
            self.assertNotIn(action, text)
        for cause, action in self.before + self.after:
            self.assertNotIn(cause, text)
            self.assertNotIn(action, text)

    def test_page_title_does_not_override_three_separate_groups(self):
        hits = [self.hit([self.header, *self.before]),
                self.hit([self.header, self.noise, *self.after]),
                self.hit([self.header, self.target, *self.during])]
        self.verify_rows(self.cause_text(hits))

    def test_only_rows_between_internal_headings_in_whole_table(self):
        rows = [self.header, *self.before, self.target, *self.during, self.noise, *self.after]
        self.verify_rows(self.cause_text([self.hit(rows, section="Возможные неисправности двигателя")]))

    def test_headerless_continuation_cannot_be_assigned_by_page_title_alone(self):
        self.assertFalse(self.cause_text([self.hit([self.header, *self.before])]))

    def test_caption_in_excerpt_and_cause_column_define_single_topic(self):
        title = "Таблица 5. 8. Низкое или отсутствие давления масла"
        rows = [["Способ устранения", "Вероятная причина"], ["Проверить сетку", "Засорение сетки маслоприемника"]]
        text = self.cause_text([self.hit(rows, section="Таблица 5", preamble=title + "\n")])
        self.assertIn("Засорение сетки маслоприемника", text)
        self.assertNotIn("Проверить сетку", text)
