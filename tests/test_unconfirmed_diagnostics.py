"""Проверяет, что чат поясняет отказы по выбранной модели и признаку."""

import unittest

from ai_mechanic.ask import _diagnosis_applicability_message
from ai_mechanic.knowledge.search import retrieve


class Source:
    def __init__(self, rows=()):
        self.rows = rows

    def search(self, engine, query, *, variant, limit):
        return [dict(text=text, page=page, document_id=doc, engine_models=[engine]) for doc, page, text in self.rows]


def initial(engine, code, title, symptoms, rows=(), question=None):
    return retrieve(Source(rows), engine, question or title + ". Что уточнить у водителя и что проверить на двигателе?",
                    dict(anomaly_type=code, anomaly_title=title, description=symptoms, status="anomaly", signal_quality="good"))


class UnconfirmedDiagnosticsTests(unittest.TestCase):
    def test_generator_noise_needs_localization(self):
        result = initial("ЗМЗ-5234.10", "F05", "Генератор", "Стук и гул в зоне генератора на холостом ходу", [
            ("zmz-5234-10-rk-2025", 57, "Неисправен подшипник генератора | Заменить подшипник")])
        self.assertIn("генератор", result["clarification"].lower())
        self.assertIn("креплен", result["clarification"].lower())
        self.assertEqual([item["page"] for item in result["evidence"]], [57])
        self.assertFalse(any(e.get("work") for e in result["evidence"]))

    def test_generator_voltage_and_bearing_are_answered_separately(self):
        rows = [
            ("zmz-5234-10-rk-2025", 57, "Неисправен подшипник генератора | Заменить подшипник"),
            ("zmz-5234-10-rk-2025", 67, "Номинальное напряжение в системе 12 В."),
            ("zmz-5234-10-rk-2025", 76, "Проверка датчика температуры при напряжении питания 14 В."),
        ]
        nominal_table = ("zmz-5234-10-rk-2025", 9, "Электрооборудование | Однопроводное, номинальным напряжением 12 В")
        for available, pages in ((rows, {57, 67}), (rows[:1], {57}), (rows[1:], {67}),
                                 (rows[:1] + [nominal_table], {57, 9})):
            with self.subTest(pages=pages):
                result = initial("ЗМЗ-5234.10", "F05", "Генератор", "Стук и гул в зоне генератора", available,
                                 question="Как проверить генератор и по каким условиям измеряют напряжение?")
                self.assertEqual({e["page"] for e in result["evidence"]}, pages)
                text = (result["missing"] + " " + result["clarification"]).lower()
                self.assertIn("условия измерения напряжения", text)
                self.assertIn("генератор", text)
                self.assertIn("ослабление", text)
                if 57 not in pages:
                    self.assertIn("подшипник", result["missing"].lower())
                self.assertFalse(any(e["work"] for e in result["evidence"]))
                self.assertNotIn("14 В", " ".join(e["text"] for e in result["evidence"]))

    def test_clutch_noise_needs_installed_model(self):
        result = initial("ЯМЗ-236М2", "F06", "Сцепление", "Шум в зоне картера сцепления меняется при нажатии педали",
                         [("yamz-236m2-re-2010", 67, "Пробуксовка. Заменить ведомый диск.")])
        self.assertTrue(result["needs_clarification"])
        self.assertIn("сцеплен", result["clarification"])
        self.assertIn("модел", result["clarification"])
        self.assertFalse(result["evidence"])

    def test_unconfirmed_egr_recheck_recording(self):
        fault = {"anomaly_type":"F11","anomaly_title":"Рециркуляция отработавших газов"}
        vehicle = {"engine_model":"ЯМЗ-236М2"}
        msg = _diagnosis_applicability_message("Рециркуляция отработавших газов. Что проверить?",vehicle,fault)
        self.assertIn("повторите", msg.lower())
        self.assertIn("ЯМЗ-236М2",msg)
        self.assertFalse(_diagnosis_applicability_message("Какое моторное масло использовать?",vehicle,fault))

    def test_gear_train_explains_mismatch_and_localization(self):
        rows = [("yamz-236m2-rk-2014", 42, "Шестерни распределения и привода ТНВД должны быть установлены по меткам.")]
        for question in (None, "Есть ли на двигателе цепь ГРМ?", "Есть ли гидронатяжитель?"):
            with self.subTest(question=question):
                result = initial("ЯМЗ-236М2", "F14", "Цепь ГРМ, гидронатяжитель",
                                 "Равномерный шум в передней части", rows, question)
                evidence = " ".join(e["text"] for e in result["evidence"])
                self.assertIn("шестерённый привод", evidence)
                self.assertIn("Цепь ГРМ и её гидронатяжитель в штатной конструкции не предусмотрены.", evidence)
                self.assertIn("шум", result["clarification"])
                self.assertFalse(any(e.get("work") for e in result["evidence"]))
        absent = initial("ЯМЗ-236М2","F14","Цепь ГРМ, гидронатяжитель","Шум")
        self.assertFalse(absent["evidence"])
