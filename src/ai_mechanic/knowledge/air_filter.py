"""Отвечает по обслуживанию воздушного фильтра с учётом его типа."""

import re

from . import sources
from .qdrant_chunks import clean


AIR_FILTER_TOPIC = "air-filter-reference"
_MANUAL = "yamz-236m2-re-2010"
_SUBJECT = re.compile(r"\b(?:воздушн\w*\s+фильтр\w*|фильтр\w*\s+воздуха)\b")
_SERVICE = re.compile(r"\b(?:поменять|заменить|сменить|снять|установить|обслужить|замен[аыуе]|"
                      r"смен[аыуе]|обслуживани[ея]|сняти[ея]|установк[аиу])\b")
_DRY = r"сух(?:ой|ого|ому|им|ом)(?:\s+типа)?"
_OIL_BATH = r"инерционно\s*-\s*маслянн?(?:ый|ого|ому|ым|ом)(?:\s+типа)?"
_TYPE_REPLY = re.compile(rf"(?:у меня\s+|стоит\s+)?(?:{_DRY}|{_OIL_BATH})")
_ANCHORS = {
    9: (r"Воздушный фильтр Инерционно\s*-\s*масляный или сухого типа",),
    43: (r"Снять крышку фильтра, извлечь фильтрующий элемент, промыть его",
         r"Вымыть масляную ванну и залить в не[её] масло до метки"),
    44: (r"снять бункер, отвернуть гайку крепления и вынуть элемент из корпуса фильтра",
         r"Прокладки, имеющие надрывы, заменить",
         r"при использовании двух фильтрующих элементов установка проставки между ними обязательна",
         r"стрелка на бункере должна быть направлена вверх"),
    45: (r"при установке нового проверить его состояние визуально, подсвечивая изнутри лампой",),
}


def air_filter_reference(registry, root, engine, question, *, topic):
    """Показывает применимые шаги и предлагает сверить маркировку, если тип неизвестен."""
    q = " ".join(question.casefold().translate(str.maketrans("–—‑", "---")).strip(" ?!.").split())
    if engine != "ЯМЗ-236М2":
        return None
    requested = (_SUBJECT.search(q) and _SERVICE.search(q)
                 and not re.search(r"\bкорпус\w*\s+воздушн\w*\s+фильтр", q))
    if not requested and not (topic == AIR_FILTER_TOPIC and _TYPE_REPLY.fullmatch(q)):
        return None

    dry = bool(re.search(rf"\b{_DRY}\b", q))
    oil_bath = bool(re.search(rf"\b{_OIL_BATH}\b", q))
    unknown = dry == oil_bath
    required = [9] if unknown else []
    if dry or unknown:
        required.extend((44, 45))
    if oil_bath or unknown:
        required.append(43)

    pages = sources.verified_pdf_pages(registry, root, _MANUAL, engine, tuple(p for p in required if p != 9))
    if unknown:
        pages.update(sources.verified_pdf_pages(registry, root, _MANUAL, engine, (9,), sort=False))
    failure = "Не удалось сверить порядок обслуживания воздушного фильтра по РЭ ЯМЗ-236М2."
    for page in required:
        if page not in pages or not all(re.search(pattern, clean(pages[page]["text"]), re.I)
                                        for pattern in _ANCHORS[page]):
            return failure, [], False
    try:
        references = [sources.resolve_source(pages[page], registry, root) for page in required]
    except sources.SourceUnavailable:
        return failure, [], False
    links = {page: f"[{index}]" for index, page in enumerate(required, 1)}
    lines = []
    if unknown:
        lines.append("РЭ ЯМЗ-236М2 допускает сухой и инерционно-масляный фильтры. "
                     f"Уточните тип или перепишите маркировку с корпуса фильтра. {links[9]}")
    if dry or unknown:
        lines.append("Сухой фильтр. Снимите бункер, отверните гайку крепления и выньте старый элемент. "
                     f"Новый элемент подходящей маркировки перед установкой осмотрите, подсвечивая изнутри лампой. {links[45]} "
                     "При сборке замените надорванные прокладки. Если элементов два, установите между ними проставку. "
                     f"Стрелка на бункере должна смотреть вверх. {links[44]}")
    if oil_bath or unknown:
        lines.append("Инерционно-масляный фильтр. Выньте и промойте фильтрующий элемент, промойте масляную ванну, "
                     f"залейте масло до метки и соберите фильтр. {links[43]}")
    return "\n\n".join(lines), references, unknown
