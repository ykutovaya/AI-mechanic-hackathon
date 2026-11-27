"""Проверяет первичный осмотр сцепления до разборки ЗМЗ-40905.10."""

import unittest

from ai_mechanic.knowledge.search import retrieve


class ClutchInspectionTest(unittest.TestCase):
    engine = "ЗМЗ-40905.10"
    title = "Сцепление"
    first = "Сцепление. Что уточнить у водителя и что проверить на двигателе?"
    symptom = "Дребезжание в зоне картера сцепления, пропадает при выжатой педали"
    mount = ("Уход за сцеплением заключается в периодической проверке крепления коробки передач "
             "к блоку цилиндров двигателя и степени изношенности фрикционных накладок.")
    drive = ("Перед проведением проверки деталей сцепления проверить работу и отрегулировать привод "
             "выключения сцепления. При необходимости прокачать гидропривод сцепления, ослабленные крепления подтянуть.")
    possible = "При их непригодности заменяются новыми."

    def result(self, pages, question=None):
        engine = self.engine
        hits = [dict(document_id="zmz-40904-40905-rk-2025", edition="2025", source_sha256="a" * 64,
                     title="Руководство ЗМЗ", applicability=[engine + "|all"], engine_models=[engine],
                     text=text, page=page) for page, text in pages]

        class Search:
            def search(self, engine, query, *, variant, limit):
                return hits

        d = dict(anomaly_type="F06", anomaly_title=self.title, status="anomaly", signal_quality="good",
                 description=self.symptom)
        return retrieve(Search(), engine, question or self.first, diagnosis=d)

    def test_first_actions_have_their_own_physical_page(self):
        result = self.result([(194, self.mount), (195, self.drive + " " + self.possible)])
        works = {e["page"]: e["work"] for e in result["evidence"] if e.get("work")}
        self.assertEqual({194, 195}, set(works))
        self.assertIn("крепление коробки передач к блоку цилиндров", works[194].lower())
        self.assertIn("работу привода выключения сцепления", works[195].lower())
        self.assertNotRegex(" ".join(works.values()).lower(), r"замен|снять|прокач|отрегулир")
        self.assertIn("дребезжание", result["clarification"].lower())

    def test_general_followup_keeps_the_two_independent_clutch_checks(self):
        for question in ("Как проверить сцепление при признаках этой неисправности?",
                         "Какие проверки сцепления нужны по результату диагностики?",
                         "С чего начать проверку сцепления в моём случае?"):
            with self.subTest(question=question):
                result = self.result([(194, self.mount), (195, self.drive)], question)
                self.assertEqual({194, 195}, {e["page"] for e in result["evidence"] if e.get("work")})
                self.assertFalse(result["missing"])
        missing_drive = self.result([(194, self.mount)], "Как проверить сцепление при признаках этой неисправности?")
        self.assertIn("привода", missing_drive["missing"])
        self.assertEqual([194], [e["page"] for e in missing_drive["evidence"]])

    def test_cannot_reconstruct_missing_clutch_work(self):
        result = self.result([(195, self.possible)])
        self.assertFalse([e for e in result["evidence"] if e.get("work")])
        self.assertIn("не подтвержд", result["missing"].lower())

    def test_unrelated_question_does_not_repeat_clutch_work(self):
        result = self.result([(194, self.mount), (195, self.drive)], "Как проверить уровень масла?")
        self.assertFalse([e for e in result["evidence"] if e.get("work") and "сцеплен" in e["work"].lower()])
