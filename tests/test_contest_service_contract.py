"""Проверки контрактов диагностики, ТОиР и хранения подтверждённой версии."""

import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch


DIAGNOSIS = {
    "vehicle_id": "M333YK47", "session_id": "S-0005", "status": "anomaly",
    "anomaly_type": "F02", "anomaly_title": "Отклонение",
    "anomaly_score": 0.8, "confidence": 0.6, "signal_quality": "good",
    "description": "Выявлены признаки отклонения", "model_version": "mock-v1",
}
SOURCE = {"document_id": "guide", "page": 23, "manual": "Руководство"}
REPLY = {
    "inspection_steps": [dict(SOURCE, text="Проверить датчик", role="work")],
    "sources": [SOURCE],
}
ORDER = {
    "machine_id": "M333YK47", "check_id": "S-0005",
    "fault_code": "F02", "diagnosis": "Причина требует проверки",
    "priority": "medium", "works": ["Проверить датчик"], "parts": [],
    "sources": [SOURCE],
}


def response(body):
    return io.BytesIO(json.dumps(body, ensure_ascii=False).encode("utf-8"))


class ServiceContractTests(unittest.TestCase):
    def test_ml_posts_organizer_pair_and_reads_official_fields(self):
        from ai_mechanic import ml_client

        with patch.object(ml_client, "urlopen", return_value=response(DIAGNOSIS)) as send:
            received = ml_client.fetch_diagnosis("http://mock/", "M333YK47", "S-0005")
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, "http://mock/diagnose")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"vehicle_id": "M333YK47", "session_id": "S-0005"})
        self.assertEqual(received, DIAGNOSIS)

    def test_clients_use_configured_service_addresses(self):
        from ai_mechanic import ml_client, work_order_service

        with patch.dict(os.environ, {
            "MOCK_ML_URL": "http://ml.internal:8001/", "MOCK_TOIR_URL": "http://toir.internal:8002/",
        }):
            with patch.object(ml_client, "urlopen", return_value=response(DIAGNOSIS)) as send_ml:
                ml_client.fetch_diagnosis(None, "M333YK47", "S-0005")
            with patch.object(work_order_service, "urlopen", return_value=response(
                {"status": "created", "ticket_id": "TKT-0001"}
            )) as send_toir:
                work_order_service.submit_order(None, ORDER, "draft-1")
        self.assertEqual(send_ml.call_args.args[0].full_url, "http://ml.internal:8001/diagnose")
        self.assertEqual(send_toir.call_args.args[0].full_url, "http://toir.internal:8002/tickets")

    def test_ml_rejects_wrong_identity_or_invalid_contract(self):
        from ai_mechanic import ml_client

        for fields in ({"vehicle_id": "OTHER"}, {"session_id": "OTHER"}, {"status": "deviation"},
                       {"anomaly_type": "F15"}, {"confidence": True}, {"signal_quality": "noisy"},
                       {"anomaly_score": 1.2}):
            with self.subTest(fields=fields), patch.object(
                ml_client, "urlopen", return_value=response(dict(DIAGNOSIS, **fields))
            ):
                with self.assertRaises(ml_client.MLUnavailable):
                    ml_client.fetch_diagnosis("http://mock", "M333YK47", "S-0005")

    def test_ml_accepts_failed_recording_with_null_fields(self):
        from ai_mechanic import ml_client

        failed = dict(DIAGNOSIS, status="check_failed", signal_quality="bad", anomaly_type=None,
                      anomaly_title=None, anomaly_score=None, confidence=None)
        with patch.object(ml_client, "urlopen", return_value=response(failed)):
            self.assertEqual(ml_client.fetch_diagnosis("http://mock", "M333YK47", "S-0005"), failed)

    def test_toir_posts_only_official_fields_and_returns_ticket_id(self):
        from ai_mechanic import work_order_service

        with patch.object(work_order_service, "urlopen", return_value=response(
            {"status": "created", "ticket_id": "TKT-0001"}
        )) as send:
            number = work_order_service.submit_order("http://mock/", ORDER, "draft-1")
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, "http://mock/tickets")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {
            "vehicle_id": "M333YK47", "session_id": "S-0005", "anomaly_type": "F02",
            "conclusion": "Причина требует проверки", "urgency": "medium",
            "operations": ["Проверить датчик"], "spare_parts": [],
            "references": [{"manual": "Руководство", "pdf_page": 23}],
        })
        self.assertEqual(number, "TKT-0001")

    def test_toir_bad_response_does_not_retry(self):
        from ai_mechanic import work_order_service

        with patch.object(
            work_order_service, "urlopen", return_value=response({"status": "created", "number": "TKT-0001"})
        ) as send:
            with self.assertRaises(work_order_service.OrderServiceError):
                work_order_service.submit_order("http://mock", ORDER, "draft-1")
        send.assert_called_once()


