"""Подбирает сведения о проверке форсунок по руководствам ЯМЗ."""

import re

from .qdrant_chunks import clean


_PARTS = {
    241: re.compile(
        r"№\s*10\s*[.:]?\s*ТЕСТ ФОРСУНОК\s+"
        r"1\.\s*Подключить\b.{1,200}?\bЭСУ\.\s*"
        r"2\.\s*Провести тест форсунок\b.{1,350}?\bKTS\)\.",
        re.I,
    ),
    242: re.compile(
        r"\b3\.\s*Вычислить разницу\b.{1,330}?"
        r"\b4\.\s*Определить среднее значение\b.{1,330}?"
        r"\b5\.\s*Отклонение от среднего значения не должно превышать\s*"
        r"±\s*\d+(?:[.,]\d+)?\s*мин\s*[-−⁻]\s*[1¹]\.",
        re.I,
    ),
}


def select_injector_test(search):
    """Добавляет работу только при наличии обеих частей одной редакции РЭ."""
    pairs = {f"{search.engine}|all"}
    if search.variant:
        pairs.add(f"{search.engine}|{search.variant}")
    hits = search.find("№ 10 Тест форсунок подключить диагностический прибор АСКАН-10 KTS")
    hits += search.find("Тест форсунок вычислить разницу частоты вращения до и после отключения цилиндров ±15 мин-1")
    by_revision = {}
    for hit in hits:
        part = _PARTS.get(hit["page"])
        if (not part or hit["document_id"] != "yamz-536-re-2016"
                or search.engine not in hit["engine_models"]
                or not pairs.intersection(hit.get("applicability", ()))
                or not hit.get("source_sha256")):
            continue
        match = part.search(clean(hit["text"]))
        if match:
            key = hit["edition"], hit["source_sha256"]
            by_revision.setdefault(key, {}).setdefault(hit["page"], (hit, match[0]))

    for pages in by_revision.values():
        if pages.keys() != _PARTS.keys():
            continue
        for page in _PARTS:
            hit, excerpt = pages[page]
            search.add(hit, excerpt, work="Провести тест форсунок № 10.")
        return True
    return False


_INJECTOR = r"(?:топливн\w*\s+)?форсун(?:к\w*|ок)"
_INJECTOR_LOOKUP = re.compile(
    rf"(?:(?:как\s+(?:проверить|отрегулировать)|проверк[ауи]|регулировк[ауи]|обслуживание|"
    rf"(?:признаки\s+)?(?:дефект|неисправност|засорени)\w*)\s+)?{_INJECTOR}"
)
_INJECTOR_PRESSURE_QUESTION = re.compile(
    rf"(?:(?:какое|нормативное)\s+)?давлени\w*\s+начала\s+впрыск(?:а|ивания)\s+{_INJECTOR}"
)
_INJECTOR_SEALS_QUESTION = re.compile(
    rf"(?:как\s+проверить\s+|проверка\s+)?герметичност[ьи]\s+соединени[яй]\s+{_INJECTOR}"
)
_INJECTOR_PRESSURE = re.compile(
    r"После длительной работы на двигателе допускается снижение давления начала впрыскивания "
    r"форсунок до \d+[,.]\d+ МПа \(\d+ кгс/см[2²]\)\. При обслуживании форсунок, "
    r"замене хотя бы одной форсунки, все форсунки отрегулировать на давление начала "
    r"впрыскивания \d+[,.]\d+\s*\+\s*\d+[,.]\d+ МПа \(\d+\s*\+\s*\d+ кгс/см[2²]\)\.",
    re.I,
)
_INJECTOR_FAULT = re.compile(
    r"Нарушение регулировки или (?:"
    r"засорение форсунки\s*\|?\s*Отрегулиро\s*вать форсунку и, есл\s*и "
    r"|Отрегулиро\s*вать форсунку и, есл\s*и засорение форсунки )"
    r"необходимо, промыть и прочистить е[её]\b",
    re.I,
)


def select_yamz236_injector_reference(search, question):
    """Показывает нормы и условия проверки форсунок из применимого руководства."""
    if search.engine != "ЯМЗ-236М2":
        return False
    q = " ".join(question.casefold().strip(" ?!.").split())
    if _INJECTOR_SEALS_QUESTION.fullmatch(q):
        return search.passage(
            "герметичность соединений форсунки проверять безмоторный стенд КИ-562",
            r"При необходимости герметичность соединений форсунки проверять на "
            r"безмоторном стенде или приборе КИ-562 в течение 10 минут\.",
            document_id="yamz-236m2-rk-2014",
        )
    pressure_only = _INJECTOR_PRESSURE_QUESTION.fullmatch(q)
    if not pressure_only and not _INJECTOR_LOOKUP.fullmatch(q):
        return False

    document = "yamz-236m2-re-2010"
    found = False
    for hit in search.find("Обслуживание форсунок давление начала впрыскивания"):
        if hit["document_id"] != document or hit["page"] != 37:
            continue
        if match := _INJECTOR_PRESSURE.search(clean(hit["text"])):
            search.add(hit, match[0])
            found = True
            break
    if pressure_only:
        return found

    for hit in search.find("Двигатель не развивает мощности дымит нарушение регулировки засорение форсунки"):
        if hit["document_id"] != document or hit["page"] != 52:
            continue
        body = clean(hit["text"])
        # Текстовый слой чередует колонки причины и действия в пределах одной строки.
        if (re.search(r"Двигатель не разви\s*вает мощности, дымит", body, re.I)
                and (match := _INJECTOR_FAULT.search(body))):
            search.add(hit, match[0], text=(
                "Если двигатель не развивает мощности и дымит, возможная причина — "
                "нарушение регулировки или засорение форсунки. Руководство указывает "
                "отрегулировать форсунку и, если необходимо, промыть и прочистить её."
            ))
            return True
    return found
