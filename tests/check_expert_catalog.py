"""Сверяет все доступные строки отчёта импорта с указателем настоящего веб-чата."""
import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
from time import monotonic
from urllib.request import Request, urlopen

from ai_mechanic.knowledge._qdrant_store import QdrantSearch
from check_e5_pilot import _check_page, _links, _physical, _post, _sha, chat_server


def compare_rows(expected, actual):
    """Проверяет номер строки, уровень доказательности, документ и все страницы."""
    seen, valid, errors = set(), set(), set()
    for row in actual:
        number = row["row"]
        if number not in expected:
            errors.add(f"unexpected:{number}")
            continue
        if number in seen:
            errors.add(f"duplicate:{number}")
            valid.discard(number)
            continue
        seen.add(number)
        doc, pages, level = expected[number]
        refs = sorted((source["document_id"], source["page"]) for source in row["sources"])
        if level == row["level"] and refs == [(doc, page) for page in pages]:
            valid.add(number)
        else:
            errors.add(f"source_mismatch:{number}")
    errors.update(f"missing:{n}" for n in expected.keys() - seen)
    return valid, sorted(errors)


def expected_rows(rows):
    """Задаёт ожидаемую видимость по отчёту импорта, включая исключения."""
    groups, excluded = defaultdict(dict), defaultdict(list)
    for row in rows:
        if row["result"] == "bound":
            level, pages = "bound", row["source_pages"]
        elif row.get("routes"):
            level, pages = "route", {item["page"] for item in row["routes"]}
        elif row.get("reading_reference"):
            level, pages = "reading", row["reading_reference"]["source_pages"]
        else:
            excluded[row["result"]].append(row["row"])
            continue
        key = (row["code"], row["engine_model"], row["variant"] or "")
        groups[key][row["row"]] = (row["document_id"], tuple(sorted(pages)), level)
    return groups, excluded


def actual_checks(vehicles):
    """Выбирает одну пригодную проверку организаторов на код, модель и исполнение."""
    checks = {}
    for vehicle in vehicles:
        for session in vehicle["sessions"]:
            if session["status"] == "anomaly" and session["signal_quality"] != "bad":
                key = (session["anomaly_type"], vehicle["engine_model"], vehicle.get("engine_variant") or "")
                checks.setdefault(key, dict(machine_id=vehicle["vehicle_id"], check_id=session["session_id"]))
    return checks


def open_original(base, path):
    """Проверяет, что ссылка ведёт к исходному PDF."""
    request = Request(base + path, headers={"Range": "bytes=0-4"})
    with urlopen(request, timeout=20) as response:
        return response.headers.get_content_type() == "application/pdf" and response.read(5) == b"%PDF-"


