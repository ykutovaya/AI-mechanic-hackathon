"""Проверки подтверждения конкретной версии заявки через веб-чат."""

from test_web_flow import WebFixture


class WorkOrderWebTests(WebFixture):
    def test_edit_and_confirm_exact_revision(self):
        reply = self.diagnose()
        context = {"context_id": reply["context_id"]}
        self.assertTrue(context["context_id"])
        draft = self.request("/api/orders/draft", context)
        original = context | {key: draft[key] for key in ("draft_id", "revision")}
        edited = self.request("/api/orders/update", original | {"changes": {
            "priority": "high", "parts": ["Прокладка"],
        }})
        self.assertNotEqual(edited["revision"], original["revision"])
        self.assert_http_error(409, "/api/orders/send", original | {"confirmed": True})
        current = original | {"revision": edited["revision"]}
        self.assert_http_error(400, "/api/orders/send", current | {"confirmed": "true"})
        self.assertEqual(self.ticket_calls, [])
        sent = self.request("/api/orders/send", current | {"confirmed": True})
        self.assertEqual(sent["number"], "TKT-0018")
        self.assertEqual(len(self.ticket_calls), 1)
        payload = self.ticket_calls[0]
        self.assertEqual(set(payload), {"vehicle_id", "session_id", "anomaly_type", "conclusion",
                                        "urgency", "operations", "references", "spare_parts"})
        self.assertEqual((payload["vehicle_id"], payload["session_id"], payload["anomaly_type"]),
                         ("vehicle", "good", "F02"))
        self.assertEqual((payload["urgency"], payload["spare_parts"]), ("high", ["Прокладка"]))
        self.assertEqual(payload["references"], [{"manual": "ЗМЗ-5234.10. РК", "pdf_page": 2}])
        self.assertTrue(payload["operations"])
        self.request("/api/orders/new", context | {"revision": sent["revision"]})
        self.assertEqual(len(self.ml_calls), 1)

    def test_accepts_mechanics_work_and_sends_it_in_confirmed_revision(self):
        context = {"context_id": self.diagnose()["context_id"]}
        draft = self.request("/api/orders/draft", context)
        original = context | {key: draft[key] for key in ("draft_id", "revision")}
        work = "Выполнить дефектовку стука."
        works = [work]
        update = self.request("/api/orders/update", original | {"changes": {"works": works}})
        self.assertEqual(update["payload"]["works"], works)
        self.assertEqual(update["manual_works"], [work])
        self.assertEqual(update["payload"]["sources"], draft["payload"]["sources"])
        self.assertGreater(update["revision"], draft["revision"])
        self.assertEqual(self.request("/api/orders/draft", context), update)
        self.assert_http_error(409, "/api/orders/send", original | {"confirmed": True})
        self.assertEqual(self.ticket_calls, [])

        sent = self.request("/api/orders/send", original | {"revision": update["revision"], "confirmed": True})
        self.assertEqual(sent["number"], "TKT-0018")
        self.assertEqual(sent["manual_works"], [work])
        self.assertEqual(self.ticket_calls[0]["operations"], works)
        self.assertEqual(self.ticket_calls[0]["references"], [{"manual": "ЗМЗ-5234.10. РК", "pdf_page": 2}])
        self.assertEqual(len(self.ml_calls), 1)

    def test_supported_edit_rechecks_source_and_advances_revision(self):
        context = {"context_id": self.diagnose()["context_id"]}
        draft = self.request("/api/orders/draft", context)
        selection = context | {key: draft[key] for key in ("draft_id", "revision")}
        work = "Проверить масляный насос"
        updated = self.request("/api/orders/update", selection | {"changes": {"works": [work]}})
        self.assertGreater(updated["revision"], draft["revision"])
        self.assertEqual(updated["payload"]["works"], [work])
        self.assertEqual(updated["payload"]["sources"], [
            {"document_id": "guide", "page": 2, "manual": "ЗМЗ-5234.10. РК"},
        ])
        self.assertEqual(len(self.ml_calls), 1)
