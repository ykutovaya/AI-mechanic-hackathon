"""Связи экспертной таблицы с текстом и применимостью конкретных фрагментов PDF."""

import hashlib
import json
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from openpyxl import load_workbook

from .expert_locations import (
    candidates, check_paraphrase, labelled_row_locations, locator, point_number, point_on_page, verified_locations,
)
from .qdrant_chunks import _chunk, _model_restriction


_REQUIRED = ("Код", "Название неисправности", "vehicle_id", "Модель двигателя",
             "Документ", "Страницы PDF", "Выдержка", "Статус", "Примечание")
_ABSENT = {"не найдено", "в проверенных разделах не найдено"}
_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_DASHES = str.maketrans("‐‑‒–—−", "------")
_TOKEN = re.compile(r"""\d+(?:[.,]\d+)*|(?<=\d)['"′″]|(?<=\d):(?=\d)|[^\W\d_]+|[^\w\s«»“”„"'.,;:]""")
_DESIGNATION = re.compile(r"(?<!\d)\d{3}(?:-\d{7}(?:-[а-я])?|\.\d{2}\.\d{2}|\.\d{7})(?!\d)")
_BARE_VALUES = re.compile(r"\d+(?:[.,]\d+)?\s*[-–…]\s*\d+(?:[.,]\d+)?\s*"
                          r"\(\s*\d+(?:[.,]\d+)?\s*[-–…]\s*\d+(?:[.,]\d+)?\s*\)\.?\s*[»\"]?$")
_UNITS = re.compile(r"(?:[Нн]\s*[·*×∙-]?\s*м\b|кгс|[МКкм]?Па\b|[Ммк]?В\b|[мк]?Вт\b|мм\b|°С\b)")
_CONFLICT = re.compile(r"противореч|расхождени[ея] (?:в |между |по )|двойная трактовка|"
                       r"предлагаемая выдержка|не использовать как норматив", re.I)


def _label(text):
    return " ".join(str(text).casefold().replace("ё", "е").translate(_DASHES).split())


def _document(label, registry):
    """Определяет конкретное издание по обозначению или имени файла и году."""
    name = _label(label)
    years = set(_YEAR.findall(name))
    # После «;» в книге встречается как примечание к нумерации, так и ссылка на вторую книгу.
    refs = name.strip(" ;").split(";")
    if any(not re.match(r"\s*физ\.\s*номер страницы\s*=", part) for part in refs[1:]):
        raise ValueError("Ссылка объединяет несколько документов или содержит неясное уточнение")
    mentioned = set(_DESIGNATION.findall(name))
    filenames = set(re.findall(r"[\w.-]+\.pdf\b", name))
    found = []
    for doc in registry.values():
        title = _label(doc["title"])
        basename = _label(Path(doc["path"]).name)
        named = _label(doc["document_id"]) in name or basename in name
        designation = _DESIGNATION.search(title)
        if mentioned and (not designation or mentioned != {designation[0]}):
            continue
        if filenames and any(not basename.endswith(filename) for filename in filenames):
            continue
        if designation:
            identified = designation[0] in name
            repair = bool(re.search(r"\bрк\b|руководство по ремонту", name))
            operation = bool(re.search(r"\bрэ\b|руководство по эксплуатации", name))
            if ("рк" in title and operation) or ("рэ" in title and repair):
                continue
        else:
            # В общем руководстве ЗМЗ обозначение второго двигателя часто пишут без префикса.
            models = [re.escape(_label(m).split("-", 1)[-1]) for m in doc["engine_models"]]
            identified = "змз" in name and all(re.search(r"(?<!\d)" + m + r"(?!\d)", name) for m in models)
        if not (identified or named):
            continue
        edition_years = set(_YEAR.findall(doc["edition"]))
        if (years and years != edition_years) or (not years and not named):
            continue
        ordinal = re.match(r"(\d+)-е издание", _label(doc["edition"]))
        number = re.search(r"(?:изд(?:ание|\.)?)\s*(\d+)(?:-е)?", name)
        written = re.search(r"(?:изд(?:ание|\.)?)\s+(пятое|десятое)", name)
        if ordinal and ((number and int(number[1]) < 100 and number[1] != ordinal[1])
                        or (written and {"пятое":"5", "десятое":"10"}[written[1]] != ordinal[1])):
            continue
        found.append(doc)
    if len(found) != 1:
        raise ValueError("Не определена единственная редакция из выданных PDF")
    return found[0]


