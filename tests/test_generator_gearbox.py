"""Проверки диалога о генераторе и коробке передач на примерах из выданных сессий."""

import unittest

from ai_mechanic.knowledge.search import retrieve


YAMZ_236 = "ЯМЗ-236М2"
YAMZ_536 = "ЯМЗ-536"
ZMZ = "ЗМЗ-40904.10"
CASES = {
    "S-0028": (YAMZ_236, "F05", "Гул и шорох подшипника в зоне генератора, амперметр показывает разряд"),
    "S-0181": (ZMZ, "F05", "Гул подшипника в зоне генератора, сигнализатор разряда батареи мигает"),
    "S-0082": (YAMZ_536, "F05", "Шум в зоне генератора, пропадает после снятия ремня привода агрегата"),
    "S-0039": (YAMZ_236, "F07", "Повышенный шум за картером маховика на нейтральной передаче, пропадает при выжатом сцеплении"),
}
MANUALS = {YAMZ_236: "yamz-236m2-re-2010", YAMZ_536: "yamz-536-re-2016", ZMZ: "zmz-40904-40905-rk-2025"}
EXCITATION = """Колебание наг рузки генератора
Недостаточен  контакт  в  цепи  Проверить  целостность  цепи  воз-
возбуждения                   буждения и надежность соединения
                                 в местах контактов, при необходи-
                              мости зачистить и затянуть контакты"""
FASTENINGS = ("Периодически необходимо очищать генератор от грязи, проверять надеж-\n"
              "ность его крепления к двигателю и надежность соединений проводов с выводами\nгенератора.")
NOISE = """Повышенный шум при работе коробки передач
Износ подшипников валов коробки передач    Заменить неисправные подшипники
Перекос осей первичного и вторичного валов из-за ослабления затяжки болтов    Проверить затяжку и при необходимости подтянуть болты
крепления картера сцепления к картеру маховика и болтов крепления задней    крепления картера сцепления к картеру маховика и болты
опоры коробки передач    крепления задней опоры коробки передач"""
FAMILIES = "В зависимости от назначения коробки передач выпускаются в модификациях: ЯМЗ-236П, ЯМЗ-236ПО, ЯМЗ-236Н, ЯМЗ-236Л, ЯМЗ-236У, ЯМЗ-236УО."


def fragment(engine, page, text, rows=()):
    return dict(document_id=MANUALS[engine], engine_models=[engine], page=page, text=text,
                tables=[dict(rows=rows)] if rows else [])


def table(engine, page, rows):
    return fragment(engine, page, "\n".join(" | ".join(row) for row in rows), rows)


def response(check, hits, question="Что проверить в первую очередь?", topic=""):
    engine, code, symptoms = CASES[check]

    class Search:
        def search(self, engine, query, *, limit, variant):
            return hits

        def search_fault(self, engine, query, *, code, limit, variant):
            return hits

    return retrieve(Search(), engine, question, topic=topic, diagnosis=dict(
        status="anomaly", signal_quality="warning", anomaly_type=code,
        anomaly_title="Генератор" if code == "F05" else "Коробка передач", description=symptoms))


