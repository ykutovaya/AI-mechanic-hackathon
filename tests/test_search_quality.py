"""Проверки отчёта, позволяющие найти этап потери страницы или факта."""

import unittest

from check_e5_pilot import score_case, validate_cases


def source(page, doc="guide"):
    return {"document_id": doc, "page": page}


def passage(page, text, doc="guide"):
    return source(page, doc) | {"text": text, "excerpt": text}


def citation(page, doc="guide"):
    return source(page, doc) | {"url": f"/manuals/{doc}.pdf#page={page}",
                                "page_url": f"/manuals/{doc}/page/{page}"}


CASE = {"id": "torque", "vehicle_id": "V1", "session_id": "S1", "question": "Какой момент?",
        "facts": [{"terms": ["пробку", "7 Н·м"], "sources": [source(12)]}]}
FACT = "Затяните пробку моментом 7 Н·м."
GOOD_REPLY = {"text": FACT + " [1]", "sources": [citation(12)]}


class SearchReportTests(unittest.TestCase):
    def test_ranks_unique_physical_pages_from_primary_query(self):
        repeated = [passage(p, "Условия") for p in (7, 7, 8, 9, 9, 10, 11)]
        result = score_case(CASE, [repeated + [passage(12, FACT)]], [passage(12, FACT)], GOOD_REPLY)
        self.assertEqual(result["search"]["top5"], [source(page) for page in range(7, 12)])
        self.assertEqual(result["failures"], ["page_outside_top5"])
        self.assertTrue(result["answer"]["all_facts"])

    def test_reports_stage_that_lost_fact(self):
        fact = passage(12, FACT)
        generic = passage(12, "Применяйте динамометрический ключ.")
        wrong = {"text": "Затяните пробку моментом 17 Н·м. [1]", "sources": [citation(12)]}
        scenarios = (
            ([[generic]], [generic], wrong, ["fact_missing_in_search"]),
            ([[generic], [fact]], [generic], wrong, ["fact_dropped_by_selection"]),
            ([[fact]], [fact], wrong, ["fact_missing_in_answer"]),
        )
        for queries, excerpts, reply, failures in scenarios:
            with self.subTest(failures=failures):
                result = score_case(CASE, queries, excerpts, reply)
                self.assertEqual(result["failures"], failures)

    def test_requires_fact_to_cite_the_expected_page(self):
        reply = {"text": FACT + " [2]\n\nПрочие условия. [1]",
                 "sources": [citation(12), citation(12, "other")]}
        result = score_case(CASE, [[passage(12, FACT)]], [passage(12, FACT)], reply)
        self.assertEqual(result["failures"], ["fact_missing_in_answer"])

    def test_refusal_requires_defect_inspection_and_no_foreign_source(self):
        case = {**CASE, "facts": [], "insufficient_evidence": True, "diagnose": True}
        reply = {"text": "В руководстве не нашлось процедуры проверки. Рекомендована дефектовка.",
                 "sources": []}
        result = score_case(case, [[]], [], reply)
        self.assertTrue(result["answer"]["refusal_ok"])
        leaked = score_case(case, [[]], [], {**reply, "sources": [citation(12, "other")]})
        self.assertFalse(leaked["answer"]["refusal_ok"])
        self.assertIn("expected_refusal_missing", leaked["failures"])

    def test_rejects_a_foreign_page_even_with_the_same_document_and_model_tag(self):
        case = CASE | {"forbidden_sources": [source(21)]}
        fact = passage(12, FACT)
        other = passage(21, "Схема другого двигателя.")
        clean = score_case(case, [[fact]], [fact], GOOD_REPLY)
        self.assertEqual(clean["failures"], [])
        retrieved = score_case(case, [[fact], [other]], [fact], GOOD_REPLY)
        self.assertIn("inapplicable_source", retrieved["failures"])
        cited = score_case(case, [[fact]], [fact], {**GOOD_REPLY, "sources": [citation(12), citation(21)]})
        self.assertIn("inapplicable_source", cited["failures"])

    def test_rejects_ambiguous_or_malformed_forbidden_pages(self):
        machines = {"V1": {"history": [{"check_id": "S1"}]}}
        for forbidden in ([source(0)], [source(12)], "guide", [{}]):
            with self.subTest(forbidden=forbidden), self.assertRaises(ValueError):
                validate_cases([CASE | {"forbidden_sources": forbidden}], machines)

    def test_validates_exact_vehicle_and_session_pairs(self):
        machines = {
            "V1": {"engine_model": "A", "history": [{"check_id": "S1"}]},
            "V2": {"engine_model": "A", "history": [{"check_id": "S2"}]},
        }
        second = {**CASE, "id": "other", "vehicle_id": "V2", "session_id": "S2"}
        self.assertIsNone(validate_cases([CASE, second], machines))
        with self.assertRaisesRegex(ValueError, "S1"):
            validate_cases([second | {"session_id": "S1"}], machines)


if __name__ == "__main__":
    unittest.main()
