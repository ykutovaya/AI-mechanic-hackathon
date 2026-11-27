"""Редактирование и хранение подтверждённых заявок на дефектовку."""

from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from typing import NamedTuple
from uuid import uuid4

from filelock import FileLock, Timeout

from .knowledge.clutch_yamz236 import CLUTCH_CHECK_ID, clutch_order_basis
from .knowledge.diagnostic_works import CHAIN_CHECK_ID, CHAIN_REPLACEMENT_TOPIC, CHAIN_REPLACEMENT_WORK, CHAIN_SOURCE
from .knowledge.generator_bearing import (
    BEARING_CONFIRMED, GENERATOR_BEARING_SOURCE, GENERATOR_BEARING_WORK, GENERATOR_ROTOR_REASON, ROTOR_CONFIRMED,
)
from .knowledge.yamz236_oil import OIL_CHECK_ID, OIL_MEASUREMENT_CONFIRMED_TOPIC, OIL_WORK, OIL_WORK_SOURCES
from .knowledge.yamz536_oil import (
    YAMZ536_CHECK_ID, YAMZ536_OIL_CONFIRMED_TOPIC, YAMZ536_OIL_WORK_SOURCES, YAMZ536_OIL_WORKS,
)
from .knowledge.zmz40904_oil import (
    ZMZ40904_CHECK_ID, ZMZ40904_OIL_CONFIRMED_TOPIC, ZMZ40904_OIL_WORK_SOURCES, ZMZ40904_OIL_WORKS,
)
from .knowledge.zmz5234_oil_knock import (
    ZMZ5234_CHECK_ID, ZMZ5234_REPAIR_CONFIRMED_TOPIC, ZMZ5234_REPAIR_SOURCE, ZMZ5234_REPAIR_WORK,
)
from .ml_client import FAULT_CODES


class _MeasuredOilRule(NamedTuple):
    topic: str
    works: frozenset[str]
    sources: frozenset[tuple[str, int]]
    diagnosis: str

    @property
    def priority_reason(self):
        return self.diagnosis + " Для проверки системы смазки установлен средний приоритет."


_MEASURED_OIL_RULES = {
    OIL_CHECK_ID: _MeasuredOilRule(
        OIL_MEASUREMENT_CONFIRMED_TOPIC, frozenset({OIL_WORK}), OIL_WORK_SOURCES,
        "Механик сообщил о давлении масла ниже 300 кПа на номинальных оборотах.",
    ),
    YAMZ536_CHECK_ID: _MeasuredOilRule(
        YAMZ536_OIL_CONFIRMED_TOPIC, frozenset(YAMZ536_OIL_WORKS), YAMZ536_OIL_WORK_SOURCES,
        "Механик сообщил о давлении масла ниже 400 кПа на номинальных оборотах "
        "при прогреве двигателя до 80–100 °C.",
    ),
    ZMZ40904_CHECK_ID: _MeasuredOilRule(
        ZMZ40904_OIL_CONFIRMED_TOPIC, frozenset(ZMZ40904_OIL_WORKS), ZMZ40904_OIL_WORK_SOURCES,
        "Механик сообщил о постоянном горении лампы аварийного давления масла. "
        "Стрелочный указатель или контрольный манометр показал выше 1 кгс/см².",
    ),
}


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def validate_order(payload):
    """Проверяет поля заявки в представлении веб-чата."""
    keys = {"machine_id", "check_id", "fault_code", "diagnosis", "priority", "works", "parts", "sources"}
    if not isinstance(payload, dict) or set(payload) != keys:
        raise ValueError("Нужны все поля заявки.")
    if not all(_text(payload[k]) for k in ("machine_id", "check_id", "fault_code", "diagnosis")):
        raise ValueError("Укажите машину, проверку, код неисправности и диагноз.")
    if payload["fault_code"] not in FAULT_CODES:
        raise ValueError("Код неисправности должен быть одним из F01–F14.")
    if payload["priority"] not in ("low", "medium", "high"):
        raise ValueError("Выберите приоритет заявки.")
    for key in ("works", "parts"):
        values = payload[key]
        if not isinstance(values, list) or not all(_text(v) for v in values):
            raise ValueError("В перечне работ или запчастей есть пустая или неверная строка.")
    if not payload["works"]:
        raise ValueError("Добавьте хотя бы одну работу.")
    if not isinstance(payload["sources"], list) or not payload["sources"]:
        raise ValueError("Нужна применимая страница руководства.")
    for source in payload["sources"]:
        if (not isinstance(source, dict) or set(source) not in (
                    {"document_id", "page"}, {"document_id", "page", "manual"})
                or not _text(source["document_id"]) or type(source["page"]) is not int or source["page"] < 1
                or ("manual" in source and not _text(source["manual"]))):
            raise ValueError("Проверьте документ и номер физической страницы PDF.")


