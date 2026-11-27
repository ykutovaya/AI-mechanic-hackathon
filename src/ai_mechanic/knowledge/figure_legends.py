"""Находит обозначения позиций в подписи к указанному механиком рисунку."""

import re


_FIGURE_NUMBER = r"(\d+(?:\.\d+)*[а-яa-z]?)\b"
_FIGURE_REFERENCE = re.compile(r"\b(?:рисунк[аеу]|рисунок|рис\.)\s*" + _FIGURE_NUMBER, re.I)
_CAPTION = re.compile(r"^[ \t]*(?:рисунок\b|рис\.)[ \t]*" + _FIGURE_NUMBER, re.M | re.I)
_NUMBERS = r"\d+(?:(?:\s*,\s*|\s+и\s+)\d+)*"
_POSITION = re.compile(r"\b(?:позици\w*|поз\.|канал\w*|выноск\w*)\s*№?\s*(" + _NUMBERS + r")\b", re.I)
_LABEL = re.compile(r"(?:^[ \t]*|[;:][ \t]*)(" + _NUMBERS + r")[ \t]*[–—-][ \t]*", re.M)


def _captions(text, number, direction):
    headings = list(_CAPTION.finditer(text))
    for i, heading in enumerate(headings):
        if heading[1].casefold() != number:
            continue
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        body = text[heading.end():end]
        labels = list(_LABEL.finditer(body))
        if not labels or direction and direction not in body[:labels[0].start()].casefold():
            continue
        for j, marker in enumerate(labels):
            stop = labels[j + 1].start() if j + 1 < len(labels) else len(body)
            label = body[marker.end():stop].strip()
            # За последней позицией могут идти основной текст или печатный номер страницы.
            label = re.split(r";|\n\s*\n|(?<=[.!?])\s+(?=[А-ЯЁ])|\n\s*\d+\s*$", label, maxsplit=1)[0]
            label = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", label)
            label = " ".join(label.split()).rstrip(". ")
            if label:
                yield re.findall(r"\d+", marker[1]), label, marker[0].lstrip(";: \t") + label


def select_figure_positions(search, question):
    """Возвращает пояснение о пропусках либо None для вопросов без номера позиции и рисунка."""
    figure = _FIGURE_REFERENCE.search(question)
    position = _POSITION.search(question)
    if not figure or not position:
        return None
    number = figure[1].casefold()
    requested = list(dict.fromkeys(re.findall(r"\d+", position[1])))
    direction = next((word for word in ("спереди", "сзади", "сверху", "снизу", "слева", "справа")
                      if word in question), "")
    matches = {}
    for hit in search.find(f"Рисунок {number} {search.engine} {direction}".strip()):
        for numbers, label, excerpt in _captions(hit["text"], number, direction):
            for n in requested:
                if n in numbers:
                    key = (hit["document_id"], hit["page"])
                    labels = matches.setdefault(key, {}).setdefault(n, {})
                    labels.setdefault(label.casefold(), (hit, label, excerpt))
    if not matches:
        return f"В найденных подписях к рисунку {number} запрошенные позиции не найдены."
    if len(matches) > 1:
        return f"В руководствах есть несколько рисунков с номером {number}. Уточните страницу или название узла."
    by_number = next(iter(matches.values()))
    missing = []
    selected = {}
    for n in requested:
        labels = by_number.get(n, {})
        if len(labels) != 1:
            missing.append(n)
            continue
        hit, label, excerpt = next(iter(labels.values()))
        _, _, numbers = selected.setdefault(excerpt, (hit, label, []))
        numbers.append(n)
    for excerpt, (hit, label, numbers) in selected.items():
        search.add(hit, excerpt, text=f"{', '.join(numbers)} — {label}.")
    if missing:
        return f"Для рисунка {number} не найдена однозначная подпись к позициям {', '.join(missing)}."
    return ""
