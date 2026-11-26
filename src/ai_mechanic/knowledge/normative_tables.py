"""Выбирает запрошенные строки из таблиц «параметр — норматив» руководства."""

import re

from py_rust_stemmers import SnowballStemmer

from .qdrant_chunks import clean


_STEMMER = SnowballStemmer("russian")
_METRIC = re.compile(r"\b(?:биени|диаметр|зазор|перекос|отклонени|толщин|высот|ширин|глубин|давлени|сопротивлени)\w*", re.I)
_WORDS = re.compile(r"[а-яёa-z]+|\d+(?:[.,]\d+)?", re.I)
_STOP = set("""какой какое каков каково какие каковы сколько какая при проверке контроле
               допустимое допустим предельный максимальный минимальный предел допускается допускаются
               должно должен должна быть составлять величина значение норматив норма параметр
               таблица руководство миллиметр миллиметров мм мкм по в с к на и для у""".split())
_STOP = {_STEMMER.stem_word(w) for w in _STOP}
_REFERENCE = re.compile(r"\b(?:относительно|по отношению к)\b", re.I)
_UNIT = r"(?:мм|мкм|см|кПа|МПа|Па|кОм|Ом|Н[·∙⋅]м|°[СC]|°)"
_NUMBER = r"[+−–-]?\d+(?:[.,]\d+)?"
_READING = re.compile(rf"(?:(?:не более|не менее|от|до)\s+)?{_NUMBER}"
                      rf"(?:\s*(?:±|…|\.{{2,3}}|до|[–—])\s*{_NUMBER})?\s*(?P<unit>{_UNIT})?", re.I)


def _terms(text):
    text = re.sub(r"\bколенвал\w*\b", "коленчатый вал", text.casefold())
    text = re.sub(r"\bшеек\b", "шейка", text)
    return {_STEMMER.stem_word(w) for w in _WORDS.findall(text)} - _STOP


def _subjects(text):
    parts = _REFERENCE.split(text, maxsplit=1)
    return _terms(parts[0]), _terms(parts[1]) if len(parts) > 1 else set()


def _reading(header, cell):
    """Сохраняет знак предела и единицу исходной колонки или ячейки."""
    value = _READING.fullmatch(cell)
    if not value:
        return None
    heading_unit = re.search(rf"[,(]\s*({_UNIT})\.?\s*\)?\s*$", header, re.I)
    unit = heading_unit[1] if heading_unit else value["unit"]
    if not unit or (value["unit"] and value["unit"].casefold() != unit.casefold()):
        return None
    result = cell if value["unit"] else f"{cell} {unit}"
    limit = re.search(r"\bне (?:более|менее)\b", header, re.I)
    if limit:
        if re.match(r"не (?:более|менее)\b", cell, re.I):
            if not cell.casefold().startswith(limit[0].casefold()):
                return None
        else:
            result = f"{limit[0].lower()} {result}"
    heading = header[:heading_unit.start()].strip() if heading_unit else header
    # Сохраняем режим измерения, если он записан в шапке, а не в названии строки.
    if not re.fullmatch(r"(?:(?:номинальн|допустим|предельн)\w*\s+)?(?:величина|значение)(?:\s+параметра)?"
                        r"(?:,?\s+не (?:более|менее))?", heading, re.I):
        result += f" ({heading})"
    elif not limit and re.match(r"номинальн", heading, re.I) and not cell.lower().startswith(("не более", "не менее")):
        result += " (номинальное значение)"
    return result


def select_normative_rows(search, question):
    """Возвращает выдержки по ячейкам одной величины с учётом детали и базы измерения."""
    metric = _METRIC.search(question)
    if not metric or re.search(r"\b(?:как|чем)\s+(?:провер|измер|устран|уменьш|увелич)", question):
        return None
    intents = re.split(r"\s+(?:и|а также)\s+", question.strip(" ?!."))
    quantity = _terms(metric[0])
    requested = [_subjects(part) for part in intents]
    # В «биение барабана и диска относительно оси» последняя база относится к обоим.
    if requested[-1][1] and all(not base for _, base in requested[:-1]):
        requested = [(subject, requested[-1][1]) for subject, _ in requested]
    matches = [{} for _ in intents]
    relevant_table = False
    for hit in search.primary:
        if hit.get("kind") != "table":
            continue
        for table in hit.get("tables", ()):
            rows = table.get("rows", ())
            if not rows or len(rows[0]) != 2 or not re.fullmatch(
                    r"(?:наименование\s+)?параметра?", clean(rows[0][0] or ""), re.I):
                continue
            header = clean(rows[0][1] or "")
            if not re.search(r"номинальн|допустим|предельн|значени|величин|не (?:более|менее)", header, re.I):
                continue
            group = ""
            for row in rows[1:]:
                if len(row) != 2 or not row[0]:
                    continue
                name, cell = (clean(v or "") for v in row)
                if not cell:
                    group = name if not re.search(r"\d", name) else ""
                    continue
                measured, reference = _subjects(name)
                if not quantity <= measured:
                    continue
                relevant_table = True
                for (subject, base), found in zip(requested, matches):
                    if not (subject - quantity) or not subject <= (measured | _terms(group)):
                        continue
                    if base and not base <= reference:
                        # «Шеек относительно крайних» опускает повтор названия тех же деталей.
                        abbreviated = re.search(r"\bотносительно\s+\w+(?:их|ых)$", name, re.I)
                        if not (abbreviated and reference <= base and base - reference <= measured & subject):
                            continue
                    key = (hit["document_id"], hit["page"], hit.get("condition"), group, name, header, cell)
                    found[key] = hit
    if not relevant_table:
        return None
    missing = []
    for clause, found in zip(intents, matches):
        if len(found) == 1:
            key, hit = next(iter(found.items()))
            _, _, _, group, name, header, cell = key
            value = _reading(header, cell)
            if value:
                label = f"{group}. {name}" if group and not _terms(group) <= _terms(name) else name
                search.add(hit, f"{label} | {header}: {cell}", text=f"{label} — {value}.")
                continue
        missing.append(clause.strip())
    if missing:
        return "В таблице не найден однозначный норматив с единицей измерения для запроса «" + "», «".join(missing) + "»."
    return ""
