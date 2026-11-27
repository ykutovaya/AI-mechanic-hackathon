"""Проверяет, к какому режиму относится совет из таблицы неисправностей сцепления."""

import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ai_mechanic.ask import ManualSession
from ai_mechanic.knowledge.search import retrieve


class ClutchNoiseTests(unittest.TestCase):
    ENGINE = "ЗМЗ-5234.10"
    IDLE = "Металлическое дребезжание в зоне маховика на холостом ходу"

    def diagnose(self, symptoms, *, table=True, question="Что проверить в первую очередь?", topic="",
                 cause="Поломка или износ деталей демпферного устройства ведомого диска"):
        rows = [
            ["Вероятная причина", "Метод устранения"],
            ["Вибрация, шумы и металлическое дребезжание в трансмиссии при движении", None],
            [cause, "Заменить ведомый диск"],
        ]
        hits = [dict(document_id="zmz-5234-10-rk-2025", page=80, engine_models=[self.ENGINE],
                     fault_code="F06", expert_rows=[403], kind="table", tables=[dict(rows=rows)],
                     text="\n".join(" | ".join(cell for cell in row if cell) for row in rows))] if table else []

        class Search:
            def search(self, engine, query, *, limit, variant):
                return hits

            def search_fault(self, engine, query, *, code, limit, variant):
                return hits if code == "F06" else []

        diagnosis = dict(status="anomaly", anomaly_type="F06", anomaly_title="Сцепление",
                         description=symptoms, signal_quality="warning")
        return retrieve(Search(), self.ENGINE, question, diagnosis=diagnosis, topic=topic)

    def test_followup_about_checking_clutch_keeps_idle_noise_context(self):
        result = self.diagnose(self.IDLE, question="Как проверить сцепление при признаках этой неисправности?")
        self.assertTrue(result["needs_clarification"])
        self.assertEqual([item["page"] for item in result["evidence"]], [80])

    def test_idle_noise_does_not_inherit_repair_for_noise_while_driving(self):
        result = self.diagnose(self.IDLE)
        evidence = result["evidence"]
        self.assertNotIn("заменить", " ".join(item["text"].lower() for item in evidence))
        self.assertEqual([item["page"] for item in evidence], [80])
        self.assertIn("при движении", evidence[0]["text"])
        self.assertFalse(any(item["work"] for item in evidence))
        self.assertTrue(result["needs_clarification"])
        self.assertRegex(result["clarification"], "нажатии педали сцепления")
        self.assertIn("под нагрузкой", result["clarification"])

    def test_missing_table_still_asks_about_noise_without_fabricating_reference(self):
        result = self.diagnose(self.IDLE, table=False)
        self.assertFalse(result["evidence"])
        self.assertTrue(result["needs_clarification"])

    def test_chat_requests_observations_before_recommending_an_inspection_order(self):
        vehicle, check = "C063EM78", "S-0128"
        card = dict(vehicle_id=vehicle, model="Машина", engine_model=self.ENGINE, sessions=[dict(session_id=check)])
        diagnosis = dict(vehicle_id=vehicle, session_id=check, status="anomaly", anomaly_type="F06",
                         anomaly_title="Сцепление", description=self.IDLE, signal_quality="warning")
        result = self.diagnose(self.IDLE, table=False)
        with TemporaryDirectory() as root, patch("ai_mechanic.ask._records",
                                                 side_effect=[[card], [dict(engine_models=[self.ENGINE])]]):
            session = ManualSession(root=root, search=object(), retriever=lambda *_args, **_kwargs: result)
            reply = session.ask(vehicle, "Что проверить?", check_id=check, diagnosis=diagnosis)
        self.assertIn("нажатии педали сцепления", reply["text"])
        self.assertNotIn("Рекомендована дефектовка", reply["text"])
        self.assertFalse(reply["inspection_steps"])

    def respond(self, question, topic="clutch-noise", **kwargs):
        return self.diagnose(self.IDLE, question=question, topic=topic, **kwargs)

    def test_idle_only_after_pedal_clarification_explains_limit_without_repeating_questions(self):
        result = self.respond("При нажатии педали сцепления шум исчезает. Слышен только на холостом ходу, "
                              "в движении не слышен.")
        self.assertFalse(result["needs_clarification"])
        self.assertNotIn("?", result["missing"] + result["clarification"])
        self.assertIn("холостом ходу", result["missing"])
        self.assertFalse(any(i["work"] for i in result["evidence"]))

    def test_partial_answer_is_kept_until_driver_reports_if_noise_persists_in_motion(self):
        pedal = self.respond("Когда нажимаю педаль, шум пропадает.")
        self.assertIn("движении", pedal["clarification"])
        self.assertNotIn("педал", pedal["clarification"])
        self.assertTrue(pedal["needs_clarification"])
        mixed = self.respond("При нажатии педали шум исчезает, а при движении пока не проверял.")
        self.assertEqual(mixed["topic"], pedal["topic"])
        for question in ("Шум есть и в движении под нагрузкой. Что делать дальше?",
                         "Да, металлическое дребезжание слышно и при движении под нагрузкой.", "Да"):
            with self.subTest(question=question):
                confirmed = self.respond(question, pedal["topic"])
                self.assertFalse(confirmed["needs_clarification"])
                self.assertEqual([i["page"] for i in confirmed["evidence"]], [80])
                item = confirmed["evidence"][0]
                self.assertIn("Поломка или износ деталей демпферного устройства", item["excerpt"])
                self.assertIn("при движении", item["excerpt"])
                self.assertEqual(item["work"], "Проверить детали демпферного устройства ведомого диска на поломку и износ.")
                self.assertNotIn("заменить", item["text"].lower())
                again = self.respond(item["work"], confirmed["topic"])
                self.assertEqual([i["work"] for i in again["evidence"]], [item["work"]])

    def test_unsigned_uncertain_and_other_sounds_do_not_confirm_damper_damage(self):
        questions = ("Нет. В движении дребезга нет.", "В движении только свист, а дребезжание — на холостом.",
                     "В движении ещё не проверял.", "Будет ли шум в движении?", "Да", "Какое масло залить?")
        for q in questions:
            with self.subTest(question=q):
                result = self.respond(q)
                self.assertFalse(any(i["work"] for i in result["evidence"]))
                self.assertNotIn("заменить", " ".join(i["text"] for i in result["evidence"]).lower())
        unrelated = self.respond("Какое масло залить?")
        self.assertFalse(unrelated["topic"].startswith("clutch-noise"))

    def test_motion_without_the_correct_row_cannot_open_an_order(self):
        result = self.respond("Такое же дребезжание слышно и при движении.",
                              cause="Повреждены фрикционные накладки")
        self.assertFalse(any(i["work"] for i in result["evidence"]))
        self.assertTrue(result["missing"])

    def test_driver_can_correct_previous_statement_about_motion(self):
        first = self.respond("На ходу тоже слышно такое же дребезжание.")
        self.assertTrue(any(i["work"] for i in first["evidence"]))
        result = self.respond("В движении дребезга всё-таки нет.", first["topic"])
        self.assertFalse(any(i["work"] for i in result["evidence"]))

    def test_other_component_and_uncertain_motion_are_not_a_driver_confirmation(self):
        for q in ("Дребезжит только на месте, на ходу не дребезжит.", "Не знаю, в движении шум вообще есть или нет.",
                  "При движении дребезга больше не было.", "В движении есть запах гари.",
                  "В движении слышен только писк.", "Насос ГУР шумит в движении. Как его проверить?",
                  "Почему шумит масляный насос?"):
            with self.subTest(question=q):
                result = self.respond(q)
                self.assertFalse(any(i["work"] for i in result["evidence"]))
                self.assertNotIn("заменить", " ".join(i["text"] for i in result["evidence"]).lower())
        self.assertFalse(self.respond("Почему шумит масляный насос?")["topic"].startswith("clutch-noise"))

    def test_loss_of_observation_revokes_previously_confirmed_motion(self):
        first = self.respond("При движении дребезг не исчезает.")
        self.assertTrue(any(i["work"] for i in first["evidence"]))
        for q in ("При движении шум отсутствует", "На ходу дребезг пропадает", "На ходу шума не заметил",
                  "Не ездил, не знаю", "Не могу сказать, есть ли при движении"):
            with self.subTest(question=q):
                result = self.respond(q, first["topic"])
                self.assertFalse(any(i["work"] for i in result["evidence"]))
                self.assertFalse(result["topic"].endswith(":moving"))

    def test_motion_condition_is_distinct_from_idle_noise(self):
        result = self.diagnose("Вибрация и металлическое дребезжание в трансмиссии при движении")
        self.assertFalse(result["needs_clarification"])
        self.assertIn("при движении", result["evidence"][0]["text"])
        self.assertNotIn("заменить", result["evidence"][0]["text"].lower())
