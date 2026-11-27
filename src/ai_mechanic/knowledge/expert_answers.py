"""Выбирает по вопросу и признакам ML исходные строки на страницах, указанных специалистом."""

import re


_STOP = ("двигат", "машин", "провер", "смотр", "подскаж", "нужн", "неисправ",
         "причин", "сдела", "делать", "диагнос", "результат", "водител", "предлож", "возмож",
         "уточн", "отклонен", "каки", "како", "каку", "кака", "этой", "этом", "после", "важно", "перв", "работа", "работы", "сначала", "подтверд")
_CHECK = re.compile(r"\b(?:проверить|измерить|замерить|осмотреть|убедиться)\b", re.I)


def _terms(text):
    words = re.findall(r"[а-яёa-z0-9]+", text.lower().replace("ё", "е"))
    return {w[:4] for w in words if (len(w) >= 4 or w == "ход") and not w.startswith(_STOP)}


def generic_diagnostic_question(question):
    """Общие вопросы после диагностики используют наблюдения выбранной сессии."""
    return not _terms(question) and bool(re.search(r"провер|причин|диагност|делать|предлож", question, re.I))


def _groups(hit):
    for table in hit.get("tables", ()):
        rows = table.get("rows", [])
        if (not rows or len(rows[0]) != 2 or not re.search("причин|неисправност", rows[0][0] or "", re.I)
                or not re.search("метод|способ|устран|провер", rows[0][1] or "", re.I)):
            continue
        heading = ""
        for row in rows[1:]:
            if len(row) != 2 or not row[0]:
                continue
            if not row[1]:
                heading = row[0].strip()
            else:
                yield heading, row[0].strip(), row[1].strip()


def select_fault_evidence(search, focus, *, symptoms):
    """Выбирает до двух проверок по совпавшим симптомам. Рекомендации читает только из PDF."""
    terms = _terms(focus)
    if len(terms) < 2:
        return
    groups = {}
    for hit in search.fault_hits:
        for title, cause, remedy in _groups(hit):
            in_title = terms & _terms(title)
            in_row = terms & _terms(cause + " " + remedy)
            # Слова «карбюратор» или «клапан» по отдельности недостаточно для выбора причины.
            if len(in_title | in_row) < 2:
                continue
            key = (hit["document_id"], hit["page"], title or cause)
            check = _CHECK.search(remedy)
            matched = in_title if symptoms else in_title | in_row
            rank = (len(matched), bool(check), len(in_row))
            if key not in groups or rank > groups[key][0]:
                groups[key] = (rank, hit, title, cause, remedy)
    ranked = sorted(groups.values(), key=lambda entry: entry[0], reverse=True)
    for _, hit, title, cause, remedy in ranked[:2 if symptoms else 1]:
        passage = "\n".join(value for value in (title, cause, remedy) if value)
        text = (title.rstrip(".") + ". " if title else "") + cause.rstrip(".") + ". " + remedy
        search.add(hit, passage, text=text, kind="cause")
    if search.evidence:
        return
    # Для обычного текста показываем целый абзац с предметными словами вопроса и действием.
    # Полные таблицы нормативов обрабатывает отдельный отбор с заголовками и единицами.
    found = []
    for hit in search.fault_hits:
        if hit.get("tables") or hit.get("kind") == "table":
            continue
        for block in re.split(r"\n\s*\n", hit["text"]):
            block = " ".join(re.sub(r"(?<=\w)-[ \t]*\n\s*(?=\w)", "", block).split())
            overlap = len(terms & _terms(block))
            if overlap >= max(2, (len(terms) + 1) // 2) and _CHECK.search(block) and len(block) <= 1100:
                found.append((overlap, hit, block))
    if found:
        _, hit, passage = max(found, key=lambda item: item[0])
        search.add(hit, passage)