def _page_numbers(value, last):
    spec = re.sub(r"(?i)\b(?:страниц(?:а|ы)?\s*(?:PDF)?|стр\.\s*(?:PDF)?|PDF)\s*",
                  "", str(value)).translate(_DASHES).strip()
    parts = re.split(r"[,;]", spec)
    pages = set()
    for part in parts:
        match = re.fullmatch(r"\s*(\d+)(?:\s*-\s*(\d+))?\s*", part)
        if not match:
            raise ValueError("Нужны точные физические номера страниц PDF")
        start, end = int(match[1]), int(match[2] or match[1])
        if not 1 <= start <= end <= last:
            raise ValueError("Диапазон выходит за границы PDF")
        pages.update(range(start, end + 1))
    return sorted(pages)


@lru_cache(maxsize=3072)
def _tokens(text):
    """Сохраняет числа, знаки, единицы и координаты слов в очищенном тексте PDF."""
    visible = re.sub(r"(?<=[А-Яа-яЁё])[-\u00ad][ \t]*\r?\n\s*(?=[а-яё])", "", text)
    result = []
    for match in _TOKEN.finditer(visible):
        word = match[0].translate(_DASHES)
        if word[0].isdigit():
            word = word.replace(",", ".")
        elif word in {"'", '"'}:
            word = "′" if word == "'" else "″"
        elif word.isalpha() and (len(word) > 4 or word.casefold() in {"щуп", "вал", "кольцо", "ключ", "болт", "гайка"}):
            word = word.casefold().replace("ё", "е")
        result.append((word, match.start(), match.end()))
    return visible, result


def _find(parts, needle):
    """Ищет целую выдержку, возвращая исходный текст и физическую страницу каждой части."""
    stream = []
    content = []
    for part in parts:
        visible, words = _tokens(part["text"])
        index = len(content)
        content.append((part["page"], visible))
        stream.extend((token, index, start, end) for token, start, end in words)
    hits = []
    expected = len(needle)
    for ix, (word, _, _, _) in enumerate(stream):
        if word != needle[0] or ix + expected > len(stream):
            continue
        span = stream[ix:ix+expected]
        if any(w[0] != ref for w, ref in zip(span, needle)):
            continue
        _, start_index, begin, _ = span[0]
        _, end_index, _, finish = span[-1]
        prefix = content[start_index][1][:begin]
        suffix = content[end_index][1][finish:]
        if re.search(r"\b(?:не|без|нельзя|запрещается)\s+$", prefix, re.I):
            continue
        if needle[-1][0].isdigit() and re.match(r"""\s*(?:[%°º′″'"]|[Мкм]?Па\b|[Ммк]?В\b|мм\b|Н\b|кг)""", suffix):
            continue
        pieces = []
        for _, index, start, end in span:
            if not pieces or pieces[-1][0] != index:
                pieces.append([index, start, end])
            else:
                pieces[-1][2] = end
        hit = []
        for index, start, end in pieces:
            page, text = content[index]
            while end < len(text) and text[end] in ".!?»":
                end += 1
            hit.append(dict(page=page, text=text[start:end]))
        hits.append(hit)
    return hits


def _raw_hits(pages, needle):
    # Номера раздельных диапазонов нельзя объединять в одну фразу.
    groups = []
    for page in pages:
        if not groups or page["page"] != groups[-1][-1]["page"] + 1:
            groups.append([])
        groups[-1].append(page)
    hits = []
    for group in groups:
        hits.extend(_find(group, needle))
    text_pages = {part["page"] for hit in hits for part in hit}
    for page in pages:
        ocr = page.get("ocr", {})
        if ocr.get("status") == "verified" and page["page"] not in text_pages:
            hits.extend(_find([dict(page=page["page"], text=ocr["text"])], needle))
    return hits


def _new_scoped_chunk(page, parts, engine, variant):
    """Выделяет только целый абзац с прямо указанной в PDF моделью."""
    if len(parts) != 1 or variant:
        return None
    quote = parts[0]["text"].strip()
    if not re.match(r"(?:на двигателе|для двигателя)\s+" + re.escape(engine) + r"(?![\w.-])", quote, re.I):
        return None
    paragraphs = re.split(r"\r?\n[ \t]*\r?\n|\r?\n(?=(?:На двигателе|Для двигателя)\s)", page["text"])
    if not any([w[0] for w in _tokens(p)[1]] == [w[0] for w in _tokens(quote)[1]] for p in paragraphs):
        return None
    if _model_restriction(dict(text=quote, engine_models=[engine]), [engine]):
        return None
    return _chunk(page, page["section"], [engine], [engine + "|all"], parts, "text")


