"""Работы по свисту турбокомпрессора, пропускам зажигания и шуму цепи."""

import unittest

from ai_mechanic.knowledge.search import retrieve


CASES = {
    "F09": ("ЯМЗ-536", "yamz-536-re-2016", "Турбокомпрессор", "Свист и подвывание в районе турбокомпрессора, растёт с оборотами"),
    "F12": ("ЗМЗ-5234.10", "zmz-5234-10-rk-2025", "Зажигание", "Неравномерная работа с пропусками в одном цилиндре, двигатель трясёт на холостом ходу"),
    "F14": ("ЗМЗ-40905.10", "zmz-40904-40905-rk-2025", "Привод ГРМ", "Равномерный шум в передней части двигателя, усиливается на средних оборотах"),
}
TURBO = ("11.6 Шум в районе турбокомпрессора: а) неисправен турбокомпрессор; б) негерметична система впуска после турбокомпрессора;",
         "Осмотреть состояние гильз и поршней. Заменить турбокомпрессор. Проверить крепление соединительных рукавов и состояние воздухопроводов, при необходимости подтянуть крепление или заменить вышедшие из строя детали.")
SPARK = ("Зазор между электродами свечи не соответствует норме", "Проверить зазор круглым щупом и, при необходимости, отрегулировать")
PLUG = ("Также проверить цвет изолятора свечи и зазор между электродами. На правильно отрегулированном двигателе после длительной работы двигателя под нагрузкой цвет изолятора свечи должен быть светло-серым. "
        "Величина зазора между электродами свечи – 0,85…1,0 мм. Зазор измеряется круглым щупом (рис.39).")
CHAIN = ("е) повышенное удлинение цепи – равномерный шум в передней части двигателя, усиливающийся при средних оборотах и переменных нагрузках;",
         "проверить увеличение длины цепей, при необходимости заменить цепи со звездочками комплектом")


def fragment(code, page, text="", rows=()):
    engine, document, *_ = CASES[code]
    return dict(document_id=document, engine_models=[engine], page=page, text=text or "\n".join(" | ".join(row) for row in rows),
                tables=[dict(rows=rows)] if rows else [], kind="table" if rows else "text", fault_code=code, expert_rows=[1])


def response(code, hits, question="Что проверить в первую очередь?", symptoms=None, topic="", check_id=None, fault_title=None):
    engine, _, name, description = CASES[code]

    class Backend:
        def search(self, *args, **kwargs):
            return hits

        def search_fault(self, *args, **kwargs):
            return hits

        def page_hits(self, engine, document, page, *, variant, code):
            return [hit for hit in hits if hit["document_id"] == document and hit["page"] == page
                    and engine in hit["engine_models"]]

    return retrieve(Backend(), engine, question, diagnosis=dict(status="anomaly", signal_quality="good",
                    session_id=check_id, anomaly_type=code, anomaly_title=fault_title or name, description=description if symptoms is None else symptoms), topic=topic)


