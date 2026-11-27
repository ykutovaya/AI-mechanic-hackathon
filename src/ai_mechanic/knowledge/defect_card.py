"""Подбирает заключение из строки карты дефектации по обозначению детали."""

import re


_PART = re.compile(r"(?<!\w)\d{2,}[а-яa-z]?[.-]\d{4,}(?:[-.][а-яa-z0-9]+)*(?![\w-])", re.I)
_DEFECT = re.compile(r"\b(?:трещин|облом|изно[сш]|задир|выкраш|скол|корроз|деформац|биени|царапин)\w*")
_DASHES = str.maketrans("‐‑–—", "----")


def _parts(text):
    return set(_PART.findall(text.casefold().translate(_DASHES)))


def _defects(text):
    return {word[:4] for word in _DEFECT.findall(text.casefold())}


def _words(text):
    return {w[:4] for w in re.findall(r"[а-яё]{4,}", text.casefold())}


def _rows(hit, part, damage, question):
    scoped = part in _parts((hit.get("section") or "") + " " + (hit.get("condition") or ""))
    for table in hit.get("tables", []):
        rows = table.get("rows", [])
        if not rows:
            continue
        header = [(label or "").strip().casefold() for label in rows[0]]
        if "возможный дефект" not in header or "заключение" not in header:
            continue
        code_column = next((i for i, label in enumerate(header) if label.startswith("обозначение")), None)
        if code_column is None and not scoped:
            continue
        for cells in rows[1:]:
            if len(cells) != len(header):
                continue
            if code_column is not None and part not in _parts(cells[code_column] or ""):
                continue
            defect = (cells[header.index("возможный дефект")] or "").strip()
            conclusion = (cells[header.index("заключение")] or "").strip()
            if not conclusion or not conclusion[0].isalpha() or not damage <= _defects(defect):
                continue
            # Уточнение места дефекта существенно: трещина на торце и на ободе
            # может требовать разного решения. Нельзя подставить случайную строку.
            specifics = _words(defect) - _defects(defect)
            if specifics and not specifics.intersection(question):
                continue
            method = next((v for label, v in zip(header, cells)
                           if label.startswith("способ установления") and v), "")
            excerpt = f"{defect}. " + (f"{method}. " if method else "") + f"Заключение — {conclusion}."
            yield len(question & specifics), excerpt


def select_defect_card(search, question):
    """Сверяет точное обозначение и дефект до выдачи заключения со страницы карты."""
    codes = _parts(question)
    damage = _defects(question)
    if len(codes) != 1 or not damage or not re.search(r"дефектац|заключени|бракова|решени", question):
        return None
    for match in _DEFECT.finditer(question):
        left, right = question[:match.start()], question[match.end():]
        if re.search(r"\b(?:нет|без|отсутстви[ея]|не (?:обнаружено|выявлено))\s*$", left) or re.match(
            r"\s+(?:нет\b|отсутству\w*|не (?:обнаруж\w*|выяв\w*|подтвержд\w*))", right
        ):
            return "Для указанного осмотра механик сообщил об отсутствии дефекта."
    part, = codes
    terms = _words(question)
    matches = {}
    for hit in search.find(f"{part} {_DEFECT.search(question).group()} карта дефектации заключение"):
        for score, excerpt in _rows(hit, part, damage, terms):
            matches.setdefault((score, excerpt), hit)
    if matches:
        best = max(score for score, _ in matches)
        choices = [(excerpt, hit) for (score, excerpt), hit in matches.items() if score == best]
        if len(choices) == 1:
            excerpt, hit = choices[0]
            search.add(hit, excerpt)
            return ""
    return "В карте дефектации не найдена однозначная строка для указанной детали и дефекта."
