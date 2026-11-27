"""Подбирает проверку угла впрыска для ЯМЗ-236М2 по двум страницам РЭ."""

import re

from .qdrant_chunks import clean


_DOCUMENT = "yamz-236m2-re-2010"
_WORK = "Проверить угол опережения впрыска топлива по меткам коленчатого вала и муфты опережения впрыскивания."
_SETUP = re.compile(
    r"\bВращать коленчатый вал двигателя по часовой стрелке "
    r"\(если смотреть со стороны вентилятора\) до совмещения меток "
    r"(?=.{1,520}?\b15\s*[°º])"
    r".{1,660}?(?:\(рис\.\s*16\)\.|установочный угол\s+15\s*[°º]\.)",
    re.I,
)
_SIDE_MARK = re.compile(
    r"Для бокового указателя.{1,430}?букве «Б»\s*(?:[-–—]\s*|соответствует\s+)15\s*[°º]",
    re.I,
)
_CHECK = re.compile(
    r"\bВ момент совмещения меток(?:, показанных на рис\.\s*15 и 16)?,?\s+"
    r"должны совместиться метка на торце муфты(?:\s+2)?(?:\s*\(рис\.\s*17\))?\s+"
    r"опережения впрыскивания с риской на указателе(?:\s+8)?\.",
    re.I,
)
_EARLY_INJECTION = re.compile(r"\bДвигате\s*ль стучит\b.{0,160}?\bРанний впрыск топлива\b", re.I)


def _source_key(hit):
    return hit["document_id"], hit.get("edition"), hit.get("source_sha256")


def select_injection_timing(search, *, explain_cause):
    """Цитирует проверку по меткам и связывает работу с обеими страницами."""
    if search.engine != "ЯМЗ-236М2":
        return False

    allowed = {f"{search.engine}|all"}
    if search.variant:
        allowed.add(f"{search.engine}|{search.variant}")

    def usable(hit, number):
        return (hit["document_id"] == _DOCUMENT and hit["page"] == number
                and search.engine in hit.get("engine_models", ())
                and bool(allowed.intersection(hit.get("applicability", ())))
                and bool(hit.get("source_sha256")))

    starts = []
    for hit in search.find("Совмещение меток коленчатого вала и маховика установочный угол опережения впрыска 15°"):
        if not usable(hit, 30):
            continue
        text = clean(hit["text"])
        setup = _SETUP.search(text)
        if setup:
            starts.append((hit, setup, _SIDE_MARK.search(text)))
    if not starts:
        return False

    # Из двух кусков с одной страницы берём тот, где указана метка бокового указателя.
    starts.sort(key=lambda item: item[2] is not None, reverse=True)
    ends = search.find("В момент совмещения меток опережения впрыскивания метка на торце муфты с риской на указателе")
    for start, setup, mark in starts:
        options = [hit for hit in ends if usable(hit, 31) and _source_key(hit) == _source_key(start)]
        for end in options:
            check = _CHECK.search(clean(end["text"]))
            if not check:
                continue
            if explain_cause:
                for cause in search.find("Двигатель стучит ранний впрыск топлива в цилиндры"):
                    if not usable(cause, 53) or _source_key(cause) != _source_key(start):
                        continue
                    early = _EARLY_INJECTION.search(clean(cause["text"]))
                    if early:
                        search.add(cause, early.group(), kind="cause",
                                   text="Ранний впрыск топлива в цилиндры указан среди возможных причин стука двигателя.")
                        break
            excerpt = setup.group()
            statement = excerpt
            if mark:
                excerpt += " " + mark.group()
                statement += " Для бокового указателя установочному углу 15° соответствует буква «Б»."
            search.add(start, excerpt, text=statement, work=_WORK)
            search.add(end, check.group(), work=_WORK)
            return True
    return False
