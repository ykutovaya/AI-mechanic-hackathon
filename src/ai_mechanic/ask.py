"""Ответ по руководству выбранной машины. Запуск через python -m ai_mechanic.ask."""

import argparse
import json
import re
import webbrowser

from qdrant_client.http.exceptions import ApiException
from pathlib import Path
import sys

from .knowledge.valve_belt_checks import VALVE_TOPIC, complete_valve_sources
from .knowledge.clutch_yamz236 import CLUTCH_TOPIC, complete_clutch_sources
from .knowledge.diagnostic_clarifications import GEAR_TOPIC, add_gear_drive_source
from .knowledge.diagnostic_works import complete_chain_sources
from .knowledge.f11_egr import add_catalog_egr_evidence
from .knowledge.temperature_pressure_checks import add_oil_indicator_evidence
from .knowledge.zmz40904_oil import ZMZ40904_OIL_TOPIC, complete_zmz40904_oil_sources
from .knowledge.air_filter import AIR_FILTER_TOPIC, air_filter_reference
from .knowledge.fuel_system import fuel_system_reference
from .knowledge.ignition import ignition_observations
from .knowledge.search import SearchUnavailable, add_search_arguments, configured_search, retrieve
from .knowledge.sources import SourceUnavailable, resolve_source
from .knowledge.expert_catalog import requests_references
from .knowledge.expert_answers import generic_diagnostic_question


def _records(path, key=None):
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if key and isinstance(data, dict):
        data = data.get(key)
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        raise ValueError(f"{path}: ожидается список записей.")
    return data


def _machine(vehicle):
    """Добавляет имена полей, которые использует перенесённый интерфейс."""
    vehicle_id = vehicle["vehicle_id"]
    history = [
        dict(check, check_id=check["session_id"])
        for check in vehicle["sessions"]
        if check.get("vehicle_id", vehicle_id) == vehicle_id
    ]
    return dict(vehicle, machine_id=vehicle_id, label=vehicle["model"], history=history)


def _blank(engine="", variant=None):
    return dict(engine_model=engine, engine_variant=variant, text="", sources=[],
                queries=[], warnings=[], inspection_steps=[], synthesis="none")


def _validated_result(result, machine, check_id):
    if not isinstance(result, dict) or (result.get("vehicle_id"), result.get("session_id")) != (
            machine["vehicle_id"], check_id):
        raise ValueError("Идентификаторы результата ML не совпадают с выбранной проверкой.")
    if result.get("status") not in {"normal", "anomaly", "check_failed"}:
        raise ValueError("Неизвестный статус проверки ML.")
    if result.get("signal_quality") not in {"good", "warning", "bad"}:
        raise ValueError("Неизвестное качество записи ML.")
    if result.get("anomaly_type") == "F12":
        result = dict(result, description=ignition_observations(result.get("description") or ""))
    return result


def _applies(hit, engine, variant):
    if engine not in hit["engine_models"]:
        return False
    allowed = {f"{engine}|all"}
    if variant:
        allowed.add(f"{engine}|{variant}")
    return bool(allowed.intersection(hit.get("applicability", ())))


# Заводская конструкция сверена именно в этих редакциях PDF.
_FACTORY_LAYOUTS = {
    ("F09", "ЗМЗ-40905.10"): (
        ("zmz-40904-40905-rk-2025", "99cf64334c578d668ce67bc4c9a1d7b0bb1e9c59ddac982b6061ea18533a7147", (64, 65)),
    ),
    ("F09", "ЗМЗ-5234.10"): (
        ("zmz-5234-10-rk-2025", "1abd24d9c7f81167a895bec4372bc439e85995aa6207349d8b6e8edb4eac9c09", (30, 84)),
    ),
    ("F11", "ЯМЗ-236М2"): (
        ("yamz-236m2-re-2010", "4268a9519e02a668a826fe6d154de0360f6767015868ca2de7410ba570d73dd1", (43,)),
        ("yamz-236m2-rk-2014", "231e067ea9d220dec0ab7629bf19fbbf59d29e01d81e9f3bf5b0b1dee76a141b", (180, 221)),
    ),
}
_FACTORY_SYSTEM_NOTICES = {
    "F09": ("Турбокомпрессор, наддув", "турбокомпрессор не предусмотрен"),
    "F11": ("Рециркуляция отработавших газов", "система рециркуляции отработавших газов (EGR) не предусмотрена"),
}


