"""Собирает индекс руководств и ищет применимые физические страницы PDF."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

import pymupdf
from qdrant_client.http.exceptions import ApiException

from ._qdrant_store import COLLECTION, URL, QdrantSearch, replace_index
from .qdrant_chunks import make_chunks


def load_chunks(pages_path, notes_path, registry_path, *, documents=()):
    """Сверяет PDF с реестром и выгрузкой перед формированием фрагментов."""
    entries = json.loads(Path(registry_path).read_text(encoding="utf-8-sig"))
    registry = {entry["document_id"]: entry for entry in entries}
    if len(registry) != len(entries):
        raise ValueError("В реестре повторяются идентификаторы руководств.")
    selected = set(documents) or set(registry)
    if missing := selected - registry.keys():
        raise ValueError(f"В реестре отсутствуют руководства: {', '.join(sorted(missing))}.")

    by_doc = {name: [] for name in sorted(selected)}
    with Path(pages_path).open(encoding="utf-8-sig") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            name = row["document_id"]
            if name not in registry:
                raise ValueError(f"{name}: руководства из выгрузки нет в реестре.")
            if name in by_doc:
                by_doc[name].append(row)
    notes = [
        note for note in json.loads(Path(notes_path).read_text(encoding="utf-8-sig"))
        if note["document_id"] in selected
    ]
    for name, rows in by_doc.items():
        record = registry[name]
        physical = Path(record["path"])
        with physical.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        with pymupdf.open(physical) as pdf:
            count = len(pdf)
            allowed_models = set(record["engine_models"])
            if (
                record.get("source_sha256", digest) != digest
                or record.get("page_count", count) != count
                or len(rows) != count
                or {row["page"] for row in rows} != set(range(1, count + 1))
                or any(
                    row["source_sha256"] != digest
                    or row["edition"] != record["edition"]
                    or row["path"] != record["path"]
                    or not set(row["engine_models"]).issubset(allowed_models)
                    for row in rows
                )
            ):
                raise ValueError(f"{name}: реестр, PDF и выгрузка расходятся. Повторите извлечение.")
            by_number = {row["page"]: row for row in rows}
            for note in notes:
                if note["document_id"] != name or "numbered_table" not in note:
                    continue
                if note["source_sha256"] != digest:
                    raise ValueError(f"{name}: разметка таблицы относится к другой редакции.")
                last = note["numbered_table"]["last_page"]
                for number in range(note["page"], last + 1):
                    # Извлечение страниц пропускает таблицы из одной строки без отдельной шапки.
                    tables = pdf[number - 1].find_tables().tables
                    by_number[number]["numbered_rows"] = [
                        cells for table in tables if table.col_count == 2 for cells in table.extract()
                    ]
    return make_chunks((row for rows in by_doc.values() for row in rows), notes)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="Собрать коллекцию и переключить указанный псевдоним")
    build.add_argument("--pages", default="data/processed/pages.jsonl")
    build.add_argument("--notes", default="data/annotations/page_notes.json")
    build.add_argument("--registry", default="data/annotations/manuals.json")
    build.add_argument("--document", action="append", help="Ограничить сборку руководством из реестра")
    build.add_argument("--page", action="append", type=int, help="Оставить фрагменты с указанной страницей")
    search = commands.add_parser("search", help="Найти фрагменты в коллекции")
    scope = search.add_mutually_exclusive_group(required=True)
    scope.add_argument("--engine", help="Модель двигателя для проверки поиска")
    scope.add_argument("--vehicle-id", help="Взять модель двигателя из карточки машины")
    search.add_argument("--vehicles", default="data/raw/vehicles.json")
    search.add_argument("--variant", help="Подтверждённое исполнение двигателя")
    search.add_argument("--query", required=True)
    search.add_argument("--mode", choices=("dense", "hybrid"), default="hybrid")
    search.add_argument("--limit", type=int, choices=range(1, 101), metavar="1–100", default=12)
    for command in (build, search):
        command.add_argument("--url", default=URL)
        command.add_argument("--collection", default=COLLECTION)

    args = parser.parse_args(argv)
    try:
        return _run(args)
    except (ValueError, OSError, KeyError, ApiException) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


def _run(args):
    if args.command == "build":
        if args.document and args.collection == COLLECTION:
            raise ValueError("Для пробной сборки укажите отдельный псевдоним через --collection.")
        if args.page and (len(args.document or ()) != 1 or min(args.page) < 1):
            raise ValueError("Для --page укажите одно руководство через --document и страницы от 1.")
        chunks = load_chunks(args.pages, args.notes, args.registry, documents=args.document or ())
        if args.page:
            selected = set(args.page)
            chunks = [chunk for chunk in chunks if selected.intersection(chunk["source_pages"])]
        total = replace_index(chunks, url=args.url, collection=args.collection)
        print(f"Сохранено фрагментов: {total}. Псевдоним: {args.collection}.")
        return 0

    vehicle = None
    if args.vehicle_id:
        cards = json.loads(Path(args.vehicles).read_text(encoding="utf-8-sig"))["vehicles"]
        vehicle = next((card for card in cards if card["vehicle_id"] == args.vehicle_id), None)
        if vehicle is None:
            raise ValueError(f"Машина {args.vehicle_id} отсутствует в списке.")
    reader = QdrantSearch(args.url, args.collection, args.mode)
    try:
        params = dict(variant=args.variant, limit=args.limit)
        if vehicle is not None:
            hits = reader.search_vehicle(vehicle, args.query, **params)
        else:
            hits = reader.search(args.engine, args.query, **params)
        for item in hits:
            print(f"{item['title']} ({item['edition']}), страница PDF {item['page']}. {item['section']}")
            print(f"{item['path']}#page={item['page']}")
            print(item["text"])
    finally:
        reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