class DiagnosticWorksTests(unittest.TestCase):
    def test_f09_picks_only_subparagraph_b_from_11_6(self):
        noise = fragment("F09", 216, rows=[TURBO, ("11.7 Неисправна одна или несколько форсунок", "Выполнить тест форсунок")])
        evidence = response("F09", [noise])["evidence"]
        self.assertEqual([row["page"] for row in evidence if row["work"]], [216])
        work = evidence[0]["work"].casefold()
        self.assertIn("крепление соединительных рукавов", work)
        self.assertIn("состояние воздухопроводов", work)
        self.assertIn("11.6", evidence[0]["text"])
        self.assertNotRegex(evidence[0]["text"].casefold(), r"лопат|заменить|форсун|гильз|люфт")
        self.assertTrue(any(row["work"] for row in response("F09", [noise], evidence[0]["work"])["evidence"]))

    def test_unrelated_subparagraph_or_table_cannot_support_the_requested_work(self):
        for code, hit in (
            ("F09", fragment("F09", 216, rows=[(TURBO[0].replace("11.6", "11.7"), TURBO[1])])),
            ("F14", fragment("F14", 107, rows=[(CHAIN[0], "произвести ремонт двигателя")])),
            ("F12", fragment("F12", 54, rows=[("Недостаточная мощность искры", "Заменить неисправную свечу")])),
        ):
            with self.subTest(code=code):
                self.assertFalse(response(code, [hit])["evidence"])

    def test_f12_identifies_spark_plug_inspection_and_gauge_gap(self):
        hits = [fragment("F12", 54, rows=[SPARK]), fragment("F12", 65, PLUG)]
        evidence = response("F12", hits)["evidence"]
        self.assertEqual({item["page"] for item in evidence}, {54, 65})
        works = {item["work"] for item in evidence if item["work"]}
        self.assertEqual(len(works), 1)
        self.assertEqual({item["page"] for item in evidence if item["work"]}, {54, 65})
        self.assertRegex(works.pop().casefold(), "осмотреть свечу.*зазор между электродами круглым щупом")
        text = " ".join(item["text"] for item in evidence).casefold()
        for term in ("цвет изолятора", "под нагрузкой", "круглым щупом", "0,85", "1,0 мм"):
            self.assertIn(term, text)
        self.assertNotIn("заменить", text)
        measured = response("F12", hits[:1])["evidence"]
        self.assertTrue(any(item["work"] for item in measured))
        self.assertNotIn("0,85", " ".join(item["text"] for item in measured))

    def test_chain_and_tensioner_have_separate_symptoms_and_sources(self):
        chain = fragment("F14", 107, rows=[CHAIN])
        tensioner = fragment("F14", 143, "Гидронатяжитель подлежит проверке и ремонту при обнаружении стука в зоне "
                             "передней крышки головки цилиндров и крышки цепи. Стук отчетливо слышен при резком "
                             "сбросе частоты вращения коленчатого вала с помощью стетофонендоскопа, приставленного "
                             "к пробке крышки верхнего или нижнего гидронатяжителя, причинами которого могут быть "
                             "заклинивание плунжера и негерметичность шарикового клапана. Кроме гидронатяжителя "
                             "причинами стука также могут быть: износ звездочек, повышенная вытяжка цепи, "
                             "разрушение успокоителя цепи.")
        question = "Как проверить цепь ГРМ и гидронатяжитель по результату диагностики?"
        for available, pages in (([chain, tensioner], {107, 143}), ([chain], {107}), ([tensioner], {143})):
            with self.subTest(pages=pages):
                result = response("F14", available, question)
                self.assertEqual({e["page"] for e in result["evidence"]}, pages)
                if 143 in pages:
                    info = next(e for e in result["evidence"] if e["page"] == 143)
                    self.assertFalse(info["work"])
                    detail = info["text"].lower() + " " + result["clarification"].lower()
                    for term in ("стук", "сброс", "стетофонендоскоп", "?"):
                        self.assertIn(term, detail)
                else:
                    self.assertIn("гидронатяжител", result["missing"].lower())
                if 107 not in pages:
                    self.assertIn("длин", result["missing"].lower())
        for statement, expected_pages, is_work in (
                ("При сбросе оборотов слышен стук у передней крышки цепи.", {143}, False),
                ("При резком сбросе оборотов слышен стук у передней крышки цепи.", {143}, True),
                ("Стука нет, шум равномерный.", {107}, False)):
            with self.subTest(statement=statement):
                prompt = response("F14", [chain, tensioner], question)
                answer = response("F14", [chain, tensioner], statement, topic=prompt["topic"])
                self.assertEqual({e["page"] for e in answer["evidence"]}, expected_pages)
                self.assertEqual(any(e["work"] for e in answer["evidence"] if e["page"] == 143), is_work)
        first = response("F14", [chain, tensioner],
                         "Привод ГРМ. Что уточнить у водителя и что проверить на двигателе?")
        self.assertEqual({e["page"] for e in first["evidence"]}, {107})

    def test_f14_chain_work_depends_on_location_type_and_speed_of_noise(self):
        hits = [fragment("F14", 107, rows=[CHAIN, ("л) неисправен подшипник генератора", "заменить подшипник на СТО")])]
        evidence = response("F14", hits)["evidence"]
        self.assertEqual([row["page"] for row in evidence if row["work"]], [107])
        self.assertRegex(evidence[0]["work"].lower(), r"измерить удлинение цеп")
        self.assertIn("переменн", evidence[0]["text"].casefold())
        self.assertNotIn("заменить", evidence[0]["work"].casefold())
        self.assertIn("необходим", evidence[0]["text"].casefold())
        self.assertNotIn("генератор", evidence[0]["text"].casefold())
        unrelated = response("F14", hits, symptoms="Резкий стук сзади на холостом ходу")
        self.assertFalse(any(row["work"] for row in unrelated["evidence"]))