def _factory_layout_sources(registry, root, engine, documents):
    """Возвращает ссылки после проверки всех редакций и физических страниц."""
    sources = []
    for document, digest, pages in documents:
        entries = [entry for entry in registry if entry["document_id"] == document
                   and entry.get("source_sha256") == digest and engine in entry["engine_models"]]
        if len(entries) != 1:
            return []
        try:
            sources.extend(resolve_source(dict(entries[0], page=page), registry, root) for page in pages)
        except SourceUnavailable:
            return []
    return sources


def _diagnosis_applicability_message(question, machine, diagnosis):
    """Объясняет, когда выбранная машина требует сверки результата ML."""
    code = diagnosis.get("anomaly_type")
    engine = machine.get("engine_model")
    known_fuel = {"ЯМЗ-536": "дизель", "ЗМЗ-5234.10": "бензин"}
    fuel = known_fuel.get(engine) or (machine.get("fuel") or "").strip().casefold()
    # Экспертная таблица: строки 512, 525 и 536 для выданных руководств ЗМЗ.
    if code == "F09" and (code, engine) in _FACTORY_LAYOUTS:
        notice = ("ML выдал код F09. По руководству не удалось подтвердить, "
                  "что этот код применим к выбранному двигателю.")
        subject = r"наддув\w*|турбокомпрессор\w*|ткр"
    elif code == "F11" and engine == "ЯМЗ-236М2":
        notice = ("ML выдал код F11. В проверенных руководствах для ЯМЗ-236М2 применимость "
                  "проверки системы рециркуляции отработавших газов не подтверждена.")
        subject = r"рециркуляци\w*|egr|срог|рог"
    elif code == "F13" and fuel == "дизель":
        notice = "ML выдал код F13 — подача бензина. В карточке машины указан дизельный двигатель."
        subject = r"бензин\w*"
    elif code == "F08" and fuel == "бензин":
        notice = "ML выдал код F08 — дизельная топливоподача. В карточке машины указан бензиновый двигатель."
        subject = r"дизел\w*|топливоподач\w*|тнвд"
    elif code == "F12" and fuel == "дизель":
        notice = "ML выдал код F12 — зажигание. В карточке машины указан дизельный двигатель."
        subject = r"зажигани\w*|искр\w*|катушк\w*"
    else:
        return ""

    title = (diagnosis.get("anomaly_title") or "").casefold()
    focus = question.casefold().replace(title, "").strip(" .?!")
    focus = re.sub(r"\bв первую очередь\b", "", focus).strip()
    if not focus or generic_diagnostic_question(focus) or re.search(rf"\b(?:{code}|{subject})\b", question, re.I):
        return (f"{notice} Сверьте модель двигателя и убедитесь, "
                "что запись сделана на этой машине. Затем повторите проверку")
    return ""


def _ml_observation(diagnosis):
    description = diagnosis.get("description") or ""
    if diagnosis["status"] == "normal":
        lines = [f"Результат ML. Значимых отклонений по проверке не выявлено. {description}".strip()]
    else:
        title = diagnosis.get("anomaly_title") or "Предполагаемая неисправность"
        code = diagnosis.get("anomaly_type") or ""
        label = f"{code} — {title}" if code else title
        lines = [f"Гипотеза ML. {label}."]
        if description:
            lines.append(f"Признаки по ML. {description}")
        else:
            lines.append("В описании ML не удалось выделить признаки неисправности.")
    if diagnosis["signal_quality"] == "warning":
        lines.append("В записи есть помехи. Сверьте признаки при осмотре или повторите запись.")
    confidence = diagnosis.get("confidence")
    if diagnosis["status"] == "anomaly" and confidence is not None and confidence < 0.60:
        lines.append("Уверенность ML в предполагаемой неисправности низкая. Тип неисправности нужно подтвердить.")
    return lines


