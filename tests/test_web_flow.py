"""Проверки маршрутов механика и доступа к физической странице PDF."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pymupdf

from ai_mechanic import web


@contextmanager
def running(server):
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class Session:
    def __init__(self, **kwargs):
        self.registry = [{"document_id": "guide", "path": "guide.pdf", "title": "Руководство",
                          "edition": "ЗМЗ-5234.10. РК"}]
        self.machines = [{"machine_id": "vehicle", "vehicle_id": "vehicle", "label": "Машина",
                          "engine_model": "ЗМЗ-5234.10",
                          "history": [{"check_id": check} for check in ("good", "normal", "bad", "failed")]}]
        self.topic = None
        self.selection = None

    def ask(self, machine_id, question, *, check_id=None, diagnosis=None):
        reply = {"engine_model": self.machines[0]["engine_model"], "sources": [], "warnings": []}
        if diagnosis and (diagnosis["signal_quality"] == "bad" or diagnosis["status"] == "check_failed"):
            return reply | {"text": "Повторите запись."}
        return reply | {
            "text": "Проверьте масляный насос.",
            "sources": [{"document_id": "guide", "page": 2, "title": "Руководство",
                         "edition": "2025", "manual": "ЗМЗ-5234.10. РК"}],
            "inspection_steps": [{"document_id": "guide", "page": 2, "role": "work",
                                  "text": "Проверить масляный насос."}],
        }


class WebFixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        with pymupdf.open() as document:
            document.new_page()
            document.new_page()
            document.save(self.root / "guide.pdf")
        self.ml_calls = []
        self.ticket_calls = []
        self.ml_offline = False
        fixture = self

        class Services(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/diagnose":
                    fixture.ml_calls.append(body)
                    if fixture.ml_offline:
                        self.send_error(503)
                        return
                    check = body["session_id"]
                    status = {"normal": "normal", "failed": "check_failed"}.get(check, "anomaly")
                    result = body | {"status": status, "signal_quality": "bad" if check in ("bad", "failed") else "good",
                                     "anomaly_type": "F02" if status == "anomaly" else None,
                                     "anomaly_title": "Низкое давление масла" if status == "anomaly" else None,
                                     "description": "Тест", "anomaly_score": 0.8,
                                     "confidence": 0.9, "model_version": "test"}
                    code = 200
                elif self.path == "/tickets":
                    fixture.ticket_calls.append(body)
                    result = {"ticket_id": "TKT-0018", "status": "created"}
                    code = 201
                else:
                    self.send_error(404)
                    return
                data = json.dumps(result).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        services = self.enterContext(running(ThreadingHTTPServer(("127.0.0.1", 0), Services)))
        self.enterContext(patch.object(web, "ManualSession", Session))
        self.url = self.enterContext(running(web.make_server(
            ("127.0.0.1", 0), root=self.root, search=object(), ml_url=services, toir_url=services,
            orders_db=self.root / "orders.sqlite")))

    def request(self, path, data=None):
        request = Request(self.url + path, data=json.dumps(data).encode() if data is not None else None,
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def diagnose(self, check="good"):
        return self.request("/api/diagnose", {"machine_id": "vehicle", "check_id": check})

    def assert_http_error(self, code, path, data):
        with self.assertRaises(HTTPError) as raised:
            self.request(path, data)
        self.assertEqual(raised.exception.code, code)
        return json.load(raised.exception)


class WebFlowTests(WebFixture):

    def test_machine_picker_shows_no_standalone_variant(self):
        with urlopen(self.url + "/") as response:
            html = response.read().decode("utf-8")
        picker = html.split("<aside", 1)[1].split("</aside>", 1)[0]
        self.assertIn('for="machine"', picker)
        self.assertIn('for="check"', picker)
        self.assertNotIn("Исполнение", picker)

    def test_question_and_source_do_not_trigger_diagnosis(self):
        self.assertEqual(self.request("/api/machines")["machines"][0]["engine_model"], "ЗМЗ-5234.10")
        car = {"machine_id": "vehicle", "check_id": "good"}
        reply = self.request("/api/ask", car | {"question": "Что проверить?"})
        self.assertEqual(self.ml_calls, [])
        self.assertEqual(reply["sources"][0]["url"], "/manuals/guide.pdf#page=2")
        with urlopen(self.url + reply["sources"][0]["page_url"]) as response:
            self.assertIn("Страница 2 в PDF", response.read().decode())
        with urlopen(self.url + "/manuals/guide/page/2.png") as response:
            self.assertTrue(response.read().startswith(b"\x89PNG"))
        request = Request(self.url + "/manuals/guide.pdf", headers={"Range": "bytes=0-4"})
        with urlopen(request) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"%PDF-")
        self.diagnose()
        self.request("/api/ask", car | {"question": "Что проверить?"})
        self.assertEqual(self.ml_calls, [{"vehicle_id": "vehicle", "session_id": "good"}])

    def test_normal_failed_bad_and_unavailable_diagnosis(self):
        normal = self.diagnose("normal")
        self.assertFalse(normal["context_id"])
        self.assertIn("отклонений не обнаружено", normal["text"])
        for check in ("failed", "bad"):
            reply = self.diagnose(check)
            self.assertFalse(reply["context_id"])
            self.assertEqual(reply["text"], "Повторите запись.")
            self.assertEqual(reply["sources"], [])
        self.assert_http_error(400, "/api/diagnose", {"machine_id": "vehicle", "check_id": "foreign"})
        self.ml_offline = True
        self.assert_http_error(502, "/api/diagnose", {"machine_id": "vehicle", "check_id": "good"})
        self.assertEqual(self.ticket_calls, [])


if __name__ == "__main__":
    unittest.main()
