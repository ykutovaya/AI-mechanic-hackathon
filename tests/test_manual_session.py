"""Проверки поиска и работы с результатом диагностической сессии."""

from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pymupdf

from ai_mechanic.ask import ManualSession


class ManualSessionTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        vehicle = {
            "vehicle_id": "V1", "model": "Машина", "engine_model": "ДВС-1",
            "sessions": [{"session_id": "S1", "vehicle_id": "V1", "status": "anomaly"}],
        }
        self.write_json("data/raw/vehicles.json", {"vehicles": [vehicle]})
        path = self.root / "data/raw/manuals/manual.pdf"
        path.parent.mkdir()
        with pymupdf.open() as pdf:
            pdf.new_page()
            pdf.new_page()
            pdf.save(path)
        self.manual = {
            "document_id": "manual", "title": "Руководство ДВС-1", "edition": "2026",
            "path": "data/raw/manuals/manual.pdf", "engine_models": ["ДВС-1"],
            "page_count": 2, "source_sha256": sha256(path.read_bytes()).hexdigest(),
        }
        self.write_json("data/annotations/manuals.json", [self.manual])
        self.diagnostic = {
            "vehicle_id": "V1", "session_id": "S1", "status": "anomaly",
            "anomaly_type": "F02", "anomaly_title": "Низкое давление масла",
            "description": "Горит лампа.", "anomaly_score": 0.91, "confidence": 0.47,
            "signal_quality": "warning", "model_version": "mock-v1",
        }
        self.ml_calls = []
        self.search_calls = []
        self.evidence = [
            dict(self.manual, page=1, kind="fact", applicability=["ДВС-1|all"],
                 text="Измерять на холодном двигателе."),
            dict(self.manual, page=2, kind="fact", applicability=["ДВС-1|all"],
                 text="Допустимый зазор 0,3 мм.", work="Измерить зазор."),
        ]
        self.session = ManualSession(
            root=self.root, search=object(), diagnosis_provider=self.fetch, retriever=self.retrieve)

    def write_json(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def fetch(self, vehicle_id, session_id):
        self.ml_calls.append((vehicle_id, session_id))
        return self.diagnostic

    def retrieve(self, search, engine, question, *, diagnosis, topic, variant, procedures):
        self.search_calls.append((engine, diagnosis, topic, variant))
        return {"queries": [question], "evidence": self.evidence, "topic": "зазор"}

    def test_question_keeps_conditions_and_exact_pages_without_calling_ml(self):
        answer = self.session.ask("V1", "Как измерить зазор?", check_id="S1")
        self.assertEqual(self.ml_calls, [])
        self.assertEqual(self.search_calls, [("ДВС-1", None, "", None)])
        self.assertIn("на холодном двигателе", answer["text"])
        self.assertIn("0,3 мм", answer["text"])
        self.assertEqual([source["page"] for source in answer["sources"]], [1, 2])
        self.assertTrue(answer["sources"][1]["url"].endswith("#page=2"))

    def test_explicit_ml_fetch_and_reuse(self):
        self.assertEqual(self.session.machines[0]["history"][0]["check_id"], "S1")
        diagnosis = self.session.diagnose("V1", "S1")
        answer = self.session.ask("V1", "Что проверить?", check_id="S1", diagnosis=diagnosis)
        self.assertEqual(self.ml_calls, [("V1", "S1")])
        self.assertEqual(self.search_calls[0][:2], ("ДВС-1", diagnosis))
        self.assertIn("F02", answer["text"])
        self.assertIn("Горит лампа.", answer["text"])
        self.assertIn("помехи", answer["text"])
        self.assertIn("низкая", answer["text"])
        self.assertEqual(answer["inspection_steps"], [
            {"document_id": "manual", "page": 2, "role": "work", "text": "Измерить зазор."},
        ])

    def test_bad_or_failed_recording_stops_advice_and_offers_retry(self):
        for change in ({"status": "check_failed"}, {"signal_quality": "bad"}):
            with self.subTest(change=change):
                diagnosis = dict(self.diagnostic, **change)
                answer = self.session.ask("V1", "Что проверить?", check_id="S1", diagnosis=diagnosis)
                self.assertIn("Повторите запись", answer["text"])
                self.assertNotIn("Горит лампа", answer["text"])
                self.assertEqual(answer["sources"], [])
                self.assertEqual(answer["inspection_steps"], [])
        self.assertEqual(self.search_calls, [])

    def test_rejects_wrong_session_before_search_and_service(self):
        with self.assertRaises(ValueError):
            self.session.diagnose("V1", "S2")
        with self.assertRaises(ValueError):
            self.session.ask("V1", "Что проверить?", check_id="S1",
                             diagnosis=dict(self.diagnostic, vehicle_id="V2"))
        self.assertEqual(self.search_calls, [])
        self.assertEqual(self.ml_calls, [])

    def test_ignores_other_variant_and_unavailable_physical_page(self):
        self.evidence[0]["applicability"] = ["ДВС-1|other"]
        self.evidence[1]["page"] = 3
        answer = self.session.ask("V1", "Как измерить зазор?")
        self.assertEqual(answer["sources"], [])
        self.assertEqual(answer["inspection_steps"], [])
        self.assertNotIn("0,3 мм", answer["text"])
        self.assertIn("страница 3", " ".join(answer["warnings"]))

    def test_oil_alarm_and_workshop_check_of_removed_pump_are_distinct(self):
        hits = [
            dict(self.evidence[0], section="Масляный насос", text=(
                "Падение давления в системе смазки может быть вызвано износом деталей "
                "масляного насоса или подшипников коленчатого и распределительного валов. "
                "При значительных износах деталей насоса он начинает работать шумно. "
                "Чтобы выявить неисправности насоса, его необходимо снять с двигателя и разобрать.")),
            dict(self.evidence[1], section="Разборка насоса", work=None, text=(
                "Проверить износ шестерен, замерив их высоту. "
                "Новые шестерни имеют размер высоты 40,075…40,100 мм. "
                "При значительном износе заменить дефектную шестерню.")),
        ]

        class Search:
            def search(self, engine, query, *, variant, limit):
                return hits

        session = ManualSession(root=self.root, search=Search())
        answer = session.ask("V1", "Что проверить и чем опасно отклонение?",
                             check_id="S1", diagnosis=self.diagnostic)
        self.assertIn("износом деталей масляного насоса", answer["text"])
        self.assertNotIn("40,075…40,100", answer["text"])
        self.assertNotIn("снять с двигателя и разобрать", answer["text"])
        self.assertFalse(any(step["role"] == "work" for step in answer["inspection_steps"]))

        manual = session.ask("V1", "Масляный насос уже снят и разобран. Как проверить износ его шестерён и какой должна быть их высота?",
                             check_id="S1", diagnosis=self.diagnostic)
        self.assertIn("замерив их высоту", manual["text"])
        self.assertIn("Новые шестерни имеют размер высоты 40,075…40,100 мм", manual["text"])
        self.assertEqual([source["page"] for source in manual["sources"]], [2])
        self.assertFalse(manual["inspection_steps"])

    def test_f02_only_initial_checks_become_works_before_measurement(self):
        from ai_mechanic.knowledge.search import retrieve

        engine = "ЗМЗ-5234.10"
        def hit(page, text, **fields):
            return dict(document_id="zmz-5234-10-rk-2025", engine_models=[engine],
                        page=page, section="Низкое давление масла", text=text, **fields)
        rows = [["Причина неисправности", "Метод устранения"], ["Низкое давление масла", None],
                ["Износ шестерен масляного насоса", "Заменить шестерни"]]
        hits = [
            hit(23, "Сигнализатор аварийного давления масла сигнализирует о падении давления ниже 40...80 кПа "
                    "(0,4...0,8 кгс/см2). В случае загорания сигнализатора на средней или большой частотах "
                    "вращения коленчатого вала двигателя необходимо немедленно заглушить двигатель и до устранения "
                    "неисправности двигатель не заводить."),
            hit(28, "Проверять уровень масла необходимо ежедневно. Уровень поддерживать между метками, "
                    "ближе к верхней метке, не превышая её."),
            hit(56, "Низкое давление масла", tables=[dict(rows=rows)]),
        ]
        class Search:
            def search(self, *_args, **_kwargs):
                return hits

        diag = dict(self.diagnostic, description="Загорается сигнализатор аварийного давления масла, слышен глухой стук на оборотах выше холостых")
        answer = retrieve(Search(), engine, "Низкое давление масла. Что уточнить у водителя и что проверить на двигателе?", diagnosis=diag)
        works = [item["work"] for item in answer["evidence"] if item["work"]]
        self.assertEqual(len(works), 1)
        self.assertIn("уровень масла", works[0])
        self.assertIn("обороты", answer["clarification"])
        self.assertIn("показание и единицы", answer["clarification"])

    def test_manual_search_finds_injector_with_an_unrelated_ml_result(self):
        from ai_mechanic.knowledge.search import retrieve
        from unittest.mock import Mock

        engine = "ЯМЗ-236М2"
        hits = [
            dict(document_id="yamz-236m2-re-2010", page=37, section="Техническое обслуживание", text=(
                "После длительной работы на двигателе допускается снижение давления начала впрыскивания "
                "форсунок до 18,1 МПа (185 кгс/см2). При обслуживании форсунок, замене хотя бы одной "
                "форсунки, все форсунки отрегулировать на давление начала впрыскивания "
                "22,6+0,8 МПа (230+8 кгс/см2).")),
            dict(document_id="yamz-236m2-re-2010", page=52, section="Двигатель не разви", text=(
                "Двигатель не разви вает мощности, дымит\n"
                "Нарушение регулировки или Отрегулиро вать форсунку и, есл и\n"
                "засорение форсунки необходимо, промыть и прочистить\nее")),
        ]
        search = Mock(**{"search.return_value": hits})
        diagnosis = dict(self.diagnostic, session_id="S-0014", anomaly_type="F04",
                         anomaly_title="Ремённый привод агрегатов", description="Свист при наборе оборотов.")
        for current in (None, diagnosis):
            for question in ("форсунка", "дефект топливной форсунки", "Как проверить форсунку?",
                             "Регулировка форсунки"):
                with self.subTest(question=question, diagnosis=bool(current)):
                    result = retrieve(search, engine, question, diagnosis=current, topic="accessory-belt")
                    self.assertFalse(result["asks_about_fault"])
                    self.assertEqual({item["page"] for item in result["evidence"]}, {37, 52})
                    self.assertTrue(all(not item["work"] for item in result["evidence"]))
                    answer = " ".join(item["text"] for item in result["evidence"])
                    for fragment in ("длительной работы", "18,1 МПа", "22,6+0,8 МПа", "дымит", "засорение форсунки"):
                        self.assertIn(fragment, answer)
        result = retrieve(search, engine, "Момент затяжки форсунки?")
        self.assertFalse(result["evidence"])

    def test_short_name_finds_a_section_on_a_different_component(self):
        from ai_mechanic.knowledge.search import retrieve
        from unittest.mock import Mock

        hits = [
            dict(document_id="manual", page=1, section="Схема жгута", text="1 – жгут проводов, 2 – стартер."),
            dict(document_id="manual", page=2, section="Стартер", text=(
                "Стартер установлен с правой стороны двигателя. "
                "Для пуска двигателя установлен стартер с электромагнитным тяговым реле.")),
        ]
        search = Mock(**{"search.return_value": hits})
        answer = retrieve(search, "ЯМЗ-236М2", "стартер")
        self.assertEqual([item["page"] for item in answer["evidence"]], [2])
        self.assertIn("тяговым реле", answer["evidence"][0]["text"])
        self.assertFalse(answer["evidence"][0]["work"])
        self.assertFalse(retrieve(search, "ЯМЗ-236М2", "Момент затяжки стартера?")["evidence"])

    def test_short_ambiguous_name_stays_open_for_clarification(self):
        from ai_mechanic.knowledge.search import retrieve
        from unittest.mock import Mock

        hit = dict(document_id="manual", page=36, section="Промывка фильтра", text=(
            "При возможном заедании редукционного клапана масляного насоса клапан вывернуть. "
            "Клапан промыть в дизельном топливе и установить на место."))
        search = Mock(**{"search.return_value": [hit]})
        answer = retrieve(search, "ЯМЗ-236М2", "клапаны")
        self.assertFalse(answer["evidence"])
        self.assertTrue(answer["needs_clarification"])
        self.assertIn("Уточните", answer["clarification"])

    def test_air_filter_replacement_uses_the_filter_type_and_verified_pages(self):
        from unittest.mock import Mock, patch

        engine = "ЯМЗ-236М2"
        self.write_json("data/raw/vehicles.json", {"vehicles": [{
            "vehicle_id": "V1", "model": "МАЗ-5551", "engine_model": engine,
            "sessions": [{"session_id": "S1", "vehicle_id": "V1", "status": "anomaly"}],
        }]})
        text = {
            9: "Воздушный фильтр Инерционно-масляный или сухого типа",
            43: "ОБСЛУЖИВАНИЕ ИНЕРЦИОННО - МАСЛЯНОГО ФИЛЬТРА. Снять крышку фильтра, извлечь "
                "фильтрующий элемент, промыть его. Вымыть масляную ванну и залить в нее масло до метки.",
            44: "ОБСЛУЖИВАНИЕ ВОЗДУШНОГО ФИЛЬТРА СУХОГО ТИПА. "
                "Прокладки, имеющие надрывы, заменить. При использовании двух фильтрующих элементов "
                "установка проставки между ними обязательна. Стрелка на бункере должна быть направлена вверх. "
                "Для обслуживания элемента снять бункер, отвернуть гайку крепления и вынуть элемент из корпуса фильтра.",
            45: "После каждого обслуживания элемента или при установке нового проверить его состояние визуально, "
                "подсвечивая изнутри лампой.",
        }
        document = "yamz-236m2-re-2010"
        def read_pages(_registry, _root, _document, _engine, pages, **_kwargs):
            return {page: dict(document_id=document, page=page, text=text[page]) for page in pages}

        search = Mock(**{"search.return_value": []})
        session = ManualSession(root=self.root, search=search)
        diagnosis = dict(self.diagnostic, anomaly_type="F04", anomaly_title="Ремённый привод агрегатов")
        args = {"check_id": "S1", "diagnosis": diagnosis}
        with patch("ai_mechanic.knowledge.sources.verified_pdf_pages", side_effect=read_pages), patch(
                "ai_mechanic.knowledge.sources.resolve_source",
                side_effect=lambda hit, *_args: dict(document_id=document, page=hit["page"])):
            self.assertFalse(session.ask("V1", "сухой", **args)["sources"])
            for question in ("Как поменять воздушный фильтр?", "Как заменить воздушный фильтр?"):
                with self.subTest(question=question):
                    answer = session.ask("V1", question, **args)
                    self.assertTrue(answer.get("needs_clarification"))
                    self.assertEqual({ref["page"] for ref in answer["sources"]}, {9, 43, 44, 45})
                    for phrase in ("сухой", "инерционно-масляный", "маркировк"):
                        self.assertIn(phrase, answer["text"].lower())
                    self.assertNotIn("F04", answer["text"])
                    self.assertFalse(answer["inspection_steps"])
            dry = session.ask("V1", "сухой", **args)
            self.assertEqual({ref["page"] for ref in dry["sources"]}, {44, 45})
            self.assertFalse(dry.get("needs_clarification"))
            for phrase in ("бункер", "гайку", "прокладки", "проставк", "лампой"):
                self.assertIn(phrase, dry["text"].lower())
            self.assertNotIn("масляную ванну", dry["text"])
            oil_bath = session.ask("V1", "Как обслужить инерционно-масляный воздушный фильтр?", **args)
            self.assertEqual({ref["page"] for ref in oil_bath["sources"]}, {43})
            self.assertIn("масло до метки", oil_bath["text"])
            self.assertFalse(oil_bath["inspection_steps"])
            self.assertFalse(session.ask("V1", "Как заменить топливный фильтр?", **args)["sources"])

    def test_carburetor_question_identifies_the_selected_engine_fuel_system(self):
        from unittest.mock import Mock, patch

        examples = {
            "ЯМЗ-236М2": ("yamz-236m2-re-2010", {6: "Способ смесеобразования Непосредственный впрыск",
                          8: "Топливный насос высокого давления Шестиплунжерный Восьмиплунжерный Форсунки закрытого типа"},
                          "непосредственным впрыском"),
            "ЗМЗ-5234.10": ("zmz-5234-10-rk-2025", {
                36: "На двигателе установлен двухкамерный карбюратор К-135МУ."}, "К-135МУ"),
            "ЗМЗ-40904.10": ("zmz-40904-40905-rk-2025", {
                72: "Подача топлива осуществляется посредством распределенного впрыска топлива "
                    "во впускные каналы головки цилиндров в зону впускных клапанов электромагнитными форсунками, "
                    "работающими по сигналу микропроцессорного блока управления."}, "распределённый впрыск"),
            "ЯМЗ-536": ("yamz-536-re-2016", {
                126: "Топливная система аккумуляторного типа – Common Rail System (CRS) с электронным "
                     "управлением подачей топлива производства фирмы Robert Bosch (Германия)."}, "Common Rail"),
        }
        pdf_path = "data/raw/manuals/fuel-systems.pdf"
        with pymupdf.open() as pdf:
            for _ in range(126):
                pdf.new_page()
            pdf.save(self.root / pdf_path)
        source_sha = sha256((self.root / pdf_path).read_bytes()).hexdigest()
        search = Mock(**{"search.return_value": []})
        diagnosis = dict(self.diagnostic, anomaly_type="F04", anomaly_title="Ремённый привод агрегатов")
        for engine, (document, pages, expected) in examples.items():
            with self.subTest(engine=engine):
                self.write_json("data/raw/vehicles.json", {"vehicles": [{
                    "vehicle_id": "V1", "model": "Машина", "engine_model": engine,
                    "sessions": [{"session_id": "S1", "vehicle_id": "V1", "status": "anomaly"}],
                }]})
                row = dict(self.manual, document_id=document, engine_models=[engine], path=pdf_path,
                           page_count=126, source_sha256=source_sha)
                self.write_json("data/annotations/manuals.json", [row])
                session = ManualSession(root=self.root, search=search)
                hits = {page: dict(row, page=page, text=body) for page, body in pages.items()}
                with patch("ai_mechanic.knowledge.sources.verified_pdf_pages", return_value=hits):
                    answer = session.ask("V1", "А какой карбюратор на этом двигателе?",
                                         check_id="S1", diagnosis=diagnosis)
                    self.assertIn(expected, answer["text"])
                    if engine != "ЗМЗ-5234.10":
                        self.assertIn("карбюратор не предусмотрен", answer["text"])
                    self.assertNotIn("F04", answer["text"])
                    self.assertEqual({source["page"] for source in answer["sources"]}, set(pages))
                    self.assertFalse(answer["inspection_steps"])
                    self.assertFalse(session.ask("V1", "Как разобрать карбюратор?")["sources"])
                with patch("ai_mechanic.knowledge.sources.verified_pdf_pages", return_value={}):
                    self.assertFalse(session.ask("V1", "Какой карбюратор?")["sources"])

    def test_oil_reference_keeps_both_seasonal_conditions(self):
        from ai_mechanic.knowledge.search import retrieve
        from unittest.mock import Mock

        hit = dict(document_id="yamz-236m2-re-2010", page=12, section="Топливо и смазочные материалы", text=(
            "При работе двигателя летом (при температуре воздуха\n"
            "выше плюс 5ºС) применять:\n     −  моторное масло М-10-Г2к по ГОСТ-8581-78.\n"
            "Зимой (при температуре воздуха ниже плюс 5ºС):\n     −  моторное масло М-8-Г2к по ГОСТ-8581-78."))
        search = Mock(**{"search.return_value": [hit]})
        answer = retrieve(search, "ЯМЗ-236М2", "Какое моторное масло использовать?")
        self.assertEqual([item["page"] for item in answer["evidence"]], [12])
        text = answer["evidence"][0]["text"]
        for fragment in ("выше плюс 5ºС", "М-10-Г2к", "ниже плюс 5ºС", "М-8-Г2к", "ГОСТ-8581-78"):
            self.assertIn(fragment, text)

    def test_valve_clearance_uses_requested_manual_and_one_complete_step(self):
        from ai_mechanic.knowledge.search import retrieve
        from unittest.mock import Mock
        excerpt = ("Вставив щуп в зазор между торцом клапана и носком коромысла, проверить зазоры "
                   "первого цилиндра и отрегулировать их в пределах 0,25 - 0,30 мм.")
        source = dict(self.evidence[0], text=excerpt, section="Регулировка")
        other = dict(source, title="236.01.01 РК", document_id="repair",
                     text=excerpt + " Щуп должен входить с усилием.")
        right = dict(source, title="236-3902150-Б РЭ", document_id="operation")
        result = retrieve(Mock(**{"search.return_value": [other, right]}), "ДВС-1",
                          "По руководству по эксплуатации, куда вставить щуп при регулировке клапанов "
                          "первого цилиндра и какой зазор выставить?")
        self.assertEqual([(v["document_id"], v["excerpt"]) for v in result["evidence"]], [("operation", excerpt)])

    def test_installation_torque_follows_bolt_number_within_same_step(self):
        from ai_mechanic.knowledge.search import retrieve
        from unittest.mock import Mock
        question = ("При установке термостатов какой момент затяжки болтов отводящего патрубка "
                    "на сервисном модуле?")
        operation = ("3. Установить отводящий патрубок (6) на сервисный модуль, предварительно ввернув "
                     "от руки 3 болта (7) в отверстия сервисного модуля.\n"
                     "Окончательно затянуть болты (7) ключом S=13. Момент затяжки Мкр = 20–25 Н·м.")
        hit = dict(self.evidence[0], kind="text", section="Установка термостатов", text=operation)
        wrong = dict(hit, section="Установка сервисного модуля", text=operation.replace("20–25", "60–65"))
        def answer(hits):
            return retrieve(Mock(**{"search.return_value": hits}), "ДВС-1", question)["evidence"]

        evidence = answer([wrong, hit])
        self.assertEqual(len(evidence), 1)
        self.assertIn("20–25 Н·м", evidence[0]["excerpt"])
        self.assertIn("патрубок", evidence[0]["excerpt"])
        self.assertFalse(answer([wrong]))
        self.assertFalse(answer([dict(hit, text=operation.replace("болты (7)", "болты (8)"))]))

    def test_disassembly_keeps_five_steps_under_one_citation(self):
        hit = dict(self.evidence[1], kind="text", section="Разборка двигателя", text=(
            "Разборка насоса\n"
            "1. Отвернуть две гайки крепления и снять насос. Вылить масло из насоса,\n"
            "перевернув его крышкой вверх.\n"
            "2. Закрепить корпус насоса и отвернуть пробку перепускного клапана, вы-\n"
            "нуть пружину и плунжер.\n"
            "3. Отвернуть четыре болта крепления крышки масляного насоса, снять\n"
            "крышку и перегородку с прокладками.\n"
            "4. Вынуть валик насоса с ведущей шестерней в сборе из корпуса.\n"
            "5. Вынуть ведомую шестерню насоса из корпуса.\n"
            "После разборки насоса все его детали должны быть промыты."
        ))

        class Search:
            def search(self, engine, query, *, variant, limit):
                return [hit]

        session = ManualSession(root=self.root, search=Search())
        for question in ("Как разобрать масляный насос?", "Порядок разборки масляного насоса?"):
            with self.subTest(question=question):
                answer = session.ask("V1", question, check_id="S1")
                self.assertEqual([source["page"] for source in answer["sources"]], [2])
                self.assertEqual(len(answer["text"].split("\n\n")), 1)
                steps = answer["text"].removeprefix("По руководству. ").splitlines()
                self.assertEqual([step[0] for step in steps], list("12345"))
                self.assertIn("вынуть пружину и плунжер", steps[1])
                self.assertIn("крышки масляного насоса", steps[2])
                self.assertTrue(steps[-1].endswith("[1]"))
                self.assertNotIn("После разборки", answer["text"])

    def test_disassembly_does_not_infer_a_node_or_a_missing_step(self):
        from ai_mechanic.knowledge.search import retrieve

        hit = dict(self.evidence[1], kind="text", section="Разборка двигателя")

        class Search:
            def search(self, engine, query, *, variant, limit):
                return [hit]

        for case in (
            "Разборка насоса\n1. Снять крышку топливного насоса.\nПосле разборки промыть детали.",
            "Разборка насоса\n1. Снять крышку масляного насоса.\n3. Вынуть вал.\nПосле разборки промыть детали.",
            "Разборка насоса\n1. Снять крышку масляного насоса.\n2. Вынуть вал.",
        ):
            with self.subTest(case=case):
                hit["text"] = case
                result = retrieve(Search(), "ДВС-1", "Как разобрать масляный насос?")
                self.assertNotEqual(result["topic"], "disassembly")
