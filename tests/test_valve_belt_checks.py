"""Проверяет первичные работы и применимость при F01 и F04."""

import unittest

from ai_mechanic.knowledge.search import retrieve


class Manual:
    def __init__(self, pages):
        self.pages = pages

    def search(self, engine, question, *, variant, limit):
        doc = {"ЯМЗ-236М2": "yamz-236m2-re-2010", "ЯМЗ-536": "yamz-536-re-2016"}.get(engine, "manual")
        return [dict(document_id=doc, page=p, text=t, engine_models=[engine]) for p, t in self.pages]


VALVES = "Клапанный механизм"
BELTS = "Ремённый привод агрегатов"
DRIVES = ("Нормально натянутые ремни при нажатии на середину ветви с усилием 40 Н (4 кгс) должны прогибаться: "
          "ремень водяного насоса - на 7-12 мм (рис. 8), ремень компрессора - на 4-6 мм на короткой ветви "
          "(рис. 9), ремни привода генератора - на 10-15 мм (рис. 10).")


def ask(engine, title, pages, *, question="", topic="", symptom=""):
    diagnosis = dict(status="anomaly", signal_quality="good", anomaly_type="F01" if title == VALVES else "F04",
                     anomaly_title=title, description=symptom)
    return retrieve(Manual(pages), engine, question or f"{title}. Что уточнить у водителя и что проверить на двигателе?",
                    diagnosis, topic)