class _UnscopedQuote(ValueError):
    """Цитата есть в PDF, но её область применимости не проверена."""


def _navigation(row, detail, eligible, possible, pair, doc):
    """Добавляет указатель для поиска. Проверенным основанием остаётся только fault_bindings."""
    words = _topic_words(row["Выдержка"])
    if not words:
        return
    numbers = set(detail["requested_pages"])
    if len(numbers) > 1 and len(numbers) == doc["page_count"]:
        return
    # В короткой выдержке нужны до трёх значимых терминов, в длинной — не менее трети
    # (до семи). Так слова «клапан» и «заменить» не уводят к другому узлу.
    minimum = max(2, min(3, len(words)), min(7, (len(words) + 2) // 3))
    ranked = []
    for chunk in eligible:
        for part in chunk["page_parts"]:
            if part["page"] not in numbers:
                continue
            text = _topic_words(part["text"])
            overlap = words & text
            if len(overlap) >= minimum:
                ranked.append((-len(overlap), len(text), part["page"], chunk["id"], chunk))
    seen = set()
    best = -min((item[0] for item in ranked), default=0)
    for score, _, number, key, chunk in sorted(ranked, key=lambda hit: hit[:4]):
        if -score * 3 < best * 2:
            break
        if (number, key) in seen:
            continue
        seen.add((number, key))
        routes = chunk.setdefault("fault_routes", [])
        same = next((r for r in routes if r["code"] == detail["code"] and
                     r["applicability"] == pair and r["source_pages"] == [number]), None)
        if same is None:
            same = {"code": detail["code"], "applicability": pair, "source_pages": [number], "rows": []}
            routes.append(same)
        same["rows"].append(detail["row"])
        detail.setdefault("routes", []).append({"chunk_id": key, "page": number})
        if len(seen) == 3:
            return
    # Короткая ссылка эксперта разрешает открыть PDF вручную. Страница другого
    # исполнения или непроверенный OCR никогда не добавляются в доказательства чата.
    if seen or len(numbers) > 6:
        return
    eligible_pages = {n for c in eligible for n in c["source_pages"]}
    scoped_pages = {n for c in possible.values() for n in c["source_pages"]}
    if numbers & (scoped_pages - eligible_pages):
        return
    detail["reading_reference"] = {"document_id": doc["document_id"], "source_pages": sorted(numbers),
                                   "applicability": pair}


@lru_cache(maxsize=8192)
def _topic_words(text):
    """Убирает частые слова инструкций перед выбором фрагмента как подсказки для поиска."""
    words = re.findall(r"[а-яё]{4,}", _label(text))
    common = ("двигат", "возмож", "руковод", "соответ", "провери", "проверк", "следуе",
              "провес", "устано", "неиспр", "устран", "деталь", "детали", "долж", "рекоме",
              "котор", "систем", "раздел", "таблиц", "указан", "работ", "после", "также",
              "необхо", "страни", "этого", "други", "данно", "налич", "замен", "произве",
              "состоян", "провер", "измерит", "значени", "устрани", "помощ", "выполн")
    result = set()
    for word in words:
        if word.startswith(common):
            continue
        stem = re.sub(r"(?:иями|ями|ами|ого|его|ому|ему|ой|ей|ые|ие|ую|юю|ая|яя|ых|их|ою|ом|ем|ам|ям|ах|ях|ов|ев|ия|ию|ий|ии|а|я|е|о|и|ы|у|ю|ь)$", "", word)
        result.add((stem if len(stem) >= 4 else word)[:11])
    return frozenset(result)


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def apply_expert_sources(chunks, *, workbook, registry, fault_types, vehicles, pages):
    """Привязывает строки XLSX к проверенному корпусу, возвращает фрагменты и отчёт по каждой строке."""
    if not isinstance(registry, dict):
        registry = {doc["document_id"]: doc for doc in registry}
    codes = {item["code"]: item["title"] for item in _load(fault_types)}
    cards = {item["vehicle_id"]: item["engine_model"] for item in _load(vehicles)["vehicles"]}
    by_page = defaultdict(list)
    output = {chunk["id"]: {k:v for k,v in chunk.items() if k not in ("fault_bindings", "fault_routes")} for chunk in chunks}
    for chunk in output.values():
        for number in chunk["source_pages"]:
            by_page[chunk["document_id"], number].append(chunk)
    raw_pages = {(name, row["page"]): row for name, rows in pages.items() for row in rows}
    counts = {"bound": 0, "review": 0, "not_found": 0}
    report = {"workbook_sha256": hashlib.sha256(Path(workbook).read_bytes()).hexdigest(),
              "counts": counts, "rows": []}
    book = load_workbook(workbook, read_only=True, data_only=True)
    try:
        if len(book.worksheets) != 1:
            raise ValueError("В экспертной книге должен быть один лист с выдержками")
        sheet = book.worksheets[0]
        report["sheet"] = sheet.title
        records = sheet.values
        headers = [str(v or "").strip() for v in next(records, ())]
        if (any(headers.count(name) != 1 for name in _REQUIRED)
                or any(headers.count(name) > 1 for name in ("Исполнение", "Раздел", "Место в источнике"))):
            raise ValueError("В экспертной таблице отсутствуют или повторяются обязательные столбцы")
        for number, cells in enumerate(records, 2):
            if not any(v is not None and str(v).strip() for v in cells):
                continue
            record = dict(zip(headers, (str(v).strip() if v is not None else "" for v in cells)))
            code, engine = record["Код"], record["Модель двигателя"]
            detail = dict(row=number, code=code, engine_model=engine, variant=record.get("Исполнение", ""),
                          vehicle_id=record["vehicle_id"], status=record["Статус"], document=record["Документ"],
                          pages_spec=record["Страницы PDF"], section=record.get("Раздел", ""),
                          excerpt=record["Выдержка"], locator=record.get("Место в источнике", ""))
            try:
                status = _label(record["Статус"])
                if status in _ABSENT:
                    detail.update(result="not_found", reason="В проверенных разделах нет источника для этой выдержки")
                elif status == "подтверждено":
                    _bind(record, detail, registry, codes, cards, raw_pages, by_page, output)
                    detail["result"] = "bound"
                else:
                    raise ValueError("Строка не подтверждена специалистом")
            except ValueError as exc:
                detail.update(result="review", reason=str(exc))
            counts[detail["result"]] += 1
            report["rows"].append(detail)
    finally:
        book.close()
    routed = sum(bool(row.get("routes")) for row in report["rows"])
    reading = sum(bool(row.get("reading_reference")) for row in report["rows"])
    available = counts["bound"] + routed + reading
    report["navigation"] = {"bound": counts["bound"], "routed_only": routed, "reading_only": reading,
                            "unavailable": len(report["rows"]) - available, "available": available}
    return list(output.values()), report


def _bind(row, detail, registry, codes, cards, raw_pages, by_page, output):
    code, engine, variant = detail["code"], detail["engine_model"], detail["variant"]
    if not code or code not in codes or _label(row["Название неисправности"]) != _label(codes[code]):
        raise ValueError("Проверьте код и название в официальном справочнике неисправностей")
    if cards.get(detail["vehicle_id"]) != engine or not engine:
        raise ValueError("Двигатель не совпадает с карточкой машины")
    if _CONFLICT.search(row["Выдержка"] + " " + row["Примечание"]):
        raise ValueError("В выдержке или примечании отмечено расхождение либо предложена замена")
    doc = _document(row["Документ"], registry)
    doc_id = doc["document_id"]
    detail["document_id"] = doc_id
    if engine not in doc["engine_models"]:
        raise ValueError("Двигатель отсутствует в реестре этого руководства")
    numbered_page = point_on_page(detail["pages_spec"])
    page_spec = numbered_page[0] if numbered_page else detail["pages_spec"]
    numbers = _page_numbers(page_spec, doc["page_count"])
    detail["requested_pages"] = numbers
    pages = [raw_pages.get((doc_id, number)) for number in numbers]
    if any(p is None or p["source_sha256"] != doc["source_sha256"] or p["edition"] != doc["edition"]
           or engine not in p["engine_models"] for p in pages):
        raise ValueError("Страница указанной редакции и модели отсутствует в выгрузке")
    pair = engine + "|" + (variant or "all")
    pairs = {pair, engine + "|all"}
    possible = {c["id"]: c for n in numbers for c in by_page[doc_id, n]}
    eligible = [c for c in possible.values()
                if pairs.intersection(c["applicability"])
                and c["source_sha256"] == doc["source_sha256"] and c["edition"] == doc["edition"]]
    explicit = detail["locator"]
    reference = locator(detail["section"], explicit)
    if numbered_page:
        if explicit and (reference[0] not in (None, numbered_page[0])
                         or point_number(reference) != point_number(numbered_page)):
            raise ValueError("Пункт в колонке «Страницы PDF» расходится с местом в источнике")
        reference = numbered_page
    point = point_number(reference)
    excerpt = row["Выдержка"]
    method = "quote"
    matched = []
    try:
        if explicit or point:
            found = None  # Явный локатор проверяется даже при совпадении цитаты в другом месте.
        else:
            found = _exact(eligible, pages, raw_pages, doc_id, numbers, engine, variant, excerpt, output)
        if found is None:
            if reference:
                method = "locator"
                if reference[0] is not None and reference[0] not in numbers:
                    raise ValueError("Страница в локаторе отсутствует в колонке «Страницы PDF»")
                matched = list(verified_locations(eligible, numbers, reference))
                if not matched:
                    raise ValueError("На указанных страницах не найдено применимое место по локатору")
            else:
                method = "table_row"
                matched = list(labelled_row_locations(eligible, numbers, excerpt,
                                                      lambda text: [word[0] for word in _tokens(text)[1]]))
                if not matched:
                    _navigation(row, detail, eligible, possible, pair, doc)
                    raise ValueError("Укажите место в источнике для пересказа в колонке «Раздел» или «Место в источнике»")
            if len(matched) != 1:
                raise ValueError("Найдено несколько мест в PDF. Уточните страницу, пункт или строку таблицы")
            chunk, part, comparison_text = matched[0]
            check_paraphrase(excerpt, comparison_text)
            parts = [part]
        else:
            chunk, parts = found
    except ValueError as exc:
        if isinstance(exc, _UnscopedQuote) and not explicit:
            _navigation(row, detail, eligible, possible, pair, doc)
        detail["candidates"] = candidates(eligible, numbers, detail["section"], excerpt, matched)
        raise
    if point:
        exact = _chunk(chunk, f"Пункт {point}", [engine], [pair], parts,
                       "table" if parts[0].get("tables") else chunk["kind"], chunk.get("condition", ""))
        chunk = output.setdefault(exact["id"], exact)
        detail["source_point"] = point
    detail["match_method"] = method
    source_pages = sorted({part["page"] for part in parts})
    excerpt = "\n".join(part["text"] for part in parts)
    bindings = chunk.setdefault("fault_bindings", [])
    same = next((b for b in bindings if b["code"] == code and b["applicability"] == pair
                 and b["source_pages"] == source_pages and b["excerpt"] == excerpt), None)
    if same is None:
        same = dict(code=code, applicability=pair, source_pages=source_pages, excerpt=excerpt, rows=[])
        bindings.append(same)
    same["rows"].append(detail["row"])
    detail.update(chunk_id=chunk["id"], source_pages=source_pages, applicability=pair)


def _exact(eligible, pages, raw_pages, doc_id, numbers, engine, variant, excerpt, output):
    needle = [w[0] for w in _tokens(excerpt)[1]]
    if len(needle) < 4:
        return None
    raw = _raw_hits(pages, needle)
    if len(raw) > 1:
        raise ValueError("Выдержка встречается на указанных страницах несколько раз")
    matches = []
    for chunk in eligible:
        structured_table = any(p.get("tables") for p in chunk["page_parts"])
        if not raw and not structured_table:
            continue
        if _BARE_VALUES.search(excerpt):
            headers = [str(table["rows"][0]) for part in chunk["page_parts"] for table in part.get("tables", [])
                       if table.get("rows")]
            if not any(_UNITS.search(header) for header in headers):
                continue
        for parts in _find(chunk["page_parts"], needle):
            matched = [p["page"] for p in parts]
            if matched == list(range(matched[0], matched[-1] + 1)) and all(n in numbers for n in matched):
                matches.append((chunk, parts))
    if len(matches) > 1:
        raise ValueError("Несколько совпадений с фрагментами: нужно уточнить границы выдержки")
    if matches:
        chunk, parts = matches[0]
    elif raw:
        if _BARE_VALUES.search(excerpt):
            raise ValueError("Для чисел из строки таблицы нет фрагмента с заголовками и единицами")
        parts = raw[0]
        chunk = _new_scoped_chunk(raw_pages[doc_id, parts[0]["page"]], parts, engine, variant)
        if not chunk:
            raise _UnscopedQuote("Текст найден, но подтверждённого фрагмента с этой применимостью нет")
        chunk = output.setdefault(chunk["id"], chunk)
    else:
        return None
    return chunk, parts