class TimingChainDialogueTests(unittest.TestCase):
    def test_initial_reply_and_unchecked_followup_keep_only_inspection_work(self):
        hits = [fragment("F14", 107, rows=[CHAIN])]
        initial = response("F14", hits,
                           "Цепь ГРМ, гидронатяжитель. Что уточнить у водителя и что проверить на двигателе?",
                           check_id="S-0152", fault_title="Цепь ГРМ, гидронатяжитель")
        self.assertIn("Удалось ли проверить удлинение цепей ГРМ?", initial["clarification"])
        for phrase in ("впускном", "выпускном", "втулочные", "зубчатые"):
            self.assertIn(phrase, initial["clarification"])
        for result in (initial, response("F14", hits, "Нет, цепи пока не проверяли.", topic=initial["topic"], check_id="S-0152")):
            self.assertEqual(inspection_works(result), [(107, "Проверить удлинение цепей привода ГРМ")])
            answer = result["evidence"][0]["text"]
            self.assertIn("по результатам дефектовки", answer)
            self.assertIn("предельное удлинение", answer)

    def test_phase_report_shows_both_drive_norms_and_signed_angular_tolerance(self):
        hits = [
            fragment("F14", 135, "В процессе эксплуатации в результате удлинения цепей и износа зубьев "
                     "звёздочек возможно значительное отклонение фаз газораспределения от номинальных значений."),
            fragment("F14", 138, "I. Привод распределительных валов со втулочными цепями: II. Привод "
                     "распределительных валов с зубчатыми цепями: Рис.104. Схема положения распределительных валов "
                     "при положении поршня первого цилиндра в ВМТ такта сжатия. При измерении ведущая ветвь цепи "
                     "(в районе верхнего и среднего успокоителей) должна быть натянута."),
            fragment("F14", 139, "В случае если отклонения углового положения кулачков распределительных "
                     "валов превышают допустимые ±2°40′ от номинального значения, требуется провести корректировку "
                     "установки фаз газораспределения."),
        ]
        initial = response("F14", hits, check_id="S-0152")
        for message in ("Проверили фазы. Впуск 23°30′, выпуск 19°30′, цепи втулочные.",
                        "Впуск 20°, выпуск 19°. Зубчатые.", "Как проверить фазы ГРМ?"):
            with self.subTest(message=message):
                result = response("F14", hits, message, topic=initial["topic"], check_id="S-0152")
                self.assertEqual({item["page"] for item in result["evidence"]}, {135, 138, 139})
                text = " ".join(item["text"] for item in result["evidence"])
                for term in ("20°30′", "19°30′", "20° и 19°", "±2°40′", "ВМТ", "натянутой ведущей ветви"):
                    self.assertIn(term, text)
                self.assertIn("корректиров", text)
                self.assertEqual(inspection_works(result), [])

    def test_marks_followup_cites_warning_and_recheck_without_replacement(self):
        hits = [
            fragment("F14", 141, "При установке звёздочки на дополнительное отверстие заводская "
                     "установочная метка (метки) 1 на звездочке не будет совпадать с верхней плоскостью "
                     "головки цилиндров при положении поршня 1-го цилиндра в ВМТ."),
            fragment("F14", 142, "13. Повернуть коленчатый вал по ходу вращения на два оборота. "
                     "14. Проверить установку фаз газораспределения с помощью сектора и шаблона кулачка 252° со стрелкой."),
        ]
        initial = response("F14", hits, check_id="S-0152")
        result = response("F14", hits, "Проверили только метки, они смещены.", topic=initial["topic"], check_id="S-0152")
        self.assertEqual({item["page"] for item in result["evidence"]}, {141, 142})
        text = " ".join(item["text"] for item in result["evidence"])
        for term in ("дополнительное отверстие", "двух оборотов", "сектором и шаблоном"):
            self.assertIn(term, text)
        self.assertEqual(inspection_works(result), [])

    def test_missing_page_does_not_supply_norm_or_replacement(self):
        unrelated = fragment("F14", 139, "Проверить углы на 138 странице.")
        wrong_page = fragment("F14", 140, "Рис.104. Привод распределительных валов со втулочными цепями.")
        result = response("F14", [unrelated, wrong_page], "Проверили фазы.", topic="chain-noise", check_id="S-0152")
        self.assertFalse(result["evidence"])
        self.assertTrue(result["missing"])
        self.assertEqual(inspection_works(result), [])


    def test_replacement_requires_explicit_defect_inspection_outcome(self):
        hits = [fragment("F14", 107, rows=[CHAIN])]
        initial = response("F14", hits, check_id="S-0152")
        confirmation = "По результатам дефектовки подтверждено, что цепи ГРМ требуют замены."
        result = response("F14", hits, confirmation, topic=initial["topic"], check_id="S-0152")
        self.assertEqual(inspection_works(result), [(107, "Заменить цепи ГРМ со звёздочками комплектом")])

        revoked = response("F14", hits, "По результатам дефектовки необходимость замены цепей не подтверждена.",
                           topic=result["topic"], check_id="S-0152")
        self.assertEqual(inspection_works(revoked), [(107, "Проверить удлинение цепей привода ГРМ")])

        for message in ("Можно ли по результатам дефектовки заменить цепи ГРМ?",
                        "По результатам дефектовки подтвердили только шум и предлагаем заменить цепи ГРМ.",
                        "По результатам дефектовки необходимость замены цепей не подтверждена.",
                        "По меткам решил, что цепи требуют замены."):
            with self.subTest(message=message):
                result = response("F14", hits, message, topic=initial["topic"], check_id="S-0152")
                self.assertNotIn("Заменить цепи ГРМ со звёздочками комплектом",
                                 [work for _, work in inspection_works(result)])

        unrelated = fragment("F14", 107, rows=[(CHAIN[0], "проверить увеличение длины цепей")])
        result = response("F14", [unrelated], confirmation, topic=initial["topic"], check_id="S-0152")
        self.assertEqual(inspection_works(result), [])

def inspection_works(result):
    return [(item["page"], item["work"]) for item in result["evidence"] if item["work"]]
