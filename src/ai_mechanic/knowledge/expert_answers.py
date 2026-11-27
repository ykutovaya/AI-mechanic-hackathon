"""Выбирает по вопросу и признакам ML исходные строки на страницах, указанных специалистом."""

import re


_STOP = ("двигат", "машин", "провер", "смотр", "подскаж", "нужн", "неисправ",
         "причин", "сдела", "делать", "диагнос", "результат", "водител", "предлож", "возмож",
         "уточн", "отклонен", "каки", "како", "каку", "кака", "этой", "этом", "после", "важно", "перв", "работа", "работы", "сначала", "подтверд")
_CHECK = re.compile(r"\b(?:проверить|измерить|замерить|осмотреть|убедиться)\b", re.I)


_F13_PUMP = re.compile(r"\b(?:(?:электро)?бензонасос\w*|эбн)\b", re.I)
_F13_COMPONENT = re.compile(
    r"\b(?:карбюратор\w*|ускорительн\w+ насос\w*|поплавков\w+ камер\w*|электробензонасос\w*|эбн)\b", re.I
)
_REPAIR = re.compile(r"\b(?:заменить|отрегулировать|устранить|промыть|продуть|снять|установить)\b", re.I)


def asks_if_fuel_pump_needs_disassembly(question):
    """Отличает вопрос о необходимости снятия насоса от запроса порядка разборки."""
    return bool((_F13_PUMP.search(question) or re.search(r"топливн\w* насос\w*", question))
                and re.search(r"\b(?:снять|снима\w*|сняти\w*|разобра\w*|разбира\w*|разборк\w*)\b", question)
                and re.search(r"\b(?:нужно ли|надо ли|требуется ли|стоит|пора|есть ли основания)\b", question))


def _f13_inspection(hit, cause, remedy, *, include_observations=False):
    """Выделяет проверки подачи бензина из применимой строки таблицы."""
    if hit.get("fault_code") != "F13" or not _F13_COMPONENT.search(cause):
        return None
    checks = []
    for sentence in re.split(r"(?<=[.!?;])\s+(?![^()]*\))", remedy):
        sentence = sentence.strip().rstrip(".;")
        check = re.match(r"Проверить\b", sentence, re.I)
        sound = include_observations and re.match(r"При включении зажигания должен быть слышен\b", sentence, re.I)
        if (check or sound) and not _REPAIR.search(sentence):
            checks.append(sentence[:1].upper() + sentence[1:] + ".")
    if checks and hit.get("condition"):
        checks.insert(0, hit["condition"].rstrip(".") + ".")
    return " ".join(checks) or None


def _terms(text):
    words = re.findall(r"[а-яёa-z0-9]+", text.lower().replace("ё", "е"))
    return {w[:4] for w in words if (len(w) >= 4 or w == "ход") and not w.startswith(_STOP)}


def generic_diagnostic_question(question):
    """Общие вопросы после диагностики используют наблюдения выбранной сессии."""
    if _terms(question) or not re.search(r"провер|причин|диагност|делать|предлож", question, re.I):
        return False
    # «Как проверить ГРМ?» называет отдельный узел, даже если название короче четырёх букв.
    conversational = {"на", "по", "во", "со", "за", "из", "от", "об", "до", "ко", "при", "для", "без",
                      "как", "что", "где", "чем", "это", "эта", "эту", "так", "его", "её", "ее",
                      "мне", "нам", "нас", "вы", "ли", "же", "бы", "не", "да", "или", "ещё", "еще"}
    return all(word in conversational for word in re.findall(r"\b[а-яёa-z0-9]{2,3}\b", question.casefold()))


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


def select_fault_evidence(search, focus, *, symptoms, fuel_checks_only=False):
    """Выбирает до двух проверок по совпавшим симптомам. Рекомендации читает только из PDF."""
    terms = _terms(focus)
    if len(terms) < 2:
        return
    pump_named = _F13_PUMP.search(focus)
    groups = {}
    for hit in search.fault_hits:
        for title, cause, remedy in _groups(hit):
            if pump_named and hit.get("fault_code") == "F13" and not _F13_PUMP.search(f"{title} {cause} {remedy}"):
                continue
            if fuel_checks_only and not _f13_inspection(hit, cause, remedy):
                continue
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
        work = _f13_inspection(hit, cause, remedy)
        if fuel_checks_only:
            remedy = _f13_inspection(hit, cause, remedy, include_observations=True)
        passage = "\n".join(value for value in (title, cause, remedy) if value)
        if hit.get("fault_code") == "F13":
            cause = re.sub(r"^[а-я]\)\s*", "", cause, flags=re.I).rstrip(" .;:")
            cause = cause[:1].upper() + cause[1:]
        text = (title.rstrip(".") + ". " if title else "") + cause.rstrip(".") + ". " + remedy
        search.add(hit, passage, text=text, kind="cause", work=work)
    if search.evidence or fuel_checks_only:
        return
    # Для обычного текста показываем целый абзац с предметными словами вопроса и действием.
    # Полные таблицы нормативов обрабатывает отдельный отбор с заголовками и единицами.
    found = []
    for hit in search.fault_hits:
        if hit.get("tables") or hit.get("kind") == "table":
            continue
        for block in re.split(r"\n\s*\n", hit["text"]):
            block = " ".join(re.sub(r"(?<=\w)-[ \t]*\n\s*(?=\w)", "", block).split())
            if pump_named and hit.get("fault_code") == "F13" and not _F13_PUMP.search(block):
                continue
            overlap = len(terms & _terms(block))
            if overlap >= max(2, (len(terms) + 1) // 2) and _CHECK.search(block) and len(block) <= 1100:
                found.append((overlap, hit, block))
    if found:
        _, hit, passage = max(found, key=lambda item: item[0])
        search.add(hit, passage)