class ManualSession:
    """Хранит тему диалога по выбранной машине и проверке."""

    def __init__(self, *, root=None, data_dir="data", search=None,
                 model=None, diagnosis_provider=None, retriever=None):
        self.root = Path.cwd() if root is None else Path(root)
        self.data_dir = self.root / data_dir
        self.search = search
        self.retrieve = retriever or retrieve
        self.diagnosis_provider = diagnosis_provider
        self.machines = [_machine(item) for item in _records(self.data_dir / "raw/vehicles.json", "vehicles")]
        self.registry = _records(self.data_dir / "annotations/manuals.json")
        notes_path = self.data_dir / "annotations/page_notes.json"
        notes = _records(notes_path) if notes_path.is_file() else []
        self.procedures = [
            dict(p, document_id=note["document_id"], source_sha256=note["source_sha256"])
            for note in notes for p in note.get("passages", ()) if p.get("answer_terms")
        ]
        self.selection = None
        self.topic = ""

    def _reading_references(self, reply, backend, engine, variant, code):
        seen = {}
        rows = []
        try:
            entries = backend.reference_entries(engine, code, variant=variant)
        except (ApiException, OSError) as exc:
            raise SearchUnavailable("Не удалось открыть указатель источников. Проверьте соединение с Qdrant.") from exc
        for entry in entries:
            if not _applies(entry, engine, variant):
                continue
            sources = []
            try:
                for page in entry["source_pages"]:
                    key = (entry["document_id"], entry["source_sha256"], page)
                    if key not in seen:
                        seen[key] = resolve_source(dict(entry, page=page), self.registry, self.root)
                    sources.append(dict(seen[key]))
            except SourceUnavailable as exc:
                if str(exc) not in reply["warnings"]:
                    reply["warnings"].append(str(exc))
                continue
            if sources:
                rows.append(dict(row=entry["row"], section=entry["section"], level=entry["level"],
                                 sources=sources))
        reply["reading_references"] = rows
        reply["text"] = (f"Источники для {code}, {engine}. Строк в указателе: {len(rows)}."
                         if rows else f"Для {code} и {engine} в этом индексе нет доступных ссылок.")
        return reply

    def _find_machine(self, machine_id):
        return next((item for item in self.machines if item["vehicle_id"] == machine_id), None)

    def _require_check(self, machine, check_id):
        if not machine or not any(check["session_id"] == check_id for check in machine["history"]):
            raise ValueError("В истории выбранной машины нет этой проверки.")

    def diagnose(self, machine_id, check_id):
        """Запрашивает ML по явно выбранной паре машины и проверки."""
        machine = self._find_machine(machine_id)
        self._require_check(machine, check_id)
        provider = self.diagnosis_provider
        if provider is None:
            from .ml_client import fetch_diagnosis

            return _validated_result(fetch_diagnosis(None, machine_id, check_id), machine, check_id)
        return _validated_result(provider(machine_id, check_id), machine, check_id)

    def _diagnosis(self, machine, check_id):
        """Запрашивает ML для выбранной проверки перед обработкой её результата."""
        return self.diagnose(machine["vehicle_id"], check_id) if check_id is not None else None

    def ask(self, machine_id, question, *, check_id=None, diagnosis=None):
        """Отвечает по руководству и использует полученный ранее результат ML."""
        question = question.strip()
        if not question:
            raise ValueError("Введите вопрос по руководству.")
        machine = self._find_machine(machine_id)
        if machine is None:
            reply = _blank()
            reply["text"] = "Выберите машину из списка."
            return reply
        if check_id is not None or diagnosis is not None:
            self._require_check(machine, check_id)
        selection = (machine_id, check_id)
        if selection != self.selection:
            self.topic = ""
        self.selection = selection

        engine = machine.get("engine_model") or ""
        variant = machine.get("engine_variant") or None
        reply = _blank(engine, variant)
        lines = []
        if diagnosis is not None:
            diagnosis = _validated_result(diagnosis, machine, check_id)
        if reference := fuel_system_reference(self.registry, self.root, engine, question):
            self.topic = "fuel-system-reference"
            reply["text"], reply["sources"] = reference
            reply["synthesis"] = "extractive"
            return reply
        if reference := air_filter_reference(self.registry, self.root, engine, question, topic=self.topic):
            self.topic = AIR_FILTER_TOPIC
            reply["text"], reply["sources"], reply["needs_clarification"] = reference
            if reply["sources"]:
                reply["synthesis"] = "extractive"
            return reply
        if diagnosis is not None:
            if diagnosis["status"] == "check_failed" or diagnosis["signal_quality"] == "bad":
                self.topic = ""
                failure = "Сбой проверки." if diagnosis["status"] == "check_failed" else "Запись непригодна для диагностики."
                reply["text"] = failure + " Повторите запись."
                return reply
            lines.extend(_ml_observation(diagnosis))

        anomaly = diagnosis is not None and diagnosis["status"] == "anomaly"
        if not engine or not any(engine in item["engine_models"] for item in self.registry):
            lines.append("Для выбранной машины нет применимого руководства. Уточните модель и исполнение двигателя.")
            if anomaly:
                lines.append("Рекомендована дефектовка. Для заявки нужна подтверждённая процедура.")
            reply["text"] = "\n\n".join(lines)
            return reply

        if requests_references(question):
            if not anomaly:
                reply["text"] = ("Сначала получите результат диагностики для выбранной проверки."
                                 if diagnosis is None else "По этой проверке ML не выявил отклонения.")
                return reply
            backend = self.search if self.search is not None else configured_search()
            return self._reading_references(reply, backend, engine, variant, diagnosis["anomaly_type"])
        if anomaly and (message := _diagnosis_applicability_message(question, machine, diagnosis)):
            self.topic = ""
            reply["text"] = message
            code = diagnosis["anomaly_type"]
            if documents := _FACTORY_LAYOUTS.get((code, engine)):
                sources = _factory_layout_sources(self.registry, self.root, engine, documents)
                if sources:
                    name, finding = _FACTORY_SYSTEM_NOTICES[code]
                    manuals = "руководстве" if len(documents) == 1 else "руководствах"
                    refs = " ".join(f"[{index}]" for index in range(1, len(sources) + 1))
                    reply["sources"] = sources
                    reply["text"] = (
                        f"ML выдал код {code} «{name}».\n\n"
                        f"В штатной конструкции двигателя {engine}, описанной в {manuals}, {finding}. {refs}\n\n"
                        "Сверьте модель двигателя, убедитесь, что запись сделана на этой машине, и повторите проверку."
                    )
            return reply
        backend = self.search if self.search is not None else configured_search()
        result = self.retrieve(backend, engine, question, diagnosis=diagnosis, topic=self.topic,
                               variant=variant, procedures=self.procedures)
        self.topic = result.get("topic") or ""
        if result.get("manual_reference") and (result["evidence"] or result["needs_clarification"]):
            lines.clear()
        if self.topic == "egr-inspection" and not result["evidence"] and engine == "ЗМЗ-5234.10":
            add_catalog_egr_evidence(result, backend, engine, variant, self.registry, self.root)
        if self.topic in {"chain-phases", "chain-marks"}:
            complete_chain_sources(result, self.registry, self.root, engine)
        if self.topic.partition(":")[0] == CLUTCH_TOPIC:
            complete_clutch_sources(result, self.registry, self.root, engine)
        if self.topic == GEAR_TOPIC:
            add_gear_drive_source(result, self.registry, self.root, engine)
        if self.topic == VALVE_TOPIC and not re.search(r"руководств\w* по ремонту|\bпо РК\b", question, re.I):
            complete_valve_sources(result, self.registry, self.root, engine)
        if self.topic == "oil-indicator-test" and not result["evidence"]:
            add_oil_indicator_evidence(result, self.registry, self.root, engine)
        if self.topic.partition(":")[0] == ZMZ40904_OIL_TOPIC:
            complete_zmz40904_oil_sources(result, self.registry, self.root, engine, question)
        reply["queries"] = result["queries"]
        labels = {"fact": "По руководству", "general": "Общая проверка",
                  "cause": "Возможные причины по руководству", "risk": "Оценка риска"}
        numbers = {}
        evidence_lines = []
        for item in result["evidence"]:
            if not _applies(item, engine, variant):
                continue
            try:
                source = resolve_source(item, self.registry, self.root)
            except SourceUnavailable as exc:
                warning = str(exc)
                if warning not in reply["warnings"]:
                    reply["warnings"].append(warning)
                continue
            key = (source["document_id"], source["page"])
            if key not in numbers:
                reply["sources"].append(source)
                numbers[key] = len(reply["sources"])
            if item.get("source_origin"):
                reply["sources"][numbers[key] - 1]["source_origin"] = item["source_origin"]
            if item.get("expert_rows"):
                reference = reply["sources"][numbers[key] - 1]
                reference["fault_code"] = item["fault_code"]
                reference["expert_rows"] = sorted(set(reference.get("expert_rows", ())) | set(item["expert_rows"]))
            if item.get("work") or item.get("lamp"):
                reply["inspection_steps"].append(dict(document_id=key[0], page=key[1],
                                                      role="lamp" if item.get("lamp") else "work",
                                                      text=item.get("work") or item["text"]))
            evidence_lines.append(f"{labels.get(item['kind'], labels['fact'])}. {item['text']} [{numbers[key]}]")

        if result.get("missing"):
            lines.append(result["missing"])
        needs_clarification = result.get("needs_clarification", False)
        if needs_clarification:
            reply["needs_clarification"] = True
        if result.get("clarification") and (evidence_lines or needs_clarification):
            lines.append(result["clarification"])
        if evidence_lines:
            reply["synthesis"] = "extractive"
            lines.extend(evidence_lines)
        elif reply["warnings"]:
            lines.append("Источник недоступен для сверки. Проверьте файлы руководств.")
        elif not result.get("missing") and not needs_clarification:
            if anomaly and result.get("asks_about_fault"):
                lines.append(f"В руководстве для {engine} не нашлось процедуры проверки гипотезы ML.")
            else:
                lines.append(f"В руководстве для {engine} я не нашёл ответа на этот вопрос.")
        if not evidence_lines and not variant and not needs_clarification and any(
                fragment in question.casefold() for fragment in ("комплектац", "исполнен")):
            lines.append("Уточните исполнение двигателя по табличке, чтобы подобрать процедуру.")
            reply["needs_clarification"] = True
        if (anomaly and not needs_clarification and not self.topic.startswith("clutch-noise")
                and (result.get("asks_about_fault") or result.get("inspection_missing"))):
            if not any(step["role"] == "work" for step in reply["inspection_steps"]):
                lines.append("Рекомендована дефектовка. Для заявки нужна применимая работа со ссылкой на руководство.")
        reply["text"] = "\n\n".join(lines)
        return reply


