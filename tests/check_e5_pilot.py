"""Проверяет поиск E5, отбор выдержек и ответы конкурсного веб-чата."""

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from hashlib import file_digest
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from time import perf_counter
from unittest.mock import patch
from urllib.parse import quote
from urllib.request import Request, urlopen

from qdrant_client.http.exceptions import ApiException

from ai_mechanic import ask, web
from ai_mechanic.knowledge._qdrant_store import QdrantSearch
from ai_mechanic.knowledge.search import add_search_arguments


def _key(source):
    return source["document_id"], source["page"]


def _source(key):
    return dict(document_id=key[0], page=key[1])


def _clean(text):
    joined = re.sub(r"(?<=\w)-[ \t]*\r?\n\s*(?=\w)", "", text)
    return " ".join(joined.casefold().replace("ё", "е").split())


def _matches(text, terms):
    for term in terms:
        term = _clean(term)
        before = r"(?<!\w)(?<!\d[,.])" if term[0].isdigit() else r"(?<!\w)" if term[0].isalnum() else ""
        after = r"(?!\w)" if term[-1].isalnum() else ""
        if not re.search(before + re.escape(term) + after, text):
            return False
    return True


def _score_passages(fact, passages):
    sources = {_key(ref) for ref in fact["sources"]}
    return any(ref in sources and _matches(text, fact["terms"]) for ref, text in passages)


def _candidate_passages(hits):
    for hit in hits:
        ref = _key(hit)
        condition = hit.get("condition") or ""
        yield ref, _clean(f"{condition} {hit['text']}")
        for table in hit.get("tables", ()):
            rows = table.get("rows", ())
            if rows:
                header = " | ".join(map(str, rows[0]))
                for row in rows[1:]:
                    yield ref, _clean(f"{condition} {header} | " + " | ".join(map(str, row)))


def _links(ref):
    doc, page = _key(ref)
    base = f"/manuals/{quote(doc, safe='')}"
    return f"{base}.pdf#page={page}", f"{base}/page/{page}"


def _cited_passages(reply):
    refs = reply["sources"]
    cited = []
    errors = []
    for para in reply["text"].split("\n\n"):
        for text, labels in re.findall(r"([^\[\]]+?)\s*((?:\[[1-9]\d*\]\s*)+)", para):
            for marker in re.findall(r"\d+", labels):
                index = int(marker) - 1
                if index >= len(refs):
                    errors.append(f"Ссылка [{marker}] без документа")
                else:
                    cited.append((_key(refs[index]), _clean(text)))
    for ref in refs:
        expected_pdf, expected_page = _links(ref)
        if (ref.get("url"), ref.get("page_url")) != (expected_pdf, expected_page):
            errors.append(f"Неверные ссылки на {ref['document_id']}, PDF {ref['page']}")
    return cited, errors


