"""Определяет место экспертного пересказа в проверенных фрагментах PDF."""

import re

from .qdrant_chunks import clean


_STEP = re.compile(r"(?m)^[ \t]*(\d{1,3}(?:\.\d{1,3})*)(?:[.)][ \t]+(?=\S)|[ \t]+(?=[А-ЯЁ]))")
_PAGE = re.compile(r"(?:PDF|стр\.?)\s+(\d+)\s*:\s*(.+)", re.I)
_PARAGRAPH = re.compile(r"абзац\s+[«\"]([^»\"]+)[»\"]", re.I)
_CELL = re.compile(r"строка\s+[«\"]([^»\"]+)[»\"]\s*\|\s*"
                   r"(?:графа|столбец)\s+[«\"]([^»\"]+)[»\"]", re.I)
_POINT = r"(?:п(?:ункт)?\.?|шаг)\s*(\d{1,3}(?:\.\d{1,3})*)"
_NUMBER = r"[+−-]?\d+(?:[.,]\d+)?"
_UNIT = (r"об\s*/\s*мин|мин\s*[-⁻]1|(?:кгс|Н)\s*[·⋅∙*×]?\s*м|кгс\s*/\s*см[2²]|"
         r"(?:М|к)?Па|(?:м|М|к)?В|(?:м|к)?Вт|(?:°[СC]|°)|мм|см|мкм|кг|%|сек|мин|ч|Ом")
_QUANTITY = re.compile(
    rf"(?<![\w.,])(?:(?P<relation>(?i:не\s+(?:менее|более|ниже|выше)|менее|более|до|от)|[±≤≥<>])\s*)?"
    rf"({_NUMBER})(?:\s*(?:\.\.\.|…|[–—−-]|до|±)\s*({_NUMBER}))?"
    rf"\s*({_UNIT})(?![\w²])"
)
_CONDITIONS = {
    "холод": r"\bхолодн\w*",
    "прогрев": r"\bпрогрет\w*|\bгоряч\w*|\bнагрет\w*",
    "холостой ход": r"холост\w*\s+ход\w*",
    "под нагрузкой": r"под\s+нагрузк\w*",
    "без нагрузки": r"без\s+нагрузк\w*",
    "остановлен": r"\b(?:остановлен|заглушен)\w*\s+двигател\w*",
    "работает": r"\bработающ\w*\s+двигател\w*",
}
_PROHIBITION = re.compile(r"\b(?:не\s+(?:допуска\w*|[а-яё]+(?:ть|ти|йте))|запрещ\w*|нельзя)\b", re.I)
_GENERIC = ("двигат", "работ", "прове", "прово", "долж", "необх", "следу", "устан", "выпол",
            "указа", "значе", "темпе", "обяза", "запре", "нельз")
_COMPARATORS = {"не ниже": "не менее", "не выше": "не более", "≤": "не более", "≥": "не менее",
                "<": "менее", ">": "более"}
_LABELS = re.compile(r"\b(?:метод|способ)\s+устранения\s*:\s*", re.I)
_WORD = re.compile(r"[а-яё]{4,}", re.I)


def _heading(value):
    return clean(value or "").casefold().replace("ё", "е")


def locator(section, place):
    """Читает явный локатор или номер пункта, уже записанный в разделе Excel."""
    if place:
        match = _PAGE.fullmatch(place)
        return (int(match[1]), match[2], "") if match else (None, place, "")
    if re.search(r"\bтабл(?:ица|\.?)\s*\.?\s*\d", section, re.I):
        return None
    match = re.fullmatch(rf"(?:(.+?)\s*\(\s*)?{_POINT}\s*\)?\.?", section, re.I)
    if match:
        return None, f"пункт {match[2]}", (match[1] or "").strip()
    matches = list(re.finditer(rf"(?<!\w){_POINT}", section, re.I))
    if len(matches) == 1 and re.fullmatch(r"[\s.),]*", section[matches[0].end():]):
        number = matches[0][1]
        if "." in number:  # Составной номер сам указывает раздел и подпункт.
            return None, f"пункт {number}", ""
    return None


