"""Проверка указателя экспертных строк через обычную сессию чата."""

import unittest
from unittest.mock import Mock

from ai_mechanic.ask import ManualSession
import test_manual_session as fixtures


class ExpertCatalogChatTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ManualSessionTest('test_explicit_ml_fetch_and_reuse')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.backend = Mock()
        self.source = dict(self.fixture.manual, row=24, source_pages=[1, 2],
                           section='Проверка насоса', level='reading', applicability=['ДВС-1|all'])
        self.backend.reference_entries.return_value = [self.source]
        self.session = ManualSession(root=self.fixture.root, search=self.backend, retriever=self.fixture.retrieve)

    def test_mechanic_opens_review_pages_without_using_them_as_repair_evidence(self):
        reply = self.session.ask('V1', 'Покажи источники по этой неисправности', check_id='S1',
                                 diagnosis=self.fixture.diagnostic)
        rows = reply.get('reading_references', [])
        self.assertEqual([(24, [1, 2])], [(r['row'], [s['page'] for s in r['sources']]) for r in rows])
        self.assertTrue(rows[0]['sources'][1]['url'].endswith('#page=2'))
        self.assertEqual(rows[0]['level'], 'reading')
        self.assertEqual(reply['sources'], [])
        self.assertEqual(reply['inspection_steps'], [])
        self.backend.reference_entries.assert_called_once_with('ДВС-1', 'F02', variant=None)

    def test_unavailable_catalog_is_reported_as_search_failure(self):
        from ai_mechanic.knowledge.search import SearchUnavailable
        self.backend.reference_entries.side_effect = ConnectionError('connection closed')
        with self.assertRaises(SearchUnavailable):
            self.session.ask('V1', 'Покажи источники по этой неисправности', check_id='S1',
                             diagnosis=self.fixture.diagnostic)

    def test_catalog_does_not_mix_models_or_changed_files(self):
        self.backend.reference_entries.return_value = [
            dict(self.source, applicability=['ДВС-1|морской']),
            dict(self.source, source_sha256='0' * 64),
        ]
        result = self.session.ask('V1', 'Покажи источники по этой неисправности', check_id='S1',
                                  diagnosis=self.fixture.diagnostic)
        self.assertEqual(result.get('reading_references', []), [])
        self.assertEqual(result['sources'], [])

    def test_request_does_not_trigger_ml_or_open_a_catalog_before_diagnosis(self):
        reply = self.session.ask('V1', 'Покажи источники по этой неисправности', check_id='S1')
        self.assertIn('диагностик', reply['text'].casefold())
        self.backend.reference_entries.assert_not_called()
        self.assertEqual(self.fixture.ml_calls, [])