def evaluate(options):
    started = monotonic()
    report = json.loads(options.report.read_text(encoding="utf-8-sig"))
    if report["workbook_sha256"] != _sha(options.workbook):
        raise ValueError("Отчёт импорта относится к другой версии книги.")
    vehicles_path = options.data / "raw/vehicles.json"
    groups, excluded = expected_rows(report["rows"])
    checks = actual_checks(json.loads(vehicles_path.read_text(encoding="utf-8-sig"))["vehicles"])
    pages_opened, pdfs_opened, accepted, cited, scenarios = {}, {}, set(), set(), []
    with closing(QdrantSearch(url=options.qdrant_url, collection=options.collection)) as backend:
        physical = _physical(backend, options.collection)
        backend.collection = physical
        metadata = backend.client.get_collection(physical).config.metadata or {}
        with chat_server(backend, options.data, options.ml_url, []) as server:
            base = f"http://127.0.0.1:{server.server_port}"
            for (code, engine, variant), case in sorted(checks.items()):
                expected = groups.get((code, engine, ""), {}) | groups.get((code, engine, variant), {})
                diagnosis = _post(base, "/api/diagnose", case)
                reply = _post(base, "/api/ask", case | {"question": "Покажи источники по этой неисправности"})
                actual = reply.get("reading_references", [])
                valid, errors = compare_rows(expected, actual)
                for response in (diagnosis, reply):
                    ml = response.get("diagnosis") or {}
                    if (ml.get("anomaly_type"), ml.get("vehicle_id"), ml.get("session_id")) != (
                            code, case["machine_id"], case["check_id"]):
                        errors.append("wrong_ml_result")
                    if (response["engine_model"], response.get("engine_variant") or "") != (engine, variant):
                        errors.append("wrong_applicability")
                if reply["sources"]:
                    errors.append("catalog_as_repair_evidence")
                for row in actual:
                    for source in row["sources"]:
                        doc, page = source["document_id"], source["page"]
                        pdf_url, page_url = _links(source)
                        if (source.get("url"), source.get("page_url")) != (pdf_url, page_url):
                            valid.discard(row["row"])
                            errors.append(f"invalid_link:{row['row']}")
                            continue
                        if doc not in pdfs_opened:
                            pdfs_opened[doc] = open_original(base, pdf_url.partition("#")[0])
                        if (doc, page) not in pages_opened:
                            pages_opened[(doc, page)] = _check_page(base, source)
                        if not pdfs_opened[doc] or not pages_opened[(doc, page)]:
                            valid.discard(row["row"])
                            errors.append(f"failed_pdf:{doc}/{page}")
                cited_here = set()
                for source in diagnosis["sources"]:
                    for row in source.get("expert_rows", ()):
                        if (row not in expected or source.get("fault_code") != code
                                or expected[row][0] != source["document_id"]
                                or source["page"] not in expected[row][1] or expected[row][2] == "reading"):
                            errors.append(f"invalid_diagnostic_citation:{row}")
                        else:
                            cited_here.add(row)
                if "wrong_ml_result" in errors or "wrong_applicability" in errors:
                    valid.clear()
                    cited_here.clear()
                accepted.update(valid)
                cited.update(cited_here)
                scenarios.append(dict(code=code, engine=engine, variant=variant, **case,
                                      catalog_rows=sorted(valid), diagnosis_cited_rows=sorted(cited_here),
                                      errors=sorted(set(errors))))
        if physical != _physical(backend, options.collection):
            raise ValueError("Псевдоним Qdrant изменился во время проверки.")
    expected = {n: entry for group in groups.values() for n, entry in group.items()}
    return dict(utc=datetime.now(timezone.utc).isoformat(), elapsed_seconds=round(monotonic() - started, 1),
                alias=options.collection, physical_collection=physical, question="Покажи источники по этой неисправности",
                encoder=metadata.get("encoder_signature"), workbook_sha256=report["workbook_sha256"],
                import_report_sha256=_sha(options.report), vehicles_sha256=_sha(vehicles_path),
                workbook_rows=len(report["rows"]), expected_catalog_rows=len(expected),
                verified_catalog_rows=len(accepted), verified_by_level=dict(Counter(expected[n][2] for n in accepted)),
                unavailable_in_chat=sorted(expected.keys() - accepted), excluded_by_import=excluded,
                distinct_rows_cited_in_diagnoses=len(cited), physical_pages_opened=sum(pages_opened.values()),
                pdf_files_opened=sum(pdfs_opened.values()), scenarios=scenarios)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--report", type=Path, default=Path("data/processed/expert_sources_chat_report.json"))
    parser.add_argument("--workbook", type=Path, default=Path("data/annotations/defect_sources.xlsx"))
    parser.add_argument("--output", type=Path, default=Path("data/processed/expert_chat_coverage.json"))
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6335")
    parser.add_argument("--collection", default="mechanic_hackathon_expert_chat")
    parser.add_argument("--ml-url", default="http://127.0.0.1:8001")
    args = parser.parse_args()
    result = evaluate(args)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Доступно через HTTP {result['verified_catalog_rows']} из {result['expected_catalog_rows']} строк."
          f" Открыто страниц PDF: {result['physical_pages_opened']}.")
    return int(bool(result["unavailable_in_chat"] or any(row["errors"] for row in result["scenarios"])))


if __name__ == "__main__":
    raise SystemExit(main())
