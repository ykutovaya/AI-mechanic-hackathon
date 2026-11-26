"""Извлечение одного PDF и обновление общего файла pages.jsonl."""

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

from filelock import FileLock

from ._notes import apply_notes, apply_review


def _records(path):
    records = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        raise ValueError(f"{path}: нужен массив объектов JSON")
    return records


def _metadata(pdf, registry):
    entries = _records(registry)
    keys = [entry["document_id"] for entry in entries]
    if len(set(keys)) != len(keys):
        raise ValueError(f"{registry}: повтор идентификатора документа")
    matches = [entry for entry in entries if Path(entry["path"]).resolve() == pdf.resolve()]
    if len(matches) != 1:
        raise ValueError(f"{pdf}: нужен один документ с таким путём в {registry}")
    entry = matches[0]
    for field in ("document_id", "title", "edition", "path"):
        if not isinstance(entry[field], str) or not entry[field].strip():
            raise ValueError(f"{registry}: заполните {field}")
    models = entry["engine_models"]
    if not isinstance(models, list) or not models or not all(isinstance(m, str) and m.strip() for m in models):
        raise ValueError(f"{registry}: укажите список моделей в engine_models")
    return entry


def _old_records(path):
    if path.exists():
        with path.open(encoding="utf-8-sig") as source:
            for line in source:
                if line.strip():
                    yield line, json.loads(line)


def _update(output, rows, notes):
    """Под блокировкой заменяет записи одного документа целиком."""
    document_id = rows[0]["document_id"]
    previous_ocr = {}
    previous_notes = []
    output.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(output) + ".lock", timeout=120):
        temp = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                          dir=output.parent, delete=False)
        try:
            with temp:
                for line, old in _old_records(output):
                    if old["document_id"] != document_id:
                        temp.write(line.rstrip("\r\n") + "\n")
                    elif old.get("source_sha256") == rows[0]["source_sha256"]:
                        if old.get("ocr"):
                            previous_ocr[old["page"]] = old["ocr"]
                        if "page_note" in old:
                            previous_notes.append(old["page_note"])
                    elif notes is None and "page_note" in old:
                        raise ValueError("PDF изменился. Передайте разметку текущей редакции через --notes")
                selected = previous_notes if notes is None else notes
                apply_notes(rows, selected)
                by_page = {note["page"]: note for note in selected}
                for row in rows:
                    prior = previous_ocr.get(row["page"])
                    if prior and not row.get("ocr"):
                        row["ocr"] = {k: v for k, v in prior.items()
                                      if k not in {"raw_text", "engine_models"}}
                        row["ocr"].update(text=prior.get("raw_text", prior["text"]), status="pending")
                    if row["page"] in by_page:
                        row["page_note"] = by_page[row["page"]]
                        apply_review(row, row["page_note"])
                    temp.write(json.dumps(row, ensure_ascii=False) + "\n")
            os.replace(temp.name, output)
        finally:
            Path(temp.name).unlink(missing_ok=True)


def process_manual(pdf, registry, output, *, notes=None, ocr_pages=(), ocr_language="rus",
                   ocr_rotate=0, ocr_auto=False, reader=None):
    """Читает PDF из реестра и возвращает число сохранённых физических страниц."""
    pdf, output = Path(pdf), Path(output)
    if output.suffix.lower() != ".jsonl":
        raise ValueError("Укажите файл результата с расширением .jsonl")
    entry = _metadata(pdf, registry)
    with pdf.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    metadata = {k: entry[k] for k in ("document_id", "title", "edition", "engine_models", "path")}
    metadata["source_sha256"] = digest
    if reader is None:
        from ._extract import iter_pages
        reader = iter_pages
    options = dict(ocr_pages=ocr_pages, ocr_language=ocr_language, ocr_rotate=ocr_rotate)
    if ocr_auto:
        options["ocr_auto"] = True
    rows = [dict(metadata, **page) for page in reader(pdf, **options)]
    if not rows or any(row["page"] != n for n, row in enumerate(rows, 1)):
        raise ValueError(f"{pdf}: нарушена физическая нумерация страниц")
    if entry.get("page_count", len(rows)) != len(rows):
        raise ValueError(f"{pdf}: в реестре {entry['page_count']} стр., в файле {len(rows)}")
    selected = None
    if notes is not None:
        selected = [n for n in _records(notes) if n["document_id"] == entry["document_id"]]
    _update(output, rows, selected)
    return len(rows)


def _ocr_pages(specs, pdf):
    tokens = [token.strip() for spec in specs for token in spec.split(",")]
    auto = "auto" in tokens
    explicit = [token for token in tokens if token != "auto"]
    if not explicit:
        return [], auto
    import pymupdf
    with pymupdf.open(pdf) as doc:
        total = len(doc)
    pages = set()
    for token in explicit:
        if token == "all":
            pages.update(range(1, total + 1))
            continue
        lo, sep, hi = token.partition("-")
        first, last = int(lo), int(hi if sep else lo)
        if not 1 <= first <= last <= total:
            raise ValueError(f"Страницы OCR должны находиться в диапазоне 1–{total}: {token}")
        pages.update(range(first, last + 1))
    return sorted(pages), auto

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="путь к одному PDF, как в реестре")
    parser.add_argument("--registry", required=True, type=Path, help="реестр руководств JSON")
    parser.add_argument("--output", required=True, type=Path, help="общий файл pages.jsonl")
    parser.add_argument("--notes", type=Path, help="разметка разделов, применимости и результатов сверки")
    parser.add_argument("--ocr-page", action="append", default=[],
                        help="auto, all или физические страницы (например, 5,7-9). Варианты можно сочетать.")
    parser.add_argument("--ocr-language", default="rus", help="код языка Tesseract, например rus или rus+eng")
    parser.add_argument("--ocr-rotate", type=int, choices=[0, 90, 180, 270], default=0,
                        help="поворот изображения по часовой стрелке перед OCR")
    args = parser.parse_args(argv)
    try:
        pages, auto = _ocr_pages(args.ocr_page, args.pdf)
        count = process_manual(args.pdf, args.registry, args.output, notes=args.notes,
                               ocr_pages=pages, ocr_auto=auto,
                               ocr_language=args.ocr_language, ocr_rotate=args.ocr_rotate)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    print(f"{args.pdf.name} — {count} стр. Файл: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
