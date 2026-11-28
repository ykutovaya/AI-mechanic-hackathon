"""Веб-чат механика. Запуск из корня проекта: python -m ai_mechanic.web."""

import argparse
from dataclasses import dataclass, replace
from functools import partial
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from threading import Lock
from urllib.parse import quote, unquote, urlsplit

import pymupdf

from .ask import ManualSession, _validated_result
from .knowledge.clutch_yamz236 import (
    CLUTCH_CHECK_ID, CLUTCH_TOPIC, clutch_observation_report, clutch_order_basis, clutch_overview_question,
)
from .knowledge.f06_clutch import clutch_observation_question
from .knowledge.diagnostic_works import CHAIN_INSPECTION_TOPIC, chain_observation_question
from .knowledge.generator_bearing import GENERATOR_BEARING_TOPIC, generator_observation_question
from .knowledge.generator_gearbox import gearbox_observation_question, gearbox_overview_question
from .knowledge.oil_observation_state import OilObservations
from .knowledge.valve_belt_checks import BELT_TOPIC, belt_drive_in, belt_observation_question
from .knowledge.yamz236_oil import (
    OIL_MEASUREMENT_CONFIRMED_TOPIC, OIL_MEASUREMENT_TOPIC, OIL_WORK_SOURCES, oil_observation_question,
)
from .knowledge.yamz536_oil import (
    YAMZ536_OIL_CONFIRMED_TOPIC, YAMZ536_OIL_NORM_SOURCE, YAMZ536_OIL_TOPIC, yamz536_oil_observation_question,
)
from .knowledge.zmz40904_oil import ZMZ40904_OIL_CONFIRMED_TOPIC, ZMZ40904_OIL_TOPIC, zmz40904_oil_observation_question
from .knowledge.zmz5234_oil_knock import (
    ZMZ5234_CHECK_ID, ZMZ5234_OIL_KNOCK_TOPIC, ZMZ5234_REPAIR_CONFIRMED_TOPIC, ZMZ5234_REPAIR_SOURCE,
    ZMZ5234_REPAIR_WORK, zmz5234_oil_observation_question,
)
from .knowledge.search import SearchUnavailable, add_search_arguments, configured_search
from .ml_client import MLUnavailable, fetch_diagnosis
from .work_order_service import OrderServiceError, submit_order
from .work_orders import DraftChanged, WorkOrders
from .yandex_gpt import YandexGPT, order_reference_answer


STATIC = Path(__file__).with_name("static")
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/static/orders.js": ("orders.js", "text/javascript; charset=utf-8"),
          "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/static/style.css": ("style.css", "text/css; charset=utf-8")}


ORDER_FIELDS = {
    "/api/orders/history": {"machine_id"},
    "/api/orders/draft": {"context_id", "lamp_at_speed"},
    "/api/orders/update": {"context_id", "draft_id", "revision", "changes", "lamp_at_speed"},
    "/api/orders/send": {"context_id", "draft_id", "revision", "confirmed"},
    "/api/orders/close": {"context_id"},
    "/api/orders/new": {"context_id", "revision"},
}


def _field(data, name, *, optional=False, limit=200):
    value = data.get(name)
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"Проверьте поле {name}.")
    return value.strip()


def _manual_url(doc_id):
    return "/manuals/" + quote(doc_id, safe="")


_OBSERVATIONS = {
    CLUTCH_TOPIC: ("F06", clutch_observation_report),
    CHAIN_INSPECTION_TOPIC: ("F14", chain_observation_question),
    GENERATOR_BEARING_TOPIC: ("F05", generator_observation_question),
    OIL_MEASUREMENT_TOPIC: ("F02", oil_observation_question),
    YAMZ536_OIL_TOPIC: ("F02", yamz536_oil_observation_question),
    ZMZ40904_OIL_TOPIC: ("F02", zmz40904_oil_observation_question),
    ZMZ5234_OIL_KNOCK_TOPIC: ("F02", zmz5234_oil_observation_question),
    "clutch-noise": ("F06", clutch_observation_question),
    "gearbox-noise": ("F07", gearbox_observation_question),
    BELT_TOPIC: ("F04", belt_observation_question),
}