class ValveBeltTests(unittest.TestCase):
    def assert_work(self, answer, page, word):
        self.assertTrue(any(e["page"] == page and word in (e.get("work") or "").lower()
                            for e in answer["evidence"]), answer)

    def test_hydraulic_tappets_only_assign_oil_check(self):
        result = ask("ЗМЗ-40905.10", VALVES, [
            (44, "Гидротолкатели обеспечивают беззазорный контакт кулачка распределительного вала с торцом клапана "
                 "за счет давления масла и действия пружины гидрокомпенсатора."),
            (91, "По мере прогрева двигателя до температуры охлаждающей жидкости +80…+90 °С, но не более чем через 30 минут "
                 "после достижения указанной температуры, стуки должны исчезнуть. Если стуки не исчезнут, необходимо "
                 "проверить подачу масла к гидротолкателям и гидронатяжителям или заменить неисправные гидротолкатели.")],
            symptom="Стук после прогрева")
        self.assert_work(result, 91, "подачу масла")
        text = " ".join(e["text"] for e in result["evidence"]).lower()
        self.assertIn("беззазорный", text)
        self.assertIn("30 минут", text)
        self.assertNotRegex(text, "заменить|щуп")

    def test_cold_valve_clearances_and_gauge_position(self):
        cases = [
            ("ЯМЗ-236М2", [
                (31, "Тепловые зазоры регулировать на холодном двигателе или менее чем через 15 минут после его остановки."),
                (33, "Вставив щуп в зазор между торцом клапана и носком коромысла, проверить зазоры у впускного и "
                     "выпускного клапанов первого цилиндра и, если необходимо, отрегулировать их в пределах 0,25 - 0,30 мм.")],
             33, ("холодном", "торцом клапана", "0,25", "0,30")),
            ("ЯМЗ-536", [
                (173, "Регулировку зазоров в клапанном механизме проводить на холодном двигателе или не ранее, чем через "
                      "один час после его остановки в соответствии с порядком работы цилиндров 1-5-3-6-2-4. "
                      "Величины зазоров между чашкой коромысла и торцом траверсы клапанов должны быть: "
                      "для впускных клапанов: 0,30 – 0,40 мм, для выпускных клапанов: 0,40 – 0,50 мм."),
                (175, "вставить в зазор «траверса 4 – чашка коромысла 5» щуп, соответствующей толщины;")],
             175, ("холодном", "торцом траверсы", "впускных", "0,30", "выпускных", "0,50"))]
        for engine, pages, gauge, expected in cases:
            with self.subTest(engine=engine):
                result = ask(engine, VALVES, pages)
                self.assert_work(result, gauge, "щуп")
                self.assertEqual({e["page"] for e in result["evidence"] if e.get("work")}, {p for p, _ in pages})
                self.assertTrue(all("холодном" in e["work"] for e in result["evidence"] if e.get("work")))
                text = " ".join(e["text"] for e in result["evidence"])
                for part in expected:
                    self.assertIn(part, text)

    def test_each_belt_drive_uses_its_own_manual(self):
        cases = [
            ("ЗМЗ-40905.10", 98, "Проверку состояния ремней привода вспомогательных агрегатов и подшипников натяжного "
                "и направляющих роликов производить при каждом техническом обслуживании. Состояние ремней проверять визуально.",
             "ролик"),
            ("ЗМЗ-5234.10", 54, "Пробуксовка ремня вентилятора | Отрегулировать натяжение ремня",
             "пробуксов"),
            ("ЯМЗ-536", 162, "Проверить состояние и натяжение приводных ремней, при необходимости заменить. "
                "При каждом втором ТО-2 проверить состояние натяжителя ремня на наличие посторонних шумов или люфта, "
                "при необходимости заменить.", "натяжител")]
        for engine, page, passage, word in cases:
            with self.subTest(engine=engine):
                result = ask(engine, BELTS, [(page, passage)], symptom="Свист ремня вентилятора")
                self.assert_work(result, page, word)
                self.assertNotIn("заменить", " ".join(e.get("work") or "" for e in result["evidence"]).lower())

    def test_yamz536_gives_applicable_inspection_method_for_the_named_belt(self):
        pages = [
            (162, "Проверить состояние и натяжение приводных ремней. При каждом втором ТО-2 проверить состояние "
                  "натяжителя ремня на наличие посторонних шумов или люфта, при необходимости заменить."),
            (104, "КОНТРОЛЬ НАТЯЖЕНИЯ РЕМНЕЙ ВЫПОЛНЯТЬ ТОЛЬКО НА НЕРАБОТАЮЩЕМ ДВИГАТЕЛЕ."),
            (104, "В рабочем положении метка поворотного корпуса должна находиться внутри зоны трех меток "
                  "неподвижного корпуса. При выходе из зоны ремень следует заменить."),
            (177, "Натяжение ремней привода генератора и агрегатов в процессе эксплуатации осуществляется "
                  "автоматически и не требует дополнительных регулировок. Для снятия ремня привода генератора "
                  "необходимо сжать пружину системы автоматического натяжения ремня.")]
        for drive, named in (("привода агрегатов", False), ("генератора", True)):
            with self.subTest(drive=drive):
                answer = ask("ЯМЗ-536", BELTS, pages, question=f"Как проверить ремень {drive}, его состояние и натяжение?")
                self.assertEqual({104, 162, 177}, {e["page"] for e in answer["evidence"]})
                text = " ".join(e["text"] for e in answer["evidence"]).casefold()
                for phrase in ("неработающем двигателе", "внутри зоны трех меток", "автоматически",
                               "не требует дополнительных регулировок", "шумов или люфта"):
                    self.assertIn(phrase, text)
                self.assertFalse(answer["missing"])
                self.assertNotRegex(text, "заменить|сжать пружину")
                if named:
                    self.assertFalse(answer["clarification"])
                else:
                    self.assertRegex(answer["clarification"].lower(), r"како[йм].*рем[не]")
        for component in ("генератора", "вентилятора", "компрессора кондиционера"):
            with self.subTest(component=component):
                chosen = ask("ЯМЗ-536", BELTS, pages, question=f"Это ремень {component}", topic="accessory-belt")
                self.assertEqual({104, 162, 177}, {e["page"] for e in chosen["evidence"]})
                self.assertFalse(chosen["clarification"])
        partial = ask("ЯМЗ-536", BELTS, pages[:1], question="Как проверить натяжение ремня генератора?")
        self.assertIn("метк", partial["missing"])
        self.assertFalse(any("внутри зоны" in e["text"] for e in partial["evidence"]))

    def test_identify_drive_before_showing_deflection(self):
        result = ask("ЯМЗ-236М2", BELTS, [(28, DRIVES)])
        self.assertIn("компрессор", result["clarification"].lower())
        self.assertIn("генератор", result["clarification"].lower())
        result = ask("ЯМЗ-236М2", BELTS, [(28, DRIVES)], question="Ремень компрессора", topic=result["topic"])
        text = " ".join(e["text"] for e in result["evidence"])
        self.assertIn("40 Н", text)
        self.assertRegex(text, "4\\s*[-–]\\s*6 мм")
        self.assertNotRegex(text, "10\\s*[-–]\\s*15 мм")

    def test_unrelated_page_does_not_become_work(self):
        for title in (VALVES, BELTS):
            result = ask("ЯМЗ-536", title, [(8,"Проверить подвеску двигателя.")])
            self.assertFalse(any(e.get("work") for e in result["evidence"]))