def _json(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _snapshot(row):
    payload = json.loads(row["payload"])
    reason = "Диагностика выявила отклонение, причина требует проверки."
    if row["lamp_at_speed"]:
        reason = "Механик сообщил, что лампа аварийного давления масла горела на средних оборотах."
        if payload["priority"] != "high":
            reason += " Рекомендован высокий приоритет. Итоговый приоритет выбран механиком."
    elif row["priority_reason"]:
        reason = row["priority_reason"]
        if payload["priority"] != json.loads(row["original_payload"])["priority"]:
            reason += " Итоговый приоритет выбран механиком."
    elif payload["priority"] != "medium":
        reason = "Приоритет выбран механиком."
    return dict(context_id=row["context_id"], draft_id=row["draft_id"],
                machine_id=row["machine_id"], check_id=row["check_id"],
                revision=row["revision"], status=row["status"], number=row["number"], sent_at=row["sent_at"],
                lamp_supported=bool(row["lamp_supported"]), lamp_at_speed=bool(row["lamp_at_speed"]),
                priority_reason=reason, payload=payload)


class DraftChanged(ValueError):
    """При конфликте версий возвращает механику актуальный состав."""

    def __init__(self, row):
        super().__init__("Черновик изменился. Проверьте текущую версию и подтвердите её заново.")
        self.draft = _snapshot(row)


class WorkOrders:
    """Сохраняет показанный состав и результат отправки в базе приложения."""

    def __init__(self, sender, db_path=None, *, owns_check=None, verify_works=None):
        self.sender = sender
        self.owns_check = owns_check
        self.verify_works = verify_works
        self._temporary = TemporaryDirectory() if db_path is None else None
        self.db = Path(self._temporary.name, "orders.sqlite") if self._temporary else Path(db_path)
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS drafts (
                context_id TEXT PRIMARY KEY, draft_id TEXT UNIQUE NOT NULL,
                machine_id TEXT NOT NULL, check_id TEXT NOT NULL, diagnostic TEXT NOT NULL,
                original_payload TEXT NOT NULL, payload TEXT NOT NULL,
                lamp_supported INTEGER NOT NULL, lamp_at_speed INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'draft', send_key TEXT UNIQUE,
                priority_reason TEXT NOT NULL DEFAULT '', number TEXT, sent_at TEXT)""")
            # У каждой отправленной заявки может быть один непосредственный преемник.
            columns = {item["name"] for item in conn.execute("PRAGMA table_info(drafts)")}
            if "parent_context" not in columns:
                conn.execute("ALTER TABLE drafts ADD COLUMN parent_context TEXT")
            if "priority_reason" not in columns:
                conn.execute("ALTER TABLE drafts ADD COLUMN priority_reason TEXT NOT NULL DEFAULT ''")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS by_parent ON drafts(parent_context)")
            conn.execute("CREATE INDEX IF NOT EXISTS by_machine ON drafts(machine_id, check_id)")
            # После остановки процесса ОС освобождает файловую блокировку отправителя.
            pending = conn.execute("SELECT draft_id FROM drafts WHERE status='sending'").fetchall()
            for row in pending:
                guard = self._send_lock(row["draft_id"])
                try:
                    with guard.acquire(timeout=0):
                        conn.execute("UPDATE drafts SET status='uncertain' WHERE draft_id=?", (row["draft_id"],))
                except Timeout:
                    continue

    @contextmanager
    def _transaction(self, *, write=True):
        with closing(sqlite3.connect(self.db, timeout=10)) as conn, conn:
            conn.row_factory = sqlite3.Row
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn

    def _send_lock(self, draft_id):
        return FileLock(str(self.db.with_name(f".{self.db.name}.{draft_id}.lock")))

    @staticmethod
    def _row(conn, cid):
        row = conn.execute("SELECT * FROM drafts WHERE context_id=?", (cid,)).fetchone()
        if row is None or not row["active"]:
            raise ValueError("Диагностика изменилась. Запросите её заново.")
        rule = _MEASURED_OIL_RULES.get(row["check_id"])
        if rule and row["status"] == "draft" and row["priority_reason"] != rule.priority_reason:
            raise ValueError("Получите диагностику и подтвердите показание давления масла и условия проверки.")
        return row

    def _version(self, conn, cid, draft_id, revision):
        row = self._row(conn, cid)
        if (not row["revision"] or row["draft_id"] != draft_id
                or type(revision) is not int or row["revision"] != revision):
            raise DraftChanged(row)
        return row

    @staticmethod
    def _propose(machine_id, check_id, diagnostic, reply, observation_topic):
        if diagnostic.get("status") != "anomaly" or diagnostic.get("signal_quality") not in ("good", "warning"):
            return None
        code = diagnostic.get("anomaly_type")
        name = diagnostic.get("anomaly_title")
        if not _text(name) or not _text(code) or code not in FAULT_CODES:
            return None
        cited = {(s["document_id"], s["page"]): s for s in reply.get("sources", [])}
        works, refs = [], []
        lamp = False
        for step in reply.get("inspection_steps", []):
            slot = step.get("document_id"), step.get("page")
            if slot not in cited or not _text(step.get("text")):
                continue
            source = cited[slot]
            ref = dict(document_id=slot[0], page=slot[1])
            manual = source.get("manual") or source.get("title")
            if _text(manual):
                ref["manual"] = manual
            if step.get("role", "work") == "lamp" and code == "F02":
                lamp = True
            elif step.get("role", "work") == "work":
                if step["text"] not in works:
                    works.append(step["text"])
            else:
                continue
            if ref not in refs:
                refs.append(ref)
        if not works:
            return None
        diagnosis = f"По данным ML предполагается: {name}. Конкретная причина требует дефектовки."
        priority, priority_reason = "medium", ""
        rule = _MEASURED_OIL_RULES.get(check_id)
        if code == "F02" and rule:
            linked = {(ref["document_id"], ref["page"]) for ref in refs}
            if observation_topic != rule.topic or rule.works != set(works) or not rule.sources <= linked:
                return None
            diagnosis, priority_reason = rule.diagnosis, rule.priority_reason
            lamp = False
        elif code == "F02" and check_id == ZMZ5234_CHECK_ID and ZMZ5234_REPAIR_WORK in works:
            if observation_topic != ZMZ5234_REPAIR_CONFIRMED_TOPIC or ZMZ5234_REPAIR_SOURCE not in cited:
                return None
            if not any(step.get("role") == "work" and step.get("text") == ZMZ5234_REPAIR_WORK
                       and (step.get("document_id"), step.get("page")) == ZMZ5234_REPAIR_SOURCE
                       for step in reply.get("inspection_steps", [])):
                return None
            diagnosis = "Механик подтвердил сильный износ вкладышей подшипников коленчатого вала."
            priority_reason = diagnosis
        elif code == "F06" and check_id == CLUTCH_CHECK_ID:
            if not clutch_order_basis(observation_topic, works, cited):
                return None
        elif code == "F14" and check_id == CHAIN_CHECK_ID and CHAIN_REPLACEMENT_WORK in works:
            if observation_topic != CHAIN_REPLACEMENT_TOPIC or CHAIN_SOURCE not in cited:
                return None
            diagnosis = "Механик подтвердил необходимость замены цепей ГРМ по результатам дефектовки."
        elif (code == "F05" and observation_topic in {BEARING_CONFIRMED, ROTOR_CONFIRMED}
                and GENERATOR_BEARING_WORK in works and GENERATOR_BEARING_SOURCE in cited):
            diagnosis = "Механик подтвердил неисправность подшипника генератора."
            priority_reason = diagnosis + " Для замены по умолчанию установлен средний приоритет."
            if observation_topic == ROTOR_CONFIRMED:
                priority, priority_reason = "high", GENERATOR_ROTOR_REASON
                diagnosis += " " + priority_reason
        payload = dict(machine_id=machine_id, check_id=check_id, fault_code=code, diagnosis=diagnosis,
                       priority=priority, works=works, parts=[], sources=refs)
        validate_order(payload)
        return payload, lamp, priority_reason

    def open_context(self, machine_id, check_id, diagnostic, reply, *, observation_topic=""):
        if not diagnostic or (diagnostic.get("vehicle_id"), diagnostic.get("session_id")) != (machine_id, check_id):
            return ""
        if self.owns_check and not self.owns_check(machine_id, check_id):
            return ""
        result = _json(diagnostic)
        proposal = self._propose(machine_id, check_id, diagnostic, reply, observation_topic)
        with self._transaction() as conn:
            if proposal is None:
                conn.execute("""UPDATE drafts SET active=0
                                WHERE machine_id=? AND check_id=? AND status='draft' AND active=1""",
                             (machine_id, check_id))
                return ""
            payload, lamp, priority_reason = proposal
            original = _json(payload)
            conn.execute("""UPDATE drafts SET active=0
                            WHERE machine_id=? AND check_id=? AND status='draft' AND active=1
                            AND (diagnostic<>? OR original_payload<>? OR lamp_supported<>? OR priority_reason<>?)""",
                         (machine_id, check_id, result, original, int(lamp), priority_reason))
            existing = conn.execute("""SELECT context_id FROM drafts
                WHERE machine_id=? AND check_id=? AND diagnostic=? AND original_payload=?
                  AND lamp_supported=? AND priority_reason=? AND (active=1 OR status <> 'draft' OR parent_context IS NOT NULL)
                ORDER BY rowid DESC LIMIT 1""",
                (machine_id, check_id, result, original, int(lamp), priority_reason)).fetchone()
            if existing:
                cid = existing["context_id"]
                conn.execute("UPDATE drafts SET active=1 WHERE context_id=?", (cid,))
                return cid
            cid = uuid4().hex
            conn.execute("""INSERT INTO drafts
                (context_id, draft_id, machine_id, check_id, diagnostic, original_payload, payload, lamp_supported, priority_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (cid, uuid4().hex, machine_id, check_id, result, original, original, int(lamp), priority_reason))
            return cid

    def new_from_sent(self, cid, revision, fetch_diagnosis):
        """Создаёт черновик по отправленной заявке после сверки с текущей проверкой ML."""
        with self._transaction(write=False) as conn:
            source = conn.execute("SELECT * FROM drafts WHERE context_id=?", (cid,)).fetchone()
        if source is None or source["status"] != "sent":
            raise ValueError("Для новой заявки выберите уже отправленную в ТОиР.")
        if source["revision"] != revision:
            raise DraftChanged(source)
        car, check = source["machine_id"], source["check_id"]
        if self.owns_check and not self.owns_check(car, check):
            raise ValueError("В истории машины нет выбранной проверки.")
        if _json(fetch_diagnosis(car, check)) != source["diagnostic"]:
            raise ValueError("Данные ML изменились. Получите диагностику заново.")
        with self._transaction() as conn:
            # Отправленный состав неизменяем. Повтор команды возвращает ту же копию.
            child = conn.execute("SELECT * FROM drafts WHERE parent_context=?", (cid,)).fetchone()
            if child is None:
                new_id = uuid4().hex
                conn.execute("""INSERT INTO drafts (context_id, draft_id, parent_context, machine_id,
                    check_id, diagnostic, original_payload, payload, lamp_supported, lamp_at_speed, priority_reason, revision)
                    SELECT ?, ?, context_id, machine_id, check_id, diagnostic, original_payload,
                           payload, lamp_supported, lamp_at_speed, priority_reason, 1 FROM drafts WHERE context_id=?""",
                             (new_id, uuid4().hex, cid))
            else:
                new_id = child["context_id"]
                conn.execute("UPDATE drafts SET active=1 WHERE context_id=?", (new_id,))
            return _snapshot(self._row(conn, new_id))

    def invalidate(self, cid):
        with self._transaction() as conn:
            conn.execute("UPDATE drafts SET active=0 WHERE context_id=?", (cid,))

    def refresh(self, cid, machine_id, check_id, diagnostic):
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM drafts WHERE context_id=? AND active=1", (cid,)).fetchone()
            if not row:
                return False
            if (row["machine_id"], row["check_id"]) != (machine_id, check_id):
                return False
            current = row["diagnostic"] == _json(diagnostic)
            if not current:
                conn.execute("UPDATE drafts SET active=0 WHERE context_id=?", (cid,))
            return current

    def draft(self, cid, lamp_at_speed=False):
        with self._transaction() as conn:
            row = self._row(conn, cid)
            if not row["revision"]:
                if lamp_at_speed and not row["lamp_supported"]:
                    raise ValueError("Условие аварийной лампы неприменимо к этой диагностике.")
                body = json.loads(row["payload"])
                if lamp_at_speed:
                    body["priority"] = "high"
                conn.execute("UPDATE drafts SET revision=1, lamp_at_speed=?, payload=? WHERE context_id=?",
                             (int(lamp_at_speed), _json(body), cid))
                row = self._row(conn, cid)
            return _snapshot(row)

    def update(self, cid, draft_id, revision, changes, lamp_at_speed=None):
        """Проверяет правки и сохраняет новую версию черновика."""
        if not isinstance(changes, dict) or changes.keys() - {"diagnosis", "priority", "works", "parts"}:
            raise ValueError("Можно изменить диагноз, приоритет, работы и запчасти.")
        if lamp_at_speed is not None and type(lamp_at_speed) is not bool:
            raise ValueError("Укажите, горела ли лампа аварийного давления масла.")
        with self._transaction(write=False) as conn:
            row = self._version(conn, cid, draft_id, revision)
            if row["status"] != "draft":
                raise ValueError("Эта версия уже передана в ТОиР. Проверьте результат её отправки.")
        current = json.loads(row["payload"])
        body = dict(current, **changes)
        lamp_before = bool(row["lamp_at_speed"])
        if lamp_at_speed is None:
            lamp_at_speed = lamp_before
        if lamp_at_speed and not row["lamp_supported"]:
            raise ValueError("Условие аварийной лампы неприменимо.")
        if lamp_at_speed != lamp_before and "priority" not in changes:
            body["priority"] = "high" if lamp_at_speed else "medium"
        validate_order(body)
        if self.verify_works and body["works"] != current["works"]:
            body["sources"] = self.verify_works(row["machine_id"], row["check_id"],
                                                body["works"], current["sources"])
            validate_order(body)
        encoded = _json(body)
        with self._transaction() as conn:
            row = self._version(conn, cid, draft_id, revision)
            if row["status"] != "draft":
                raise ValueError("Эта версия уже передана в ТОиР. Проверьте результат её отправки.")
            if encoded != row["payload"] or bool(row["lamp_at_speed"]) != lamp_at_speed:
                conn.execute("""UPDATE drafts SET payload=?, lamp_at_speed=?, revision=revision+1
                                WHERE context_id=?""", (encoded, int(lamp_at_speed), cid))
                row = self._row(conn, cid)
            return _snapshot(row)

    def send(self, cid, draft_id, revision, confirmed):
        guard = None
        try:
            with self._transaction() as conn:
                row = self._version(conn, cid, draft_id, revision)
                if confirmed is not True:
                    raise ValueError("Подтвердите показанную версию заявки.")
                if row["status"] != "draft":
                    return _snapshot(row)
                body = json.loads(row["payload"])
                validate_order(body)
                if self.owns_check and not self.owns_check(body["machine_id"], body["check_id"]):
                    raise ValueError("В истории выбранной машины нет этой проверки.")
                key = f"{draft_id}-{revision}"
                guard = self._send_lock(draft_id)
                guard.acquire(timeout=0)
                conn.execute("""UPDATE drafts SET payload=?, send_key=?, status='sending'
                                WHERE context_id=?""", (_json(body), key, cid))
            try:
                number = self.sender(deepcopy(body), key)
                if not _text(number):
                    raise ValueError("ТОиР не вернула номер заявки.")
            except Exception:
                with self._transaction() as conn:
                    conn.execute("UPDATE drafts SET status='uncertain' WHERE context_id=?", (cid,))
                raise
            with self._transaction() as conn:
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                conn.execute("UPDATE drafts SET status='sent', number=?, sent_at=? WHERE context_id=?",
                             (number, now, cid))
                return _snapshot(conn.execute("SELECT * FROM drafts WHERE context_id=?", (cid,)).fetchone())
        finally:
            if guard is not None:
                guard.release()

    def history(self, machine_id):
        """Возвращает отправки машины с точным составом и временем получения номера."""
        with self._transaction(write=False) as conn:
            rows = conn.execute("""SELECT * FROM drafts WHERE machine_id=? AND status <> 'draft'
                                   ORDER BY rowid DESC""", (machine_id,))
            return [_snapshot(row) for row in rows]