def _observation_rule(topic, engine):
    family = (topic or "").partition(":")[0]
    if family == CLUTCH_TOPIC and engine != "ЯМЗ-236М2":
        return None
    if family == CHAIN_INSPECTION_TOPIC and engine != "ЗМЗ-40905.10":
        return None
    if family == BELT_TOPIC and engine != "ЯМЗ-236М2":
        return None
    if family == GENERATOR_BEARING_TOPIC and engine != "ЗМЗ-5234.10":
        return None
    if family == OIL_MEASUREMENT_TOPIC and engine != "ЯМЗ-236М2":
        return None
    if family == YAMZ536_OIL_TOPIC and engine != "ЯМЗ-536":
        return None
    if family == ZMZ40904_OIL_TOPIC and engine != "ЗМЗ-40904.10":
        return None
    if family == ZMZ5234_OIL_KNOCK_TOPIC and engine != "ЗМЗ-5234.10":
        return None
    return _OBSERVATIONS.get(family)


_OIL_OBSERVATION_TOPICS = {OIL_MEASUREMENT_TOPIC, YAMZ536_OIL_TOPIC, ZMZ40904_OIL_TOPIC}


@dataclass(frozen=True)
class _Observation:
    """Хранит уточнение механика и заявки, обоснованные этим уточнением."""
    topic: str
    contexts: frozenset[str]
    drive: str | None = None
    oil: OilObservations | None = None

    @classmethod
    def from_reply(cls, topic, wording, cid, previous=None):
        contexts = frozenset({cid}) if cid else frozenset()
        if previous and cid in previous.contexts:
            contexts = previous.contexts
        drive = belt_drive_in(wording) if cid and topic == BELT_TOPIC else None
        oil = None
        if topic.partition(":")[0] in _OIL_OBSERVATION_TOPICS:
            oil = previous.oil if previous else OilObservations()
        return cls(topic, contexts, drive, oil)


def _work_text(text):
    return " ".join(text.casefold().split()).rstrip(".!?")


