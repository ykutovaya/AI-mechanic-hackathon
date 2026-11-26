"""Создание реестра шести руководств из раздела 3 ТЗ НейроДВС v6."""

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import pymupdf


MANUALS = {
    "ЯМЗ_236_руководство по эксплуатации.pdf": {
        "document_id": "yamz-236m2-re-2010",
        "title": "236-3902150-Б РЭ",
        "edition": "2010",
        "engine_models": ["ЯМЗ-236М2"],
        "page_count": 172,
    },
    "ЯМЗ_236_руководство по РЕМОНТУ.pdf": {
        "document_id": "yamz-236m2-rk-2014",
        "title": "236.01.01 РК",
        "edition": "2014",
        "engine_models": ["ЯМЗ-236М2"],
        "page_count": 287,
    },
    "yamz-536_re_2016.pdf": {
        "document_id": "yamz-536-re-2016",
        "title": "536.3902150 РЭ",
        "edition": "2016",
        "engine_models": ["ЯМЗ-536"],
        "page_count": 285,
    },
    "yamz-536_rk_2013.pdf": {
        "document_id": "yamz-536-rk-2013",
        "title": "536.01.02 РК",
        "edition": "2013",
        "engine_models": ["ЯМЗ-536"],
        "page_count": 603,
    },
    "zmz-5234_rk_2025.pdf": {
        "document_id": "zmz-5234-10-rk-2025",
        "title": "Двигатели ЗМЗ-5234.10 со сцеплением. Руководство по ремонту",
        "edition": "5-е издание, 2025",
        "engine_models": ["ЗМЗ-5234.10"],
        "page_count": 158,
    },
    "zmz-40904-40905_rk_2025.pdf": {
        "document_id": "zmz-40904-40905-rk-2025",
        "title": "Руководство по эксплуатации, техническому обслуживанию и ремонту",
        "edition": "10-е издание, 2025",
        "engine_models": ["ЗМЗ-40904.10", "ЗМЗ-40905.10"],
        "page_count": 238,
    },
}


def collect_manuals(manuals_dir):
    """Сверяет физические страницы и вычисляет SHA-256 исходных PDF."""
    records = []
    for filename, metadata in MANUALS.items():
        pdf = manuals_dir / filename
        with pymupdf.open(pdf) as document:
            if not document.is_pdf or document.needs_pass:
                raise ValueError(f"{pdf}: нужен доступный для чтения PDF")
            actual_pages = len(document)

        if actual_pages != metadata["page_count"]:
            raise ValueError(f"{pdf}: {actual_pages} стр., по ТЗ ожидается {metadata['page_count']}")

        with pdf.open("rb") as stream:
            sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
        records.append({**metadata, "path": pdf.as_posix(), "source_sha256": sha256})
    return records


def save_registry(output, records):
    """Записывает реестр целиком после проверки всех файлов."""
    text = json.dumps(records, ensure_ascii=False, indent=2) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", dir=output.parent, delete=False,
    )
    try:
        with temporary:
            temporary.write(text)
        os.replace(temporary.name, output)
    finally:
        Path(temporary.name).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manuals-dir", type=Path, default=Path("data/raw/manuals"),
                        help="каталог выданных PDF")
    parser.add_argument("--output", type=Path, default=Path("data/annotations/manuals.json"),
                        help="путь к реестру JSON")
    args = parser.parse_args(argv)

    try:
        records = collect_manuals(args.manuals_dir)
        save_registry(args.output, records)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1

    pages = sum(record["page_count"] for record in records)
    print(f"{args.output}: {len(records)} руководств, {pages} стр.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