def score_case(case, lookups, excerpts, reply):
    """Отдельно оценивает выдачу, выбранный текст и факты со ссылками в ответе."""
    facts = case["facts"]
    gold = list(dict.fromkeys(_key(ref) for fact in facts for ref in fact["sources"]))
    primary = lookups[0] if lookups else []
    pages = list(dict.fromkeys(_key(hit) for hit in primary if hit["text"].strip() or hit.get("tables")))
    top5 = pages[:5]
    candidates = list(_candidate_passages(hit for group in lookups for hit in group))
    selected = [(_key(item), _clean(f"{item.get('condition') or ''} {item['excerpt']}")) for item in excerpts]
    cited, link_errors = _cited_passages(reply)
    checks = [
        dict(fact, in_candidates=_score_passages(fact, candidates),
             in_excerpts=_score_passages(fact, selected), in_answer=_score_passages(fact, cited))
        for fact in facts
    ]
    hit5 = all(key in top5 for key in gold) if gold else None
    failures = []
    if hit5 is False:
        failures.append("page_outside_top5")
    for check in checks:
        if not check["in_excerpts"]:
            failures.append("fact_dropped_by_selection" if check["in_candidates"] else "fact_missing_in_search")
        elif not check["in_answer"]:
            failures.append("fact_missing_in_answer")
    if link_errors or reply.get("warnings"):
        failures.append("source_error")

    forbidden = {_key(ref) for ref in case.get("forbidden_sources", [])}
    observed = {_key(hit) for group in lookups for hit in group}
    observed.update(_key(ref) for ref in [*excerpts, *reply["sources"]])
    foreign_pages = sorted(forbidden & observed)
    if foreign_pages:
        failures.append("inapplicable_source")

    refusal = None
    if case.get("insufficient_evidence"):
        text = _clean(reply["text"])
        lacks_data = re.search(r"не (?:нашлось|нашел|найден\w*|удалось подтвердить)|нет (?:данных|сведений|применимого)", text)
        refusal = bool(lacks_data and "рекомендована дефектовка" in text and not (reply["sources"] or excerpts))
        if not refusal:
            failures.append("expected_refusal_missing")

    return {
        "search": {"top5": [_source(ref) for ref in top5], "hit5": hit5,
                   "ranks": [dict(_source(ref), rank=pages.index(ref) + 1 if ref in pages else None) for ref in gold],
                   "all_facts": all(check["in_candidates"] for check in checks) if checks else None},
        "selection": {"excerpts": excerpts,
                      "all_facts": all(check["in_excerpts"] for check in checks) if checks else None},
        "answer": {"text": reply["text"], "sources": reply["sources"], "refusal_ok": refusal,
                   "all_facts": all(check["in_answer"] for check in checks) if checks else None,
                   "link_errors": link_errors, "warnings": reply.get("warnings", [])},
        "inapplicable_sources": [_source(ref) for ref in foreign_pages],
        "facts": checks, "failures": list(dict.fromkeys(failures)),
    }


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _valid_sources(refs):
    return isinstance(refs, list) and all(
        isinstance(ref, dict) and _nonempty(ref.get("document_id"))
        and type(ref.get("page")) is int and ref["page"] > 0 for ref in refs
    )


def validate_cases(cases, machines):
    """Проверяет эталон и принадлежность каждой проверки выбранной машине."""
    if not isinstance(cases, list) or not cases:
        raise ValueError("Ожидается непустой список вопросов.")
    ids = set()
    for case in cases:
        if not isinstance(case, dict) or not all(_nonempty(case.get(field))
                                                 for field in ("id", "vehicle_id", "session_id", "question")):
            raise ValueError("В каждом вопросе нужны id, vehicle_id, session_id и question.")
        key, vehicle, session = case["id"], case["vehicle_id"], case["session_id"]
        if key in ids:
            raise ValueError(f"Повторяющийся вопрос {key}.")
        ids.add(key)
        machine = machines.get(vehicle)
        if not machine or not any(check["check_id"] == session for check in machine["history"]):
            raise ValueError(f"{key}: проверка {session} не найдена у машины {vehicle}.")
        if not all(type(case.get(flag, False)) is bool for flag in ("diagnose", "insufficient_evidence")):
            raise ValueError(f"{key}: флаги diagnose и insufficient_evidence должны быть логическими.")
        facts = case.get("facts")
        if not isinstance(facts, list) or bool(facts) == case.get("insufficient_evidence", False):
            raise ValueError(f"{key}: укажите факты или ожидаемый отказ из-за нехватки данных.")
        for fact in facts:
            if not isinstance(fact, dict):
                raise ValueError(f"{key}: ожидается объект факта.")
            terms, sources = fact.get("terms"), fact.get("sources")
            if not isinstance(terms, list) or not terms or not all(_nonempty(term) for term in terms):
                raise ValueError(f"{key}: укажите непустой список строк terms.")
            if not sources or not _valid_sources(sources):
                raise ValueError(f"{key}: укажите документ и физическую страницу для каждого факта.")
        forbidden = case.get("forbidden_sources", [])
        if not _valid_sources(forbidden):
            raise ValueError(f"{key}: проверьте документ и физическую страницу в forbidden_sources.")
        positives = {_key(ref) for fact in facts for ref in fact["sources"]}
        if positives.intersection(map(_key, forbidden)):
            raise ValueError(f"{key}: одна страница одновременно указана как ожидаемая и неприменимая.")