class Workshop:
    """Связывает выбранную проверку, поиск по руководству и черновик заявки."""

    def __init__(self, root, data_dir, search, ml_url, toir_url, orders_db, answer_model=None):
        root = Path(root).resolve()
        self.session = partial(ManualSession, root=root, data_dir=data_dir, search=search)
        self.fetch = partial(fetch_diagnosis, ml_url)
        self.answer_model = answer_model
        settings = self.session()
        self.machines = {m["machine_id"]: m for m in settings.machines}
        self.diagnostics = {}
        self.observations = {}
        self.diagnostics_lock = Lock()
        self.orders = WorkOrders(partial(submit_order, toir_url), root / orders_db,
                                 owns_check=self._owns_check, verify_works=self._verify_works)
        self.manuals = {}
        for manual in settings.registry:
            file = (root / manual["path"]).resolve()
            if file.is_relative_to(root) and file.suffix.lower() == ".pdf":
                self.manuals[manual["document_id"]] = (manual, file)

    def machine_list(self):
        """Возвращает карточки и историю выбора без результатов ML."""
        machines = []
        for machine in self.machines.values():
            card = {field: machine[field] for field in (
                "machine_id", "vehicle_id", "label", "model", "engine_model", "engine_variant",
            ) if field in machine}
            card["history"] = [{field: check[field] for field in ("check_id", "checked_at") if field in check}
                               for check in machine["history"]]
            machines.append(card)
        return machines

    def _owns_check(self, machine_id, check_id):
        machine = self.machines.get(machine_id)
        return bool(machine and any(c["check_id"] == check_id for c in machine["history"]))

    def _verify_works(self, machine_id, check_id, works, sources):
        """Повторно проверяет поддерживающую процедуру для изменённого списка работ."""
        selection = machine_id, check_id
        with self.diagnostics_lock:
            diagnostic = self.diagnostics.get(selection)
            observed = self.observations.get(selection)
        if not diagnostic:
            raise ValueError("Получите диагностику заново перед изменением работ.")
        session = self.session()
        related = {}
        for work in works:
            if observed:
                session.selection, session.topic = selection, observed.topic
            reply = session.ask(machine_id, work, check_id=check_id, diagnosis=diagnostic)
            cited = {(s["document_id"], s["page"]): s for s in reply["sources"]}
            matches = [cited[(step["document_id"], step["page"])]
                       for step in reply.get("inspection_steps", [])
                       if step["role"] == "work" and _work_text(step["text"]) == _work_text(work)
                       and (step["document_id"], step["page"]) in cited]
            if observed and observed.topic.startswith("gearbox-noise:") and session.topic != observed.topic:
                matches = []
            if observed and observed.topic == BELT_TOPIC and belt_drive_in(work) != observed.drive:
                matches = []
            if matches and observed and observed.topic == YAMZ536_OIL_CONFIRMED_TOPIC:
                if session.topic != observed.topic or YAMZ536_OIL_NORM_SOURCE not in cited:
                    raise ValueError("Сверьте физическую страницу 49 с нормой давления масла перед изменением работ.")
                matches.append(cited[YAMZ536_OIL_NORM_SOURCE])
            if observed and observed.topic == ZMZ40904_OIL_CONFIRMED_TOPIC and session.topic != observed.topic:
                matches = []
            if check_id == ZMZ5234_CHECK_ID and _work_text(work) == _work_text(ZMZ5234_REPAIR_WORK):
                if not observed or observed.topic != ZMZ5234_REPAIR_CONFIRMED_TOPIC:
                    matches = []
                else:
                    matches = [source for source in matches
                               if (source["document_id"], source["page"]) == ZMZ5234_REPAIR_SOURCE]
            if not matches:
                raise ValueError(f"Не найдена подтверждённая процедура для работы «{work}». Проверьте её по руководству.")
            for source in matches:
                key = source["document_id"], source["page"]
                related[key] = {"document_id": key[0], "page": key[1],
                                "manual": source.get("manual") or source["title"]}
            # Сохраняем источник для основания приоритета, если процедура снова его подтвердила.
            for step in reply.get("inspection_steps", []):
                key = step["document_id"], step["page"]
                if step["role"] == "lamp" and key in cited:
                    for source in sources:
                        if (source["document_id"], source["page"]) == key:
                            related[key] = source
        if observed and observed.topic == OIL_MEASUREMENT_CONFIRMED_TOPIC and not OIL_WORK_SOURCES <= related.keys():
            raise ValueError("Для проверки системы смазки нужны физические страницы 4 и 53 руководства.")
        if check_id == CLUTCH_CHECK_ID and diagnostic.get("anomaly_type") == "F06":
            if not observed or not clutch_order_basis(observed.topic, works, related):
                raise ValueError("Сверьте наблюдения и страницы руководства для выбранной работы по сцеплению.")
        return list(related.values())

    def answer(self, path, data):
        machine_id = _field(data, "machine_id")
        requested = path == "/api/diagnose"
        check_id = _field(data, "check_id", optional=not requested)
        machine = self.machines.get(machine_id)
        if machine is None:
            raise ValueError("Выберите машину из списка.")
        if check_id and not self._owns_check(machine_id, check_id):
            raise ValueError("В истории выбранной машины нет этой проверки.")

        selection = machine_id, check_id
        with self.diagnostics_lock:
            diagnostic = self.diagnostics.get(selection)
            observed = self.observations.get(selection)
        session = self.session()
        observation_answer = False
        oil = None
        if requested:
            if data.get("previous_context_id"):
                self.orders.invalidate(_field(data, "previous_context_id"))
            diagnostic = _validated_result(self.fetch(*selection), machine, check_id)
            question = (diagnostic["anomaly_title"] or "Результат диагностики") + (
                ". Что уточнить у водителя и что проверить на двигателе?"
            )
        else:
            question = _field(data, "question", limit=3000)
            previous = _field(data, "topic", optional=True, limit=1000) if data.get("topic") else None
            if previous:
                # Тема относится только к ранее выбранной паре машины и проверки.
                state = json.loads(previous)
                if (isinstance(state, list) and len(state) == 3 and tuple(state[:2]) == selection
                        and isinstance(state[2], str) and not _observation_rule(state[2], machine["engine_model"])):
                    session.selection = selection
                    session.topic = state[2]
            if observed:
                code, recognizes = _OBSERVATIONS[observed.topic.partition(":")[0]]
                observation_answer = recognizes(question)
                if observed.oil is not None:
                    oil = observed.oil.with_report(question)
                clutch_followup = observed.topic.partition(":")[0] == CLUTCH_TOPIC and clutch_overview_question(question)
                if (not session.topic or observation_answer or clutch_followup
                        or code == "F07" and gearbox_overview_question(question)):
                    session.selection, session.topic = selection, observed.topic

        if requested and diagnostic["status"] == "normal" and diagnostic["signal_quality"] != "bad":
            text = "По результату ML отклонений не обнаружено."
            if diagnostic["signal_quality"] == "warning":
                text += " В записи есть помехи. При необходимости повторите запись."
            reply = {"engine_model": machine["engine_model"], "engine_variant": machine.get("engine_variant"),
                     "text": text, "sources": [], "warnings": []}
        else:
            reply = session.ask(machine_id, question, check_id=check_id, diagnosis=diagnostic)

        reading_pages = [source for entry in reply.get("reading_references", []) for source in entry["sources"]]
        for source in [*reply["sources"], *reading_pages]:
            base = _manual_url(source["document_id"])
            page = source["page"]
            source["url"] = f"{base}.pdf#page={page}"
            source["page_url"] = f"{base}/page/{page}"
        reply["topic"] = json.dumps([*selection, session.topic], ensure_ascii=False) if session.topic else ""
        reply["diagnosis"] = diagnostic
        rule = _observation_rule(session.topic, machine["engine_model"])
        if requested:
            with self.diagnostics_lock:
                cid = self.orders.open_context(*selection, diagnostic, reply)
                reply["context_id"] = cid
                self.diagnostics[selection] = diagnostic
                if (rule and diagnostic.get("anomaly_type") == rule[0] and diagnostic.get("status") == "anomaly"
                        and diagnostic.get("signal_quality") in {"good", "warning"}):
                    self.observations[selection] = _Observation.from_reply(
                        session.topic, diagnostic.get("description") or "", cid)
                else:
                    self.observations.pop(selection, None)
        elif observed and observation_answer and rule and rule[0] == diagnostic.get("anomaly_type"):
            with self.diagnostics_lock:
                if self.diagnostics.get(selection) is not diagnostic or self.observations.get(selection) is not observed:
                    raise ValueError("Диагностика или наблюдения изменились. Повторите уточнение.")
                cid = self.orders.open_context(*selection, diagnostic, reply, observation_topic=session.topic)
                reply["context_id"] = cid
                state = _Observation.from_reply(session.topic, question, cid, observed)
                self.observations[selection] = replace(state, oil=oil)
        else:
            if observed and oil is not None and oil != observed.oil:
                with self.diagnostics_lock:
                    if self.diagnostics.get(selection) is not diagnostic or self.observations.get(selection) is not observed:
                        raise ValueError("Диагностика или наблюдения изменились. Повторите уточнение.")
                    self.observations[selection] = replace(observed, oil=oil)
            if data.get("context_id"):
                cid = _field(data, "context_id")
                reply["order_context_changed"] = not self.orders.refresh(cid, *selection, diagnostic)
        if not requested and diagnostic is None and self.answer_model is not None:
            order_reference_answer(reply, question, session.topic, self.answer_model)
        reply.pop("inspection_steps", None)
        return reply


    def _diagnosis_for_copy(self, cid, machine_id, check_id):
        """Сверяет основание повторной заявки с последними наблюдениями под блокировкой диагностики."""
        selection = machine_id, check_id
        observed = self.observations.get(selection)
        if observed and cid not in observed.contexts:
            raise ValueError("Состав этой заявки не подтверждён последним уточнением механика.")
        return self.diagnostics.get(selection)

    def order(self, path, data):
        """Меняет показанный черновик или отправляет подтверждённую версию."""
        allowed = ORDER_FIELDS[path]
        if set(data) - allowed:
            raise ValueError("В запросе есть поля, которые нельзя изменять.")
        if path == "/api/orders/history":
            machine_id = _field(data, "machine_id")
            if machine_id not in self.machines:
                raise ValueError("Выберите машину из списка.")
            orders = self.orders.history(machine_id)
            used = {s["document_id"] for row in orders for s in row["payload"]["sources"]}
            manuals = {doc: {k: self.manuals[doc][0][k] for k in ("title", "edition")}
                       for doc in used if doc in self.manuals}
            return {"orders": orders, "manuals": manuals}
        context = _field(data, "context_id")
        if path == "/api/orders/close":
            self.orders.invalidate(context)
            return {"closed": True}
        lamp = data.get("lamp_at_speed", False)
        if type(lamp) is not bool:
            raise ValueError("Сообщение о лампе должно быть подтверждено механиком.")
        if path == "/api/orders/draft":
            return self.orders.draft(context, lamp_at_speed=lamp)
        revision = data.get("revision")
        if type(revision) is not int or revision < 1:
            raise ValueError("Нужна версия показанного черновика.")
        if path == "/api/orders/new":
            with self.diagnostics_lock:
                snapshot = self.orders.new_from_sent(context, revision, partial(self._diagnosis_for_copy, context))
                selection = snapshot["machine_id"], snapshot["check_id"]
                observed = self.observations.get(selection)
                if observed:
                    self.observations[selection] = replace(observed, contexts=observed.contexts | {snapshot["context_id"]})
                return snapshot
        draft = _field(data, "draft_id")
        if path == "/api/orders/send":
            return self.orders.send(context, draft, revision, data.get("confirmed") is True)
        changes = data.get("changes")
        if not isinstance(changes, dict):
            raise ValueError("Передайте изменённые поля черновика.")
        return self.orders.update(context, draft, revision, changes, lamp_at_speed=data.get("lamp_at_speed"))