def _print_reply(reply, trace):
    print(reply["text"])
    for i, source in enumerate(reply["sources"], 1):
        print(f"[{i}] {source['title']}, {source['edition']}, PDF стр. {source['page']}\n    {source['url']}")
    for entry in reply.get("reading_references", []):
        print(f"Строка {entry['row']}. {entry['section']}")
        for source in entry["sources"]:
            print(f"    {source['title']}, PDF {source['page']}: {source['url']}")
    for warning in reply["warnings"]:
        print(warning, file=sys.stderr)
    if trace:
        print("\nЗапросы:", " | ".join(reply["queries"]))
        print("Режим ответа:", reply["synthesis"])


def main(argv=None):
    from .ml_client import fetch_diagnosis, MLUnavailable

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--machine", required=True, help="vehicle_id из data/raw/vehicles.json")
    parser.add_argument("--check", help="session_id для отдельного запроса диагностики")
    parser.add_argument("--question", required=True, help="вопрос своими словами")
    parser.add_argument("--follow-up", action="append", default=[], help="уточнение в том же диалоге")
    parser.add_argument("--data", default="data", help="каталог реестра и данных машин")
    parser.add_argument("--ml-url", help="адрес ML, по умолчанию MOCK_ML_URL")
    add_search_arguments(parser)
    parser.add_argument("--open-source", type=int, metavar="N", help="открыть источник [N] первого ответа")
    parser.add_argument("--trace", action="store_true", help="показать поисковые запросы и режим ответа")
    parser.add_argument("--json", action="store_true", help="вывести массив ответов в JSON")
    args = parser.parse_args(argv)
    try:
        session = ManualSession(
            data_dir=args.data, search=configured_search(args.qdrant_url, args.collection, args.search_mode),
            diagnosis_provider=lambda vehicle, check: fetch_diagnosis(args.ml_url, vehicle, check),
        )
        diagnosis = session.diagnose(args.machine, args.check) if args.check else None
        replies = []
        for number, question in enumerate([args.question, *args.follow_up]):
            reply = session.ask(args.machine, question, check_id=args.check, diagnosis=diagnosis)
            replies.append(reply)
            if not args.json:
                print(f"\nВопрос: {question}")
                _print_reply(reply, args.trace)
            if number == 0 and args.open_source is not None:
                if not 1 <= args.open_source <= len(reply["sources"]):
                    raise ValueError("В первом ответе нет источника с таким номером.")
                webbrowser.open_new_tab(reply["sources"][args.open_source - 1]["url"])
        if args.json:
            print(json.dumps(replies, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, SearchUnavailable, MLUnavailable) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
