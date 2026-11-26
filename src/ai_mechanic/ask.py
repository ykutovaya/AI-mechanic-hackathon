"""Ответ по руководству выбранной машины. Запуск через python -m ai_mechanic.ask."""

import argparse
import json
import webbrowser
from pathlib import Path
import sys

from .knowledge.search import SearchUnavailable, add_search_arguments, configured_search, retrieve
from .knowledge.sources import SourceUnavailable, resolve_source


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
    return result


def _applies(hit, engine, variant):
    if engine not in hit["engine_models"]:
        return False
    allowed = {f"{engine}|all"}
    if variant:
        allowed.add(f"{engine}|{variant}")
    return bool(allowed.intersection(hit.get("applicability", ())))


def _ml_observation(diagnosis):
    description = diagnosis.get("description") or ""
    if diagnosis["status"] == "normal":
        lines = [f"Результат ML. Значимых отклонений по проверке не выявлено. {description}".strip()]
    else:
        title = diagnosis.get("anomaly_title") or "Предполагаемая неисправность"
        code = diagnosis.get("anomaly_type") or ""
        label = f"{code} — {title}" if code else title
        lines = [f"Гипотеза ML. {label}.", f"Признаки по ML. {description}".strip()]
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

        backend = self.search if self.search is not None else configured_search()
        result = self.retrieve(backend, engine, question, diagnosis=diagnosis, topic=self.topic,
                               variant=variant, procedures=self.procedures)
        self.topic = result.get("topic") or ""
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
        if anomaly and (result.get("asks_about_fault") or result.get("inspection_missing")):
            if not any(step["role"] == "work" for step in reply["inspection_steps"]):
                lines.append("Рекомендована дефектовка. Для заявки нужна применимая работа со ссылкой на руководство.")
        reply["text"] = "\n\n".join(lines)
        return reply


def _print_reply(reply, trace):
    print(reply["text"])
    for i, source in enumerate(reply["sources"], 1):
        print(f"[{i}] {source['title']}, {source['edition']}, PDF стр. {source['page']}\n    {source['url']}")
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