def _page_html(manual, page, base):
    title = escape(manual["title"])
    edition = escape(manual["edition"])
    return f"""<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Страница {page} · {title}</title>
<style>body{{margin:0;background:#f2f3ef;color:#202b29;font:1rem/1.45 system-ui,sans-serif}}
main{{max-width:68rem;margin:auto;padding:1rem}}h1{{font-size:1.18rem;margin:0}}
p{{margin:.4rem 0}}a{{display:inline-block;padding:.55rem 0;color:#125747}}img{{display:block;width:100%;height:auto;background:white}}</style>
<main><h1>{title}</h1><p>{edition} · Страница {page} в PDF</p>
<a href="{base}.pdf#page={page}">Открыть полный PDF</a>
<img src="{base}/page/{page}.png" alt="Страница {page} руководства"></main></html>""".encode()


class WebHandler(BaseHTTPRequestHandler):
    """Отдаёт интерфейс, ответы чата и документы из реестра."""

    def _headers(self, status, mime, size, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(size))
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _send(self, status, mime, body):
        self._headers(status, mime, len(body))
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, value):
        self._send(status, "application/json; charset=utf-8", json.dumps(value, ensure_ascii=False).encode())

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlsplit(self.path).path
        try:
            if path in ASSETS:
                name, mime = ASSETS[path]
                self._send(200, mime, (STATIC / name).read_bytes())
            elif path == "/api/machines":
                self._json(200, {"machines": self.server.workshop.machine_list()})
            elif path.startswith("/manuals/"):
                self._source(path)
            else:
                self._json(404, {"error": "Страница не найдена."})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except (OSError, RuntimeError, ValueError):
            self._json(404, {"error": "Не удалось открыть источник. Проверьте файл руководства."})

    def _source(self, path):
        match = re.fullmatch(r"/manuals/([^/]+?)(?:\.pdf|/page/([1-9]\d{0,6})(\.png)?)", path)
        entry = self.server.workshop.manuals.get(unquote(match[1])) if match else None
        if not entry:
            self._json(404, {"error": "Руководство отсутствует в реестре."})
            return
        manual, file = entry
        if match[2] is None:
            self._original(file)
            return
        page = int(match[2])
        with pymupdf.open(file) as pdf:
            if page > pdf.page_count:
                self._json(404, {"error": "В PDF нет такой страницы."})
                return
            if match[3]:
                sheet = pdf.load_page(page - 1)
                scale = 1440 / max(sheet.rect.width, sheet.rect.height)
                image = sheet.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                self._send(200, "image/png", image.tobytes("png"))
                return
        self._send(200, "text/html; charset=utf-8", _page_html(manual, page, _manual_url(manual["document_id"])))

    def _original(self, file):
        with file.open("rb") as src:
            size = src.seek(0, 2)
            start, end = 0, size - 1
            headers = {"Accept-Ranges": "bytes", "Content-Disposition": "inline"}
            requested = self.headers.get("Range")
            if requested:
                m = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
                if m and (m[1] or m[2]):
                    if m[1]:
                        start = int(m[1])
                        end = min(end, int(m[2])) if m[2] else end
                    else:
                        start = max(0, size - int(m[2]))
                if not m or not (m[1] or m[2]) or start > end:
                    self._headers(416, "application/pdf", 0, {"Content-Range": f"bytes */{size}"})
                    return
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            remaining = end - start + 1
            self._headers(206 if requested else 200, "application/pdf", remaining, headers)
            if self.command == "HEAD":
                return
            src.seek(start)
            while remaining:
                chunk = src.read(min(remaining, 128 * 1024))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in {"/api/ask", "/api/diagnose"} and path not in ORDER_FIELDS:
            self._json(404, {"error": "Команда не найдена."})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 32_768 or self.headers.get_content_type() != "application/json":
                raise ValueError("Ожидается JSON с вопросом или выбранной проверкой.")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("Ожидается объект JSON.")
            workshop = self.server.workshop
            reply = workshop.order(path, data) if path in ORDER_FIELDS else workshop.answer(path, data)
        except OrderServiceError as exc:
            self._json(502, {"error": str(exc), "status": "uncertain"})
        except SearchUnavailable as exc:
            self._json(503, {"error": str(exc)})
        except DraftChanged as exc:
            self._json(409, {"error": str(exc), "draft": exc.draft})
        except MLUnavailable as exc:
            self._json(502, {"error": str(exc)})
        except (UnicodeError, json.JSONDecodeError):
            self._json(400, {"error": "Не удалось прочитать JSON."})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except (OSError, sqlite3.Error):
            self._json(503, {"error": "Не удалось прочитать данные агента. Проверьте реестр и индекс."})
        else:
            self._json(200, reply)


