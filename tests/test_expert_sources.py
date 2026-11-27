"""Приёмка связей экспертной таблицы с источником."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from ai_mechanic.knowledge.qdrant_chunks import _chunk


class ExpertSourcesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.workbook = self.directory / "expert.xlsx"
        self.codes = self.directory / "faults.json"
        self.codes.write_text(json.dumps([{"code": "F02", "title": "Давление масла"}]))
        self.vehicles = self.directory / "vehicles.json"
        self.vehicles.write_text(json.dumps({"vehicles": [{"vehicle_id": "A1", "engine_model": "ЯМЗ-536"}]}))
        self.registry = [{"document_id": "yamz-536-rk-2013", "title": "536.01.02 РК",
                          "edition": "2013", "path": "manual.pdf", "engine_models": ["ЯМЗ-536"],
                          "source_sha256": "a" * 64, "page_count": 3}]
        self.source = "Измерить давление масла в канале двигателя при температуре 80 °С."
        self.page = dict(self.registry[0], page=2, text=self.source, section="Проверка")
        self.chunk = _chunk(self.page, "Проверка", ["ЯМЗ-536"], None,
                            [dict(page=2, text=self.source)], "text")
        self.headers = ["Код", "Название неисправности", "vehicle_id", "Модель двигателя", "Исполнение",
                        "Документ", "Страницы PDF", "Выдержка", "Статус", "Примечание"]
        self.row = ["F02", "Давление масла", "A1", "ЯМЗ-536", "", "536.01.02 РК, 2013",
                    "PDF 2", self.source, "Подтверждено", ""]

    def run_import(self, rows, chunks=None, pages=None):
        book = Workbook()
        book.active.append(self.headers)
        for row in rows:
            book.active.append(row)
        book.save(self.workbook)
        book.close()
        name = "ai_mechanic.knowledge.expert_sources"
        self.assertIsNotNone(importlib.util.find_spec(name), "Импорт подтвержденных выдержек отсутствует")
        from ai_mechanic.knowledge.expert_sources import apply_expert_sources

        return apply_expert_sources([self.chunk] if chunks is None else chunks, workbook=self.workbook,
                                    registry=self.registry, fault_types=self.codes, vehicles=self.vehicles,
                                    pages={"yamz-536-rk-2013": [self.page] if pages is None else pages})

    def edited(self, **fields):
        row = list(self.row)
        for field, value in fields.items():
            row[self.headers.index(field)] = value
        return row

    def test_confirmed_paraphrases_create_only_navigation_to_relevant_source(self):
        row = self.edited(Выдержка="Манометром проверяют давление масла в канале.")
        other = dict(self.chunk, id="другой источник", page_parts=[dict(page=2, text="Кожух и кронштейн генератора.")])
        chunks, report = self.run_import([row, row], [self.chunk, other])
        self.assertEqual(report["counts"], {"bound": 0, "review": 2, "not_found": 0})
        self.assertEqual(report["navigation"]["routed_only"], 2)
        self.assertEqual(report["navigation"]["available"], 2)
        for item in report["rows"]:
            self.assertEqual(item["routes"], [{"chunk_id": self.chunk["id"], "page": 2}])
        route, = chunks[0]["fault_routes"]
        self.assertEqual(route, {"code": "F02", "applicability": "ЯМЗ-536|all",
                                 "source_pages": [2], "rows": [2, 3]})
        self.assertNotIn("fault_bindings", chunks[0])
        self.assertNotIn("fault_routes", chunks[1])
        self.assertNotIn("fault_routes", self.chunk)

    def test_rebuilding_drops_old_navigation_and_keeps_ineligible_rows_out(self):
        restricted = dict(self.chunk, applicability=["ЯМЗ-536|А"], fault_routes=[{"code": "F03"}])
        row = self.edited(Выдержка="Манометром проверяют давление масла в канале.")
        tests = [self.row, self.edited(Исполнение="А", Статус="Не найдено"),
                 self.edited(Исполнение="А", vehicle_id="неизвестная"),
                 self.edited(Исполнение="А", Примечание="Не использовать как норматив"),
                 self.edited(Исполнение="А", Документ="536.01.02 РК, 2014"), row]
        chunks, report = self.run_import(tests, [restricted])
        self.assertEqual(report["navigation"]["available"], 0)
        self.assertNotIn("fault_routes", chunks[0])
        self.assertTrue(all("routes" not in r for r in report["rows"]))

    def test_a_bad_explicit_locator_or_disputed_number_does_not_become_navigation(self):
        self.headers.append("Место в источнике")
        row = self.edited(Выдержка="Проверить давление масла при температуре 120 °С.")
        chunks, report = self.run_import([row + ["PDF 2: абзац «Измерить давление масла»"],
                                          row + ["PDF 2: пункт 99"]])
        self.assertEqual(report["counts"]["review"], 2)
        self.assertEqual(report["navigation"]["available"], 0)
        self.assertNotIn("fault_routes", chunks[0])

    def test_navigation_can_offer_only_a_pdf_bookmark_without_scoped_text(self):
        row = self.edited(Выдержка="Манометром проверяют давление масла в канале.")
        chunks, report = self.run_import([row], [])
        self.assertEqual(chunks, [])
        self.assertEqual(report["navigation"]["reading_only"], 1)
        self.assertEqual(report["rows"][0]["reading_reference"], {
            "document_id": "yamz-536-rk-2013", "source_pages": [2], "applicability": "ЯМЗ-536|all"})
        self.assertNotIn("chunk_id", report["rows"][0])
        self.assertEqual(report["rows"][0]["result"], "review")

    def test_navigation_does_not_substitute_a_range_for_a_missing_passage(self):
        pages = [dict(self.page, page=i) for i in range(1, 10)]
        self.registry[0]["page_count"] = len(pages)
        row = self.edited(**{"Страницы PDF": "1–9", "Выдержка": "Давление масла проверяют манометром."})
        chunks, report = self.run_import([row], [], pages)
        self.assertEqual(report["navigation"]["available"], 0)
        self.assertNotIn("reading_reference", report["rows"][0])

    def test_navigation_does_not_confuse_tappets_and_chain_tensioners(self):
        row = self.edited(Выдержка="При заедании гидротолкателя клапана заменить гидротолкатель.")
        text = "При заедании гидронатяжителя привода клапанов заменить гидронатяжитель."
        page = dict(self.page, text=text)
        chunk = _chunk(page, "Клапанный механизм", ["ЯМЗ-536"], None, [dict(page=2,text=text)], "text")
        chunks, report = self.run_import([row], [chunk], [page])
        self.assertNotIn("fault_routes", chunks[0])
        self.assertEqual(report["navigation"]["reading_only"], 1)

    def test_exact_binds_only_to_one_pair_and_keeps_source(self):
        chunks, report = self.run_import([self.row, self.row, self.edited(Статус="Не найдено")])
        self.assertEqual({"bound": 2, "review": 0, "not_found": 1}, report["counts"])
        self.assertEqual([2, 3], chunks[0]["fault_bindings"][0]["rows"])
        self.assertEqual("ЯМЗ-536|all", chunks[0]["fault_bindings"][0]["applicability"])
        self.assertEqual(self.chunk["text"], chunks[0]["text"])
        self.assertNotIn("fault_bindings", self.chunk)
        self.assertEqual("not_found", report["rows"][-1]["result"])

    def test_rejects_unverified_rows_and_never_uses_partial_matches(self):
        cases = [{"Код": ""}, {"Код": "F99"}, {"Модель двигателя": "ЯМЗ-236М2"},
                 {"vehicle_id": "другая"}, {"Документ": "536.01.02 РК, 2015"},
                 {"Страницы PDF": "~2"}, {"Страницы PDF": "PDF 2, 4"},
                 {"Статус": "Требует сверки"}, {"Выдержка": self.source + " Давление выше нормы."},
                 {"Примечание": "Противоречие внутри издания по значению норматива."},
                 {"Страницы PDF": "2", "Выдержка": self.source.replace("80", "8,0")}]
        chunks, report = self.run_import([self.edited(**case) for case in cases])
        self.assertEqual(len(cases), report["counts"]["review"])
        self.assertNotIn("fault_bindings", chunks[0])

    def test_duplicate_locations_and_variant_only_attachment(self):
        repeated = dict(self.chunk, id="другой", section="Второе место")
        _, report = self.run_import([self.row], [self.chunk, repeated])
        self.assertEqual(1, report["counts"]["review"])
        restricted = dict(self.chunk, applicability=["ЯМЗ-536|А"])
        _, report = self.run_import([self.row], [restricted])
        self.assertEqual(1, report["counts"]["review"])
        chunks, report = self.run_import([self.edited(Исполнение="А")], [restricted])
        self.assertEqual("ЯМЗ-536|А", chunks[0]["fault_bindings"][0]["applicability"])

    def test_new_passage_requires_explicit_scope_without_sharing_applicability(self):
        quote = "На двигателе ЯМЗ-536 проверить состояние корпуса насоса и его креплений."
        page = dict(self.page, text=quote + "\nНа двигателе ЯМЗ-536-10 используется другой порядок.")
        chunks, report = self.run_import([self.edited(Выдержка=quote)], [], [page])
        self.assertEqual(1, report["counts"]["bound"])
        self.assertEqual(quote.rstrip("."), chunks[0]["text"].rstrip("."))
        self.assertEqual(["ЯМЗ-536|all"], chunks[0]["applicability"])
        self.assertEqual([2], chunks[0]["source_pages"])
        for source in [dict(page, text=self.source), dict(page, text="", ocr={"status":"pending","text":quote}),
                       dict(page, source_sha256="другая")]:
            _, report = self.run_import([self.edited(Выдержка=quote)], [], [source])
            self.assertEqual(1, report["counts"]["review"])


    def test_foreign_designation_and_wrong_manual_kind_override_year_and_filename(self):
        labels = [
            "536.01.02 РЭ, 2013",
            "536.01.02 РК, 2013; и ещё одно неизвестное руководство",
            "536.01.02 РК, 2013 и 555.00.01 РК",
            "555.00.01 РК, 2013, файл manual.pdf",
            "РК 536.01.02, издание 2013; руководство по эксплуатации",
        ]
        _, report = self.run_import([self.edited(Документ=s) for s in labels])
        self.assertEqual(len(labels), report["counts"]["review"])

    def test_qualifications_and_units_cannot_be_dropped(self):
        cases = [
            ("Не устанавливать насос на этот двигатель.", "устанавливать насос на этот двигатель"),
            ("Гайки крепления коллектора    50 – 55 (5,0 – 5,5)", "Гайки крепления коллектора 50 – 55 (5,0 – 5,5)"),
            ("Предельный угол поворота вала 2°40′", "Предельный угол поворота вала 2°40"),
        ]
        for body, excerpt in cases:
            with self.subTest(excerpt=excerpt):
                page = dict(self.page, text=body)
                chunk = _chunk(page, "Таблица", ["ЯМЗ-536"], None, [dict(page=2,text=body)], "text")
                _, report = self.run_import([self.edited(Выдержка=excerpt)], [chunk], [page])
                self.assertEqual(1, report["counts"]["review"])

    def test_procedure_retains_physical_pages_of_both_steps(self):
        first = "Проверить уровень масла."
        second = "Установить детали в обратном порядке."
        pages = [dict(self.page, text=first), dict(self.page, page=3, text=second)]
        chunk = _chunk(self.page, "Проверка", ["ЯМЗ-536"], None,
                       [dict(page=2,text=first), dict(page=3,text=second)], "procedure")
        chunks, report = self.run_import([self.edited(**{"Страницы PDF":"2–3","Выдержка":first+" "+second})],
                                         [chunk], pages)
        self.assertEqual(1, report["counts"]["bound"])
        self.assertEqual([2, 3], chunks[0]["fault_bindings"][0]["source_pages"])

    def test_non_consecutive_pages_cannot_form_one_quote(self):
        a, b = "Проверить все детали", "перед сборкой двигателя."
        pages = [dict(self.page, page=1,text=a), dict(self.page,page=3,text=b)]
        chunk = _chunk(self.page, "Проверка", ["ЯМЗ-536"], None,
                       [dict(page=1,text=a), dict(page=3,text=b)], "procedure")
        _, report = self.run_import([self.edited(**{"Страницы PDF":"1,3","Выдержка":a+" "+b})],
                                    [chunk], pages)
        self.assertEqual(1, report["counts"]["review"])


    def test_exact_page_prefixes_and_russian_words_without_changing_units(self):
        body = "Щуп должен входить свободно. Напряжение 30 мВ."
        p = dict(self.page, text=body)
        c = _chunk(p, "Проверка", ["ЯМЗ-536"], None, [dict(page=2,text=body)], "text")
        for prefix in ("стр. ", "страница PDF ", "страницы PDF "):
            with self.subTest(prefix=prefix):
                records = [self.edited(**{"Страницы PDF":prefix+"2", "Выдержка":body.replace("Щуп","щуп")}),
                           self.edited(**{"Страницы PDF":prefix+"2", "Выдержка":body.replace("мВ","МВ")})]
                _, report = self.run_import(records, [c], [p])
                self.assertEqual([1, 1], [report["counts"]["bound"],report["counts"]["review"]])

    def test_page_column_accepts_numbered_point_and_physical_page(self):
        self.registry[0]["page_count"] = 217
        adjacent = "Снять контрольную крышку и заменить прокладку корпуса фильтра."
        body = f"11.6 {self.source}\n11.7 {adjacent}"
        page = dict(self.page, page=216, page_count=217, text=body)
        chunk = _chunk(page, "Проверка", ["ЯМЗ-536"], None,
                       [dict(page=216, text=body)], "text")
        reference = "п. 11.6 на стр. 216"
        rows = [self.edited(**{"Страницы PDF": value}) for value in (
            reference, "п. 11.7 на стр. 216", "п. 11.6 на стр. 218", "п. 11.6 без страницы 216"
        )]
        chunks, report = self.run_import(rows, [chunk], [page])
        confirmed, wrong_point, outside_pdf, malformed = report["rows"]
        self.assertEqual((confirmed["result"], confirmed.get("source_pages")), ("bound", [216]))
        self.assertEqual(wrong_point["result"], "review")
        self.assertEqual(outside_pdf["result"], "review")
        self.assertEqual(malformed["result"], "review")
        by_id = {item["id"]: item for item in chunks}
        point = by_id[confirmed["chunk_id"]]
        self.assertNotEqual(point["id"], chunk["id"])
        self.assertEqual(point["source_pages"], [216])
        self.assertEqual([part["page"] for part in point["page_parts"]], [216])
        binding, = point["fault_bindings"]
        for value in (point["text"], point["embedding_text"], point["page_parts"][0]["text"], binding["excerpt"]):
            self.assertIn(self.source, value)
            self.assertNotIn(adjacent, value)
        full_page = by_id[chunk["id"]]
        self.assertIn(adjacent, full_page["text"])
        self.assertFalse(full_page.get("fault_bindings"))

    def test_page_column_limits_search_to_the_numbered_table_row(self):
        self.registry[0]["page_count"] = 217
        table = {"rows": [
            ["Причины неисправности", "Способ устранения"],
            ["11.6 Шум в районе турбокомпрессора", "Проверить крепление соединительных рукавов."],
            ["11.7 Неисправны форсунки", "Выполнить тест форсунок на стенде."],
        ]}
        body = "\n".join(" | ".join(row) for row in table["rows"])
        page = dict(self.page, page=216, page_count=217, text=body)
        chunk = _chunk(page, "Таблица 3", ["ЯМЗ-536"], None,
                       [dict(page=216, text=body, tables=[table])], "table")
        records = [self.edited(**{"Страницы PDF": "п. 11.6 на стр. 216", "Выдержка": text})
                   for _, text in table["rows"][1:]]
        chunks, report = self.run_import(records, [chunk], [page])
        correct, wrong = report["rows"]
        self.assertEqual((correct["result"], correct.get("source_pages")), ("bound", [216]))
        self.assertEqual(wrong["result"], "review")
        by_id = {item["id"]: item for item in chunks}
        point = by_id[correct["chunk_id"]]
        self.assertNotEqual(point["id"], chunk["id"])
        self.assertEqual(point["source_pages"], [216])
        binding, = point["fault_bindings"]
        values = (point["text"], point["embedding_text"], json.dumps(point["page_parts"], ensure_ascii=False),
                  binding["excerpt"])
        for value in values:
            self.assertIn("Проверить крепление соединительных рукавов.", value)
            self.assertNotIn("Выполнить тест форсунок на стенде.", value)
        full_page = by_id[chunk["id"]]
        self.assertIn("Выполнить тест форсунок на стенде.", full_page["text"])
        self.assertFalse(full_page.get("fault_bindings"))

    def test_paraphrase_with_numbered_step_in_section_and_no_variant_column(self):
        self.headers.remove("Исполнение")
        self.row.pop(4)
        self.headers.append("Раздел")
        self.row.append("")
        body = "4. Отключить насос.\n5. " + self.source + "\n6. Снять манометр."
        page = dict(self.page, text=body)
        chunk = dict(self.chunk, page_parts=[dict(page=2, text=body)])
        row = self.edited(Раздел="Проверка (пункт 5)",
                          Выдержка="При температуре 80 °С проверить давление масла манометром.")
        chunks, report = self.run_import([row], [chunk], [page])
        self.assertEqual(report["counts"]["bound"], 1)
        self.assertEqual(report["rows"][0]["match_method"], "locator")
        point = next(c for c in chunks if c["id"] == report["rows"][0]["chunk_id"])
        link, = point["fault_bindings"]
        self.assertEqual(link["source_pages"], [2])
        self.assertIn(self.source, link["excerpt"])
        self.assertNotIn("Снять манометр", link["excerpt"])

    def test_paragraph_locator_keeps_original_range_and_reports_conflicts(self):
        self.headers.append("Место в источнике")
        body = ("Зазор между деталями при проверке на холодном двигателе должен быть "
                "от 0,25 до 0,30 мм.\n\nДругой узел. Норма 0,40 мм.")
        page = dict(self.page, text=body)
        chunk = dict(self.chunk, page_parts=[dict(page=2, text=body)])
        confirmed = self.edited(Выдержка="Зазор на холодном двигателе — 0,25–0,30 мм.")
        contradicted = [self.edited(Выдержка=value) for value in (
            "Зазор 0,25–0,40 мм.", "Зазор 0,25–0,30 см.",
            "Зазор на прогретом двигателе 0,25–0,30 мм.", "Зазор не менее 0,30 мм.")]
        marker = "PDF 2: абзац «Зазор между деталями»"
        chunks, report = self.run_import([row + [marker] for row in [confirmed, *contradicted]], [chunk], [page])
        self.assertEqual(report["counts"], {"bound": 1, "review": 4, "not_found": 0})
        self.assertEqual(chunks[0]["fault_bindings"][0]["excerpt"], body.split("\n\n")[0])
        self.assertTrue(all(row["candidates"] for row in report["rows"][1:]))

    def test_table_locator_checks_only_the_named_cell_and_keeps_source_row(self):
        self.headers.append("Место в источнике")
        table = {"rows": [["Узел", "Холодный двигатель, кПа", "Горячий двигатель, кПа"],
                          ["Масляный насос", "300", "200"], ["Датчик", "200", "70"]]}
        body = "Узел | Холодный двигатель, кПа | Горячий двигатель, кПа\nМасляный насос | 300 | 200"
        page = dict(self.page, text=body)
        chunk = dict(self.chunk, page_parts=[dict(page=2, text=body, tables=[table])])
        marker = "PDF 2: строка «Масляный насос» | графа «Холодный двигатель, кПа»"
        records = [self.edited(Выдержка=excerpt) + [marker] for excerpt in
                   ("При проверке насоса значение 300 кПа.", "При проверке насоса значение 200 кПа.")]
        chunks, report = self.run_import(records, [chunk], [page])
        self.assertEqual(report["counts"]["bound"], 1)
        self.assertEqual(report["counts"]["review"], 1)
        self.assertIn("Масляный насос | 300 | 200", chunks[0]["fault_bindings"][0]["excerpt"])

    def test_locator_requires_unique_applicable_source_and_flags_missing_page(self):
        self.headers.append("Место в источнике")
        body = "5. " + self.source
        page = dict(self.page, text=body)
        chunk = dict(self.chunk, page_parts=[dict(page=2, text=body)])
        row = self.edited(Выдержка="Проверить давление масла при 80 °С.") + ["PDF 2: пункт 5"]
        for chunks, entry in (([chunk, dict(chunk, id="другой")], row),
                              ([dict(chunk, applicability=["ЯМЗ-536|А"])], row),
                              ([chunk], row[:-1] + ["PDF 3: пункт 5"]),
                              ([chunk], row[:-1] + ["PDF 2: пункт 7"])):
            with self.subTest(entry=entry, scope=chunks[0]["applicability"]):
                result, report = self.run_import([entry], chunks, [page])
                self.assertEqual(report["counts"]["review"], 1)
                self.assertFalse(any("fault_bindings" in chunk for chunk in result))
                if chunks[0]["applicability"] == ["ЯМЗ-536|all"]:
                    self.assertTrue(report["rows"][0]["candidates"])

    def test_paraphrase_does_not_reverse_source_prohibition(self):
        self.headers.append("Место в источнике")
        body = "5. Не устанавливать уплотнение старого образца на двигатель."
        page = dict(self.page, text=body)
        chunk = dict(self.chunk, page_parts=[dict(page=2, text=body)])
        claims = ["Поставить уплотнение старого образца.", "Установите старое уплотнение.",
                  "Запрещено устанавливать уплотнение старого образца.",
                  "Заменить турбокомпрессор."]
        rows = [self.edited(Выдержка=claim) + ["PDF 2: пункт 5"] for claim in claims]
        _, report = self.run_import(rows, [chunk], [page])
        self.assertEqual([row["result"] for row in report["rows"]], ["review", "review", "bound", "review"])

