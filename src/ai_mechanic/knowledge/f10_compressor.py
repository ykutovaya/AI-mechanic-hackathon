"""Выбирает причину по двум признакам неисправности привода компрессора ЯМЗ-536."""

import re

from .qdrant_chunks import clean


_WORK = "Проверить крепление шестерни на валу компрессора пневмотормозов."


def select_compressor_drive(search, question, symptoms, *, diagnostic=True):
    """Сопоставляет отказ нагнетания воздуха и рост усилия на руле с отдельными строками РЭ."""
    if search.engine != "ЯМЗ-536":
        return None
    component = r"(?:компрессор(?: пневмотормозов)?|насос (?:гур|гидроусилителя руля))"
    refers_to_check = re.fullmatch(
        rf"\s*(?:как |что (?:нужно )?)?проверить {component}(?: (?:или|и) {component})? "
        r"(?:по результат\w* (?:этой )?(?:диагностики|проверки)|по этим признакам)[?!. ]*", question, re.I)
    if not diagnostic and not refers_to_check:
        symptoms = ""
    no_air = re.search(r"давлени\w* в пневмосистеме (?:не раст[её]т|не повышается|раст[её]т медленно)"
                       r"|компрессор не нагнетает воздух", symptoms)
    steering = re.search(r"усилие на рул(?:евом колесе|е) (?:выросло|возросло|увеличилось)"
                         r"|(?:возросло|увеличилось) усилие на руле|руль (?:стал )?тяж[её]л", symptoms)
    gear_question = re.search(r"провер\w* креплени\w* шестерни на валу компрессора", question)
    if not (gear_question or no_air and steering):
        return None

    matches = {}
    hits = search.find("Ослаблено крепление шестерни на валу компрессора пневмотормозов, "
                       "не нагнетает воздух и возросло усилие на руле")
    for hit in hits:
        if hit["document_id"] != "yamz-536-re-2016":
            continue
        for table in hit.get("tables", ()):
            for row in table.get("rows", ()):
                if len(row) != 2 or not row[0]:
                    continue
                cause = clean(row[0])
                point, _, detail = cause.partition(" ")
                if not detail.casefold().startswith("ослаблено крепление шестерни на валу компрессора"):
                    continue
                if (point == "39.3" and "не работает и насос гур" in detail.casefold()
                        or point == "42.2" and "см. п. 39.3" in detail):
                    matches.setdefault(point, (hit, cause))
    if not matches:
        return "В найденных строках руководства нет подтверждения для проверки крепления шестерни на валу компрессора."

    statements = {
        "39.3": "Пункт 39.3 указывает возможную общую причину: при ослаблении крепления шестерни "
                "на валу компрессор не нагнетает воздух и насос ГУР не работает.",
        "42.2": "При возросшем усилии на руле пункт 42.2 называет возможной причиной ослабление крепления "
                "шестерни на валу компрессора и отсылает к пункту 39.3.",
    }
    for point, statement in statements.items():
        if point not in matches:
            continue
        hit, cause = matches[point]
        if point == "39.3" or "39.3" not in matches:
            statement += " " + _WORK
        search.add(hit, cause, text=statement, kind="cause", work=_WORK)
    return ""
