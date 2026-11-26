"""Разметка применимости пунктов и переходов между страницами руководства."""

import re


def _marker(text, phrase, offset=0):
    if not isinstance(phrase, str) or not phrase.strip():
        raise ValueError("Пустой маркер фрагмента")
    pattern = r"\s+".join(re.escape(word) for word in phrase.split())
    match = re.compile(pattern, flags=re.IGNORECASE).search(text, offset)
    if match is None:
        raise ValueError(f"В тексте отсутствует маркер «{phrase}»")
    return match


def _models(value, allowed):
    if not isinstance(value, list) or not value or not all(m in allowed for m in value):
        raise ValueError(f"Укажите модели из реестра: {allowed}")
    return value


def _related(numbers, page, total):
    if not isinstance(numbers, list) or any(
        type(n) is not int or n == page or not 1 <= n <= total for n in numbers
    ):
        raise ValueError(f"Некорректная ссылка со страницы {page}: {numbers}")
    return sorted(set(numbers))


def _fragment(row, item, total):
    text = row["text"]
    start = _marker(text, item["start"])
    end = _marker(text, item["end"], start.start()).end() if "end" in item else len(text)
    return {
        "text": text[start.start():end],
        **({"applicability": item["applicability"]} if "applicability" in item else {}),
        "engine_models": _models(item["engine_models"], row["engine_models"]),
        "condition": item.get("condition", ""),
        "related_pages": _related(item.get("related_pages", []), row["page"], total),
    }


def apply_notes(rows, notes):
    """Связывает физические страницы и выделяет размеченные пункты в исходном тексте."""
    by_page = {}
    for note in notes:
        p = note["page"]
        if type(p) is not int or not 1 <= p <= len(rows) or p in by_page:
            raise ValueError(f"Повтор или неверный номер страницы в разметке: {p}")
        by_page[p] = note

    links = [set() for row in rows]
    for row in rows:
        page = row["page"]
        note = by_page.get(page, {})
        try:
            if "source_sha256" in note and note["source_sha256"] != row["source_sha256"]:
                raise ValueError("Разметка относится к другой версии PDF")
            if "section" in note:
                row["section"] = note["section"]
            if "fragments" in note:
                row["fragments"] = [_fragment(row, f, len(rows)) for f in note["fragments"]]
            neighbors = _related(note.get("related_pages", []), page, len(rows))
            if page > 1 and re.search(
                r"(?im)^\s*(?:продолжение|окончание)\s+табл(?:ицы|иц|\.?)\s*\d",
                row["text"][:1000],
            ):
                neighbors.append(page - 1)
            for neighbor in neighbors:
                links[page - 1].add(neighbor)
                links[neighbor - 1].add(page)
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError(f"{row['document_id']}, стр. {page}: {exc}") from exc
    for row, neighbors in zip(rows, links):
        row["related_pages"] = sorted(neighbors)


def apply_review(row, note):
    """Сохраняет проверенную расшифровку рядом с исходным выводом OCR."""
    review = note.get("verified_ocr")
    if review is None:
        return
    ocr = row.get("ocr")
    if not ocr or note.get("source_sha256") != row["source_sha256"]:
        raise ValueError(f"Для сверки OCR на стр. {row['page']} нужны распознавание и SHA-256 этого PDF")
    if not isinstance(review.get("text"), str) or not review["text"].strip():
        raise ValueError("Укажите проверенный текст с изображения")
    ocr["raw_text"] = ocr.get("raw_text", ocr["text"])
    ocr.update(text=review["text"], status="verified",
               engine_models=_models(review["engine_models"], row["engine_models"]))
