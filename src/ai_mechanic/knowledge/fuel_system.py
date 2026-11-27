"""Сверяет штатную топливную систему по руководству выбранного двигателя."""

import re

from . import sources
from .qdrant_chunks import clean


_ENGINE_SCOPE = r"(?:на|в|у)\s+(?:(?:этом|этого|данном|данного|нашем|мо[её]м)\s+)?(?:двигател[еяь]|мотор[ае]?)"
_QUESTION = re.compile(
    rf"(?:а\s+)?(?:{_ENGINE_SCOPE}\s+)?"
    r"(?:(?:(?:какой|какая (?:марка|модель)|марка|модель|есть(?: ли)?|"
    r"установлен ли|предусмотрен ли)\s+)?карбюратор(?:а)?(?:\s+(?:стоит|установлен|предусмотрен|есть))?"
    r"|какая система (?:питания|подачи топлива))"
    rf"(?:\s+{_ENGINE_SCOPE})?",
    re.I,
)
_DISTRIBUTED_INJECTION = (
    "zmz-40904-40905-rk-2025",
    {72: r"Подача топлива осуществляется посредством распредел[её]нного впрыска топлива "
         r"во впускные каналы головки цилиндров[^.]{0,130}электромагнитными форсунками"},
    "На {engine} карбюратор не предусмотрен. Руководство описывает распределённый впрыск "
    "бензина электромагнитными форсунками во впускные каналы. [1]",
)
_LAYOUT = {
    "ЯМЗ-236М2": (
        "yamz-236m2-re-2010",
        {6: r"Способ смесеобразования Непосредственный впрыск",
         8: r"Топливный насос высокого давления\b.*\bФорсунки\b"},
        "На {engine} карбюратор не предусмотрен. Это дизель с непосредственным впрыском. [1] "
        "Топливо подаётся через ТНВД и форсунки. [2]",
    ),
    "ЯМЗ-536": (
        "yamz-536-re-2016",
        {126: r"Топливная система аккумуляторного типа\s*[–—-]\s*Common Rail System\s*\(CRS\)"},
        "На {engine} карбюратор не предусмотрен. Здесь применяется аккумуляторная система "
        "впрыска дизельного топлива Common Rail. [1]",
    ),
    "ЗМЗ-5234.10": (
        "zmz-5234-10-rk-2025",
        {36: r"На двигателе установлен двухкамерный карбюратор К-135МУ\."},
        "На {engine} штатно установлен двухкамерный карбюратор К-135МУ. [1]",
    ),
    "ЗМЗ-40904.10": _DISTRIBUTED_INJECTION,
    "ЗМЗ-40905.10": _DISTRIBUTED_INJECTION,
}


def fuel_system_reference(registry, root, engine, question):
    """Возвращает справку и ссылки, если все сведения подтверждены в оригинале."""
    question = " ".join(question.strip(" ?!.").split())
    if engine not in _LAYOUT or not _QUESTION.fullmatch(question):
        return None
    document, anchors, answer = _LAYOUT[engine]
    # У повернутых таблиц порядок ячеек сохраняется только без сортировки по координатам.
    pages = sources.verified_pdf_pages(registry, root, document, engine, tuple(anchors), sort=False)
    if any(page not in pages or not re.search(pattern, clean(pages[page]["text"]), re.I)
           for page, pattern in anchors.items()):
        return None
    try:
        references = [sources.resolve_source(pages[page], registry, root) for page in anchors]
    except sources.SourceUnavailable:
        return None
    return answer.format(engine=engine), references