class GeneratorGearboxTests(unittest.TestCase):
    def works(self, reply):
        return {item["page"]: item["work"] for item in reply["evidence"] if item["work"]}

    def test_discharge_points_to_excitation_circuit_and_contacts(self):
        hits = [fragment(YAMZ_236, 56, "Обрыв фаз, замыкание на массу  Заменить статор"),
                fragment(YAMZ_236, 57, EXCITATION)]
        reply = response("S-0028", hits)
        self.assertEqual(set(self.works(reply)), {57})
        self.assertRegex(self.works(reply)[57].lower(), "целостность.*цепи возбуждения")
        self.assertIn("контакт", self.works(reply)[57].lower())
        self.assertNotIn("заменить", " ".join(row["text"] for row in reply["evidence"]).lower())
        no_source = response("S-0028", [hits[0]])
        self.assertFalse(no_source["evidence"])

    def test_zmz_uses_generator_mount_and_wire_connections(self):
        hit = fragment(ZMZ, 101, FASTENINGS)
        noise = fragment(ZMZ, 107, "л) неисправен подшипник генератора;  заменить подшипник на СТО")
        reply = response("S-0181", [noise, hit])
        self.assertEqual(set(self.works(reply)), {101})
        self.assertRegex(self.works(reply)[101].lower(), "креплен.*соединени.*провод")
        self.assertFalse(self.works(response("S-0181", [noise])))
        self.assertNotIn("заменить", " ".join(row["text"] for row in reply["evidence"]).lower())

    def test_yamz_536_accounts_for_noise_disappearing_without_belt(self):
        hits = [
            table(YAMZ_536, 215, [
                ("11.4 Слышен свист ремней привода вентилятора или генератора", "Полить поочередно ремни водой."),
                ("11.5 Неисправны или установлены неправильно: д) генератор;", "Проверить исправность и правильность установки агрегата. Снять ремень привода агрегата, заменить отказавший агрегат.")]),
            table(YAMZ_536, 231, [
                ("31.3 Наличие в генераторе постороннего предмета", "Удалить посторонний предмет"),
                ("31.4 Изношены или разрушены подшипники", "Заменить подшипник")])]
        reply = response("S-0082", hits)
        self.assertIn(215, self.works(reply))
        self.assertEqual({row["page"] for row in reply["evidence"]}, {215, 231})
        text = " ".join(row["text"].lower() for row in reply["evidence"])
        for term in ("генератор", "установк", "посторонн", "подшипник"):
            self.assertIn(term, text)
        self.assertNotRegex(text, "заменить|снять ремень|полить")
        unrelated = table(YAMZ_536, 215, [("11.5 Неисправен водяной насос.", "Проверить исправность и правильность установки агрегата.")])
        self.assertFalse(self.works(response("S-0082", [unrelated])))

    def test_unknown_gearbox_model_cannot_authorize_repairs(self):
        hits = [fragment(YAMZ_236, 135, NOISE), fragment(YAMZ_236, 128, FAMILIES)]
        reply = response("S-0039", hits)
        self.assertTrue(reply["needs_clarification"])
        self.assertRegex(reply["clarification"].lower(), "модел.*коробк|обозначени.*коробк")
        self.assertFalse(self.works(reply))
        self.assertNotIn("заменить", " ".join(row["text"] for row in reply["evidence"]).lower())
        for question in ("Модель пока неизвестна", "Кажется, ЯМЗ-236П", "Установлена ZF", "У нас ЯМЗ-236П?"):
            with self.subTest(question=question):
                answer = response("S-0039", hits, question, topic="gearbox-noise:ЯМЗ-236П")
                self.assertFalse(self.works(answer))

    def test_confirmed_gearbox_model_selects_noise_row_in_its_manual(self):
        hits = [fragment(YAMZ_236, 135, NOISE), fragment(YAMZ_236, 128, FAMILIES)]
        reply = response("S-0039", hits, "Установлена коробка ЯМЗ-236П.", topic="gearbox-noise:unknown")
        self.assertEqual(set(self.works(reply)), {135})
        work = self.works(reply)[135].lower()
        for term in ("картера сцепления", "картеру маховика", "задней опоры"):
            self.assertIn(term, work)
        self.assertNotIn("заменить", " ".join(row["text"] for row in reply["evidence"]).lower())
        absent = response("S-0039", [hits[1]], "Установлена коробка ЯМЗ-236П.", topic="gearbox-noise:unknown")
        self.assertFalse(self.works(absent))

    def test_known_edition_keeps_model_scope_when_index_has_only_the_noise_table(self):
        hit = fragment(YAMZ_236, 135, NOISE)
        hit["source_sha256"] = "4268a9519e02a668a826fe6d154de0360f6767015868ca2de7410ba570d73dd1"
        question = "Установлена КПП ЯМЗ-236Н."
        self.assertEqual(set(self.works(response("S-0039", [hit], question, topic="gearbox-noise:unknown"))), {135})
        hit["source_sha256"] = "исправленная редакция"
        self.assertFalse(self.works(response("S-0039", [hit], question, topic="gearbox-noise:unknown")))

    def test_question_about_a_named_transmission_is_not_an_observation(self):
        from ai_mechanic.knowledge.generator_gearbox import confirmed_gearbox_model, gearbox_observation_question

        for question in ("Какое масло залить в КП ЯМЗ-236Н?", "Подойдёт ли ЯМЗ-236П?", "Как проверить коробку передач?",
                         "Нужна схема для коробки ЯМЗ-236П", "Расскажите про коробку ЯМЗ-236Н"):
            self.assertIsNone(confirmed_gearbox_model(question))
            self.assertFalse(gearbox_observation_question(question))
        for question in ("Нет, КП не ЯМЗ-236П", "Я ошибся, коробка ZF", "Не знаю", "Модель пока неизвестна"):
            self.assertTrue(gearbox_observation_question(question))
            self.assertIsNone(confirmed_gearbox_model(question))

    def test_general_transmission_followup_still_needs_the_actual_box_model(self):
        hits = [fragment(YAMZ_236, 135, NOISE), fragment(YAMZ_236, 128, FAMILIES)]
        question = "Как проверить коробку передач при признаках этой неисправности?"
        for topic in ("", "gearbox-noise:unknown"):
            with self.subTest(topic=topic):
                reply = response("S-0039", hits, question, topic=topic)
                self.assertTrue(reply["needs_clarification"])
                self.assertIn("модель коробки", reply["clarification"])
                self.assertFalse(self.works(reply))

    def test_compound_generator_question_separates_inspection_and_missing_voltage_conditions(self):
        queries = ("Как проверить генератор и по каким условиям измеряют напряжение?",
                   "Что проверить на генераторе и при каких оборотах измерять напряжение?")
        cases = {
            "S-0028": fragment(YAMZ_236, 57, EXCITATION),
            "S-0181": fragment(ZMZ, 101, FASTENINGS),
            "S-0082": table(YAMZ_536, 215, [("11.5 Неисправны или установлены неправильно: д) генератор;",
                "Проверить исправность и правильность установки агрегата.")]),
        }
        for case, hit in cases.items():
            for question in queries:
                with self.subTest(case=case, question=question):
                    reply = response(case, [hit], question)
                    self.assertIn(hit["page"], self.works(reply))
                    self.assertIn("напряжени", reply["missing"].casefold())
                    self.assertRegex(reply["missing"].casefold(), "не найден|не указ|не привед|нет ")
            narrow = response(case, [hit], "Каково нормативное напряжение на генераторе при 2000 об/мин?")
            self.assertFalse(self.works(narrow))


    def test_former_gearbox_does_not_justify_an_inspection_of_the_current_unit(self):
        hits = [fragment(YAMZ_236, 135, NOISE), fragment(YAMZ_236, 128, FAMILIES)]
        for question in ("ЯМЗ-236П стояла раньше, сейчас стоит ZF", "ЯМЗ-236П была на старой коробке",
                         "ЯМЗ-236П сняли", "ЯМЗ-236П, но это обозначение старой коробки"):
            with self.subTest(question=question):
                reply = response("S-0039", hits, question, topic="gearbox-noise:ЯМЗ-236П")
                self.assertFalse(self.works(reply))
                self.assertTrue(reply["needs_clarification"])
                self.assertEqual(reply["topic"], "gearbox-noise:unknown")

    def test_general_question_reuses_server_confirmed_box_but_accepts_an_explicit_correction(self):
        hits = [fragment(YAMZ_236, 135, NOISE), fragment(YAMZ_236, 128, FAMILIES)]
        stated = response("S-0039", hits, "Установлена КП ЯМЗ-236П.", topic="gearbox-noise:unknown")
        repeat = response("S-0039", hits, "Что проверить?", topic=stated["topic"])
        self.assertEqual(self.works(repeat), self.works(stated))
        current = response("S-0039", hits, "ЯМЗ-236П стояла раньше, сейчас установлена КП ЯМЗ-236Н.", topic=stated["topic"])
        self.assertIn("ЯМЗ-236Н", self.works(current)[135])