class WorkOrderTests(unittest.TestCase):
    def start(self, sender, verify_works=None):
        from ai_mechanic.work_orders import WorkOrders

        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        owners = lambda machine, check: (machine, check) == ("M333YK47", "S-0005")
        store = WorkOrders(sender, Path(temporary.name, "orders.sqlite"),
                           owns_check=owners, verify_works=verify_works)
        return store

    def test_only_eligible_diagnosis_with_cited_work_produces_draft(self):
        store = self.start(Mock(return_value="TKT-0001"))
        for diagnostic, reply in (
            (dict(DIAGNOSIS, status="normal"), REPLY),
            (dict(DIAGNOSIS, status="check_failed"), REPLY),
            (dict(DIAGNOSIS, signal_quality="bad"), REPLY),
            (dict(DIAGNOSIS, vehicle_id="OTHER"), REPLY),
            (DIAGNOSIS, dict(REPLY, sources=[])),
        ):
            with self.subTest(diagnostic=diagnostic, reply=reply):
                self.assertFalse(store.open_context("M333YK47", "S-0005", diagnostic, reply))
        cid = store.open_context("M333YK47", "S-0005", DIAGNOSIS, REPLY)
        self.assertEqual(store.draft(cid)["payload"]["sources"], [SOURCE])

    def test_edits_recheck_sources_and_confirmation_is_bound_to_revision(self):
        from ai_mechanic.work_orders import DraftChanged

        sender = Mock(return_value="TKT-0001")
        new_source = dict(SOURCE, page=27)
        verify = Mock(return_value=[new_source])
        store = self.start(sender, verify)
        cid = store.open_context("M333YK47", "S-0005", DIAGNOSIS, REPLY)
        draft = store.draft(cid)
        draft_id = draft["draft_id"]
        with self.assertRaises(ValueError):
            store.send(cid, draft_id, draft["revision"], confirmed=False)
        parts = store.update(cid, draft_id, draft["revision"], {"parts": ["Прокладка"]})
        verify.assert_not_called()
        changed = store.update(cid, draft_id, parts["revision"], {"works": ["Проверить масляный фильтр"]})
        verify.assert_called_once_with("M333YK47", "S-0005", ["Проверить масляный фильтр"], [SOURCE])
        self.assertEqual(changed["payload"]["sources"], [new_source])
        with self.assertRaises(DraftChanged):
            store.send(cid, draft_id, parts["revision"], confirmed=True)
        sender.assert_not_called()
        sent = store.send(cid, draft_id, changed["revision"], confirmed=True)
        self.assertEqual(sent["number"], "TKT-0001")
        self.assertEqual(store.send(cid, draft_id, changed["revision"], confirmed=True), sent)
        sender.assert_called_once()
        self.assertEqual(store.history("M333YK47"), [sent])
        new_draft = store.new_from_sent(cid, sent["revision"], lambda machine, check: DIAGNOSIS)
        self.assertNotEqual(new_draft["draft_id"], sent["draft_id"])
        self.assertEqual(new_draft["status"], "draft")
        self.assertEqual(new_draft["payload"], sent["payload"])

    def test_rejected_work_edit_preserves_draft(self):
        store = self.start(Mock(), Mock(side_effect=ValueError("Источник не найден")))
        cid = store.open_context("M333YK47", "S-0005", DIAGNOSIS, REPLY)
        initial = store.draft(cid)
        with self.assertRaisesRegex(ValueError, "Источник не найден"):
            store.update(cid, initial["draft_id"], initial["revision"], {"works": ["Другая работа"]})
        self.assertEqual(store.draft(cid), initial)

    def test_uncertain_submission_is_recorded_and_cannot_be_retried(self):
        sender = Mock(side_effect=TimeoutError("ТОиР недоступна"))
        store = self.start(sender)
        cid = store.open_context("M333YK47", "S-0005", DIAGNOSIS, REPLY)
        draft = store.draft(cid)
        ids = cid, draft["draft_id"], draft["revision"]
        with self.assertRaises(TimeoutError):
            store.send(*ids, confirmed=True)
        self.assertEqual(store.send(*ids, confirmed=True)["status"], "uncertain")
        sender.assert_called_once()
        self.assertEqual(store.history("M333YK47")[0]["status"], "uncertain")