class RecordingSearch(QdrantSearch):
    """Записывает страницы каждой формулировки, реально использованной в чате."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lookups = []

    def search(self, engine, query, *, variant=None, limit=12):
        hits = super().search(engine, query, variant=variant, limit=limit)
        self.lookups.append((query, hits))
        return hits


@contextmanager
def chat_server(backend, data, ml_url, excerpts):
    """Поднимает обычный веб-чат на свободном порту с временной базой."""
    retrieve = ask.retrieve

    def capture(*args, **kwargs):
        result = retrieve(*args, **kwargs)
        for item in result["evidence"]:
            excerpt = {key: item[key] for key in ("document_id", "page", "excerpt")}
            if item.get("condition"):
                excerpt["condition"] = item["condition"]
            excerpts.append(excerpt)
        return result

    with TemporaryDirectory() as directory, patch.object(ask, "retrieve", capture):
        with web.make_server(("127.0.0.1", 0), search=backend, data_dir=data, ml_url=ml_url,
                             orders_db=Path(directory) / "orders.sqlite") as server:
            worker = Thread(target=server.serve_forever)
            worker.start()
            try:
                yield server
            finally:
                server.shutdown()
                worker.join()


def _post(base, path, payload):
    request = Request(base + path, data=json.dumps(payload, ensure_ascii=False).encode(),
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=120) as response:
        return json.load(response)


def _check_page(base, ref):
    expected_pdf, page_url = _links(ref)
    if (ref.get("url"), ref.get("page_url")) != (expected_pdf, page_url):
        return False
    try:
        with urlopen(base + page_url, timeout=15) as response:
            if f"Страница {ref['page']} в PDF" not in response.read().decode("utf-8"):
                return False
        with urlopen(base + page_url + ".png", timeout=15) as response:
            return response.headers.get_content_type() == "image/png" and response.read(8) == b"\x89PNG\r\n\x1a\n"
    except (OSError, UnicodeError):
        return False


def _evaluate_case(case, server, backend, excerpts, verified_pages):
    workshop = server.workshop
    machine = workshop.machines[case["vehicle_id"]]
    base = f"http://127.0.0.1:{server.server_port}"
    # Один сценарий не наследует результат ML другого, даже если пара совпадает.
    with workshop.diagnostics_lock:
        workshop.diagnostics.clear()
    payload = dict(machine_id=case["vehicle_id"], check_id=case["session_id"])
    if case.get("diagnose"):
        _post(base, "/api/diagnose", payload)
    backend.lookups.clear()
    excerpts.clear()

    start = perf_counter()
    reply = _post(base, "/api/ask", payload | {"question": case["question"]})
    elapsed = round((perf_counter() - start) * 1000, 2)
    lookups = [hits for _, hits in backend.lookups]
    row = score_case(case, lookups, list(excerpts), reply)
    row["search"]["queries"] = [query for query, _ in backend.lookups]
    engine, variant = machine["engine_model"], machine.get("engine_variant")
    allowed = {f"{engine}|all"}
    if variant:
        allowed.add(f"{engine}|{variant}")
    if any(not allowed.intersection(hit["applicability"]) for group in lookups for hit in group):
        row["failures"].append("wrong_engine_or_variant")
    diagnosis = reply.get("diagnosis") or {}
    if case.get("diagnose"):
        expected_pair = case["vehicle_id"], case["session_id"]
        actual_pair = diagnosis.get("vehicle_id"), diagnosis.get("session_id")
        expected_anomaly = case.get("insufficient_evidence") and diagnosis.get("status") != "anomaly"
        if actual_pair != expected_pair or expected_anomaly:
            row["failures"].append("diagnosis_mismatch")
    elif diagnosis:
        row["failures"].append("unexpected_diagnosis")
    if (reply["engine_model"], reply.get("engine_variant")) != (engine, variant):
        row["failures"].append("answer_engine_mismatch")
    opened = []
    for ref in reply["sources"]:
        key = _key(ref)
        if key not in verified_pages:
            verified_pages[key] = _check_page(base, ref)
        opened.append(dict(_source(key), ok=verified_pages[key]))
    row["answer"]["opened_pages"] = opened
    if any(not item["ok"] for item in opened):
        row["failures"].append("source_http_error")
    return {**row, "id": case["id"], "question": case["question"], "vehicle_id": case["vehicle_id"],
            "session_id": case["session_id"], "engine_model": engine, "engine_variant": variant,
            "diagnosis_status": diagnosis.get("status"), "elapsed_ms": elapsed}


def _sha(path):
    with path.open("rb") as source:
        return file_digest(source, "sha256").hexdigest()


def _physical(backend, collection):
    aliases = {item.alias_name: item.collection_name for item in backend.client.get_aliases().aliases}
    return aliases.get(collection, collection)


def evaluate(options):
    """Проверяет одну физическую версию коллекции и сохраняет привязки прогона."""
    cases = json.loads(options.cases.read_text(encoding="utf-8-sig"))
    inputs = [options.cases, options.data / "raw/vehicles.json", options.data / "annotations/manuals.json",
              options.data / "annotations/page_notes.json"]
    input_hashes = {str(path): _sha(path) for path in inputs if path.is_file()}
    verified, excerpts = {}, []
    with closing(RecordingSearch(url=options.qdrant_url, collection=options.collection,
                                 mode=options.search_mode)) as backend:
        physical = _physical(backend, options.collection)
        backend.collection = physical
        metadata = backend.client.get_collection(physical).config.metadata or {}
        count = backend.client.count(physical, exact=True).count
        if not count:
            raise ValueError(f"Коллекция {physical} пуста.")
        with chat_server(backend, options.data, options.ml_url, excerpts) as server:
            validate_cases(cases, server.workshop.machines)
            rows = [_evaluate_case(case, server, backend, excerpts, verified)
                    for case in cases]
        if physical != _physical(backend, options.collection) or count != backend.client.count(physical, exact=True).count:
            raise ValueError("Во время прогона изменилась коллекция. Повторите проверку.")
    code = sorted(Path("src/ai_mechanic/knowledge").glob("*.py"))
    code.extend(Path("src/ai_mechanic") / name for name in ("ask.py", "web.py"))
    code.append(Path(__file__).resolve().relative_to(Path.cwd()))
    return {"utc": datetime.now(timezone.utc).isoformat(), "alias": options.collection,
            "collection": physical, "points": count, "encoder_signature": metadata.get("encoder_signature"),
            "mode": options.search_mode, "opened_pdf_pages": sum(verified.values()), "inputs": input_hashes,
            "code_sha256": {path.as_posix(): _sha(path) for path in code}, "rows": rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_search_arguments(parser)
    parser.add_argument("--cases", type=Path, required=True, help="JSON со сценариями проверки")
    parser.add_argument("--output", type=Path, required=True, help="путь отчёта JSON")
    parser.add_argument("--data", type=Path, default=Path("data"), help="каталог данных и разметки")
    parser.add_argument("--ml-url", help="адрес ML вместо MOCK_ML_URL")
    options = parser.parse_args(argv)
    try:
        result = evaluate(options)
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (ValueError, OSError, ApiException) as exc:
        print(f"Ошибка проверки: {exc}", file=sys.stderr)
        return 2

    rows = result["rows"]
    passed = sum(not row["failures"] for row in rows)
    for row in rows:
        print(f"{row['id']}: {', '.join(row['failures']) or 'пройдено'}")
    print(f"{result['alias']} — {result['points']} фрагментов, пройдено {passed}/{len(rows)}, "
          f"открыто страниц: {result['opened_pdf_pages']}. {options.output}")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