def verified_locations(chunks, pages, reference):
    """Возвращает совпадения локатора вместе с текстом PDF и значением нужной ячейки."""
    page, coordinate, title = reference
    wanted_page = {page} if page is not None else set(pages)
    step = re.fullmatch(_POINT, coordinate, re.I)
    paragraph = _PARAGRAPH.fullmatch(coordinate)
    cell = _CELL.fullmatch(coordinate)
    if not (step or paragraph or cell):
        raise ValueError("Место должно указывать пункт, начало абзаца или строку и графу таблицы")
    for chunk in chunks:
        if title and _heading(title) != _heading(chunk["section"]):
            continue
        for part in chunk["page_parts"]:
            if part["page"] not in wanted_page:
                continue
            if cell:
                for header, row in _table_rows(part):
                    columns = [i for i, c in enumerate(header) if _heading(c) == _heading(cell[2])]
                    if len(columns) != 1 or _heading(row[0]) != _heading(cell[1]):
                        continue
                    column = columns[0]
                    if row[column] in (None, ""):
                        continue
                    value = str(row[column])
                    units = re.search(rf"(?<!\w)({_UNIT})(?!\w)", header[column] or "")
                    compare = " ".join((value, units[0])) if units and re.fullmatch(
                        rf"\s*{_NUMBER}(?:\s*[-–…]\s*{_NUMBER})?\s*", value) else value
                    context = "\n".join(_table_line(v) for v in (header, row))
                    yield chunk, dict(page=part["page"], text=context), " ".join(
                        (compare, header[column] or "", row[0] or ""))
            elif step and not part.get("tables"):
                lines = list(_STEP.finditer(part["text"]))
                for i, item in enumerate(lines):
                    if item[1] != step[1]:
                        continue
                    end = next((other.start() for other in lines[i + 1:]
                                if not other[1].startswith(item[1] + ".")), len(part["text"]))
                    passage = part["text"][item.end():end].strip()
                    if passage:
                        yield chunk, dict(page=part["page"], text=passage), passage
            elif paragraph and not part.get("tables"):
                start = _heading(paragraph[1])
                if len(start.split()) < 3:
                    raise ValueError("Укажите первые три или более слов абзаца")
                for block in re.split(r"\n[ \t]*\n+", part["text"]):
                    passage = block.strip()
                    beginning = _heading(passage)
                    if beginning == start or beginning.startswith((start + " ", start + ",")):
                        yield chunk, dict(page=part["page"], text=passage), passage


def _table_line(cells):
    return " | ".join(str(cell or "") for cell in cells)


def _table_rows(part):
    for table in part.get("tables", ()):
        rows = table.get("rows", [])
        if rows and rows[0]:
            for row in rows[1:]:
                if len(row) == len(rows[0]) and row[0]:
                    yield rows[0], row


def _quantities(text):
    """Сопоставляет число, границы, знак, единицу и направление ограничения."""
    text = text.replace("−", "-").replace("ё", "е")
    result = set()
    for match in _QUANTITY.finditer(text):
        relation, lower, upper, unit = match.groups()
        if relation:
            relation = " ".join(relation.casefold().split())
            relation = _COMPARATORS.get(relation, relation)
        if upper is not None:
            if "±" in match[0]:
                relation = "±"
            elif relation in (None, "от"):
                relation = "диапазон"
        normalized_unit = re.sub(r"[\s·⋅∙*×]", "", unit).replace("°C", "°С").replace("²", "2")
        result.add((relation, lower.replace(",", "."), upper.replace(",", ".") if upper else None,
                    normalized_unit))
    return result