def make_server(address, *, root=None, data_dir="data", search=None,
                qdrant_url=None, collection=None, search_mode="hybrid",
                ml_url=None, toir_url=None, orders_db="data/state/work_orders.sqlite", answer_model=None):
    """Создаёт HTTP-сервер с данными из указанного проекта."""
    if search is None:
        search = configured_search(qdrant_url, collection, search_mode)
    ml_url = ml_url or os.getenv("MOCK_ML_URL") or "http://127.0.0.1:8001"
    toir_url = toir_url or os.getenv("MOCK_TOIR_URL") or "http://127.0.0.1:8002"
    workshop = Workshop(root or Path.cwd(), data_dir, search, ml_url, toir_url, orders_db, answer_model)
    server = ThreadingHTTPServer(address, WebHandler)
    server.workshop = workshop
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--ml-url", help="адрес сервиса ML вместо MOCK_ML_URL")
    parser.add_argument("--toir-url", help="адрес сервиса ТОиР вместо MOCK_TOIR_URL")
    parser.add_argument("--orders-db", default="data/state/work_orders.sqlite", help="база черновиков и истории приложения")
    parser.add_argument("--data", default="data")
    add_search_arguments(parser)
    args = parser.parse_args(argv)
    try:
        with make_server((args.host, args.port), data_dir=args.data, qdrant_url=args.qdrant_url,
                         collection=args.collection, search_mode=args.search_mode,
                         ml_url=args.ml_url,
                         toir_url=args.toir_url, orders_db=args.orders_db, answer_model=YandexGPT.from_env()) as server:
            print(f"Веб-чат: http://{args.host}:{server.server_port}", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError) as exc:
        print(f"Ошибка запуска: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
