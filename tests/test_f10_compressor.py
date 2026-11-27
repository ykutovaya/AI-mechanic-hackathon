"""Проверяет выбор строк 39.3 и 42.2 при совместных признаках отказа компрессора и насоса ГУР."""

import unittest

from ai_mechanic.knowledge.search import retrieve


ENGINE = "ЯМЗ-536"
WORK = "Проверить крепление шестерни на валу компрессора пневмотормозов."
REMEDY = "Заменить вышедшие из строя детали, при необходимости, компрессор"
GEAR = ["39.3 Ослаблено крепление шестерни на валу компрессора (в этом случае не работает и насос ГУР)", REMEDY]
STEERING = ["42.2 Ослаблено крепление шестерни на валу компрессора пневмотормозов, см. п. 39.3", REMEDY]
SOOT = ["41.2 Чрезмерное отложение нагара на выпускных клапанах или каналах головки цилиндра компрессора",
        "Проверить наличие нагара. Промыть седло выпускного клапана и клапан, при необходимости заменить головку цилиндра в сборе."]
DIAGNOSIS = dict(status="anomaly", anomaly_type="F10", signal_quality="good",
                 anomaly_title="Компрессор пневмотормозов, насос ГУР",
                 description="Стук с частотой вращения компрессора в зоне привода агрегатов, давление в "
                             "пневмосистеме не растёт, усилие на рулевом колесе выросло")


def table(page, rows):
    return dict(document_id="yamz-536-re-2016", title="Руководство по эксплуатации ЯМЗ-536",
                engine_models=[ENGINE], applicability=[ENGINE + "|all"], page=page,
                kind="table", tables=[dict(rows=rows)], fault_code="F10", expert_rows=[502],
                text="\n".join(" | ".join(row) for row in rows))


class Backend:
    def __init__(self, hits):
        self.hits = hits

    def search(self, engine, query, *, variant, limit):
        return self.hits

    def search_fault(self, engine, query, *, code, variant, limit):
        return self.hits


class CompressorDriveTests(unittest.TestCase):
    def test_combined_symptoms_lead_to_one_inspection_supported_by_two_rows(self):
        steering = table(236, [SOOT, ["42.1 Разрушена муфта привода насоса ГУР", "Заменить муфту"], STEERING])
        compressor = table(235, [["39.2 Вышел из строя впускной клапан", "Снять головку цилиндра"], GEAR])
        # Слой PDF сводит конец 41.2 с заголовком раздела 42 в один абзац.
        mixed = dict(steering, tables=[], kind="paragraph", text=(SOOT[1] + "\n42 ВОЗРОСЛО УСИЛИЕ НА РУЛЕ\n"
                     "(НЕ РАБОТАЕТ НАСОС ГИДРОУСИЛИТЕЛЯ РУЛЯ)\n" + STEERING[0]))
        backend = Backend([mixed, steering, compressor])
        for question in ("Что проверить на двигателе?", WORK):
            with self.subTest(question=question):
                evidence = retrieve(backend, ENGINE, question, DIAGNOSIS)["evidence"]
                self.assertEqual([item["page"] for item in evidence], [235, 236])
                self.assertEqual({item["work"] for item in evidence if item.get("work")}, {WORK})
                text = " ".join(item["text"] for item in evidence)
                self.assertIn("39.3", text)
                self.assertIn("42.2", text)
                self.assertNotRegex(text.lower(), "головк|промыть|заменить|разобрать")
                self.assertEqual([item["excerpt"] for item in evidence], [GEAR[0], STEERING[0]])

    def test_followup_on_ml_result_selects_both_points_and_skips_oil_consumption(self):
        oil_row = ["22.1 Неисправен компрессор пневматического тормоза",
                   "Проверить наличие масла в нагнетательном трубопроводе"]
        backend = Backend([table(224, [oil_row]), table(235, [GEAR]), table(236, [SOOT, STEERING])])
        question = "Как проверить компрессор пневмотормозов или насос ГУР по результату диагностики?"
        answer = retrieve(backend, ENGINE, question, DIAGNOSIS)
        self.assertEqual([item["page"] for item in answer["evidence"]], [235, 236])
        self.assertEqual([item["excerpt"] for item in answer["evidence"]], [GEAR[0], STEERING[0]])
        self.assertEqual({entry["work"] for entry in answer["evidence"]}, {WORK})

        other = dict(DIAGNOSIS, description="Обнаружены следы масла в нагнетательном трубопроводе.")
        self.assertNotEqual(retrieve(backend, ENGINE, question, other)["topic"], "compressor-drive")
        standalone = "Как проверить наличие масла в нагнетательном трубопроводе компрессора?"
        self.assertNotEqual(retrieve(backend, ENGINE, standalone, DIAGNOSIS)["topic"], "compressor-drive")

    def test_neighboring_rows_do_not_substitute_for_missing_gear_evidence(self):
        backend = Backend([table(236, [SOOT, ["42.1 Разрушена муфта привода насоса ГУР", "Заменить муфту"]])])
        result = retrieve(backend, ENGINE, "Что проверить на двигателе?", DIAGNOSIS)
        self.assertFalse(result["evidence"])
        self.assertIn("крепления шестерни", result["missing"])


if __name__ == "__main__":
    unittest.main()