def check_paraphrase(excerpt, source):
    """Ищет явные расхождения, сохраняя исходник как основание для рекомендации."""
    if not excerpt.strip():
        raise ValueError("Заполните выдержку")
    def terms(text):
        return {word[:5] for word in _WORD.findall(_heading(text)) if not word.startswith(_GENERIC)}

    if not terms(excerpt).intersection(terms(source)):
        raise ValueError("Проверьте, к тому ли узлу или операции относится указанное место PDF")
    if bool(_PROHIBITION.search(excerpt)) != bool(_PROHIBITION.search(source)):
        raise ValueError("Выдержка и указанное место PDF по-разному формулируют запрет")
    if not _quantities(excerpt) <= _quantities(source):
        raise ValueError("Число, единица или граница допуска расходятся с указанным местом PDF")
    def conditions(text):
        return {name for name, pattern in _CONDITIONS.items() if re.search(pattern, text, re.I)}

    if not conditions(excerpt) <= conditions(source):
        raise ValueError("Условия в выдержке расходятся с указанным местом PDF")
    # Числа без известных единиц могут быть обозначением детали или другим нормативом.
    # Проверяем их внутри выбранного действия, без его номера и соседних строк таблицы.
    number = re.compile(r"(?<!\d)[+-]?\d+(?:[.,]\d+)?")
    def bare_numbers(text):
        return {m[0].replace(",", ".") for m in number.finditer(_QUANTITY.sub(" ", text))}

    if not bare_numbers(excerpt) <= bare_numbers(source):
        raise ValueError("В указанном месте PDF не подтверждены все числовые данные выдержки")


def labelled_row_locations(chunks, pages, excerpt, token_words):
    """Сопоставляет явно подписанную причину и действие с двумя ячейками одной строки."""
    pieces = _LABELS.split(excerpt, maxsplit=1)
    if len(pieces) != 2:
        return
    cause = re.sub(r"^причина\s*:\s*", "", pieces[0].strip(), flags=re.I)
    remedy = pieces[1]
    def words(text):
        text = re.sub(r"^[а-я]\)\s*", "", text, flags=re.I)
        return token_words(text)
    cause_tokens, remedy_tokens = words(cause), words(remedy)
    if len(cause_tokens) < 3 or len(remedy_tokens) < 2:
        return
    for chunk in chunks:
        for part in chunk["page_parts"]:
            if part["page"] not in pages:
                continue
            for header, row in _table_rows(part):
                if len(header) != 2 or not row[1]:
                    continue
                if not (re.search(r"причин|неисправност", header[0], re.I) and
                        re.search(r"(?:способ|метод) устранения", header[1], re.I)):
                    continue
                if words(row[0]) == cause_tokens and words(row[1]) == remedy_tokens:
                    source = "\n".join(_table_line(v) for v in (header, row))
                    yield chunk, dict(page=part["page"], text=source), source


def candidates(chunks, pages, section, excerpt, matched=()):
    """Сохраняет до трёх указателей для ручной сверки, не подтверждая их."""
    query = {word[:6] for word in _WORD.findall(_heading(section + " " + excerpt))}
    ranked = []
    for chunk in chunks:
        for part in chunk["page_parts"]:
            if part["page"] not in pages or not part["text"].strip():
                continue
            text = part["text"]
            ranked.append((chunk, part, text))
            for header, row in _table_rows(part):
                ranked.append((chunk, part, "\n".join(_table_line(v) for v in (header, row))))
    def score(item):
        words = {word[:6] for word in _WORD.findall(_heading(item[2]))}
        return len(query & words), -len(item[2])

    ordered = [(chunk, part, part["text"]) for chunk, part, _ in matched]
    ordered.extend(sorted(ranked, key=score, reverse=True))
    result = []
    seen = set()
    for chunk, part, text in ordered:
        key = (part["page"], text)
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(chunk_id=chunk["id"], document_id=chunk["document_id"], page=part["page"],
                           section=chunk["section"], excerpt=clean(text)[:500]))
        if len(result) == 3:
            break
    return result
