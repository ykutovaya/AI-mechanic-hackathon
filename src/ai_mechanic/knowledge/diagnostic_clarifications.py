"""Поясняет, каких наблюдений или сведений об агрегате не хватает для заявки."""

import re

from .generator_bearing import select_generator_bearing
from .qdrant_chunks import clean
from .sources import verified_pdf_pages


_GEAR_MANUAL = "yamz-236m2-rk-2014"
_GEAR_OPERATING_MANUAL = "yamz-236m2-re-2010"
_GEAR_SPECIFICATION = r"общий для обоих рядов цилиндров, с шестер[её]нчатым приводом"
_GEAR_SENTENCE = "Шестерни распределения и привода ТНВД должны быть установлены по меткам."
_GEAR_EXPLANATION = ("У двигателя ЯМЗ-236М2 шестерённый привод газораспределения. "
                     "Цепь ГРМ и её гидронатяжитель в штатной конструкции не предусмотрены.")
GEAR_TOPIC = "timing-drive-applicability"


def clarify_diagnostic_basis(search, code, question, symptoms, diagnostic, topic=""):
    """Возвращает вопрос о фактах без подстановки ремонта из соседней строки."""
    q = question.casefold()
    if code == "F05" and search.engine == "ЗМЗ-5234.10" and re.search(r"шум|стук|гул", symptoms, re.I):
        return select_generator_bearing(search, question, diagnostic, topic)
    if code == "F06" and search.engine == "ЯМЗ-236М2" and re.search(r"шум|стук|дребез", symptoms, re.I):
        if not diagnostic and not ("сцеплен" in q and re.search(r"провер|шум", q)):
            return None
        prompt = ("Для выбора процедуры нужна модель установленного сцепления. "
                  "Уточните обозначение по паспорту или маркировке и скажите, в каком положении педали исчезает шум.")
        return "clutch-model-question", "", prompt, True
    if code == "F14" and search.engine == "ЯМЗ-236М2":
        if not diagnostic and not re.search(r"\bцеп(?:ь|и|ей|ью)\b|цепн\w*|гидронатяжител\w*", q):
            return None
        search.passage(_GEAR_SENTENCE, re.escape(_GEAR_SENTENCE), explain=lambda _: _GEAR_EXPLANATION,
                       document_id=_GEAR_MANUAL)
        prompt = ("Сверьте модель двигателя и убедитесь, что запись сделана на этой машине. "
                  "Уточните, у какого узла слышен шум и на каких оборотах, затем повторите проверку.")
        return GEAR_TOPIC, "", prompt, True
    return None


def add_gear_drive_source(result, registry, root, engine):
    """Подтверждает тип привода по руководствам по эксплуатации и ремонту."""
    if engine != "ЯМЗ-236М2":
        return
    if not result["evidence"]:
        source = verified_pdf_pages(registry, root, _GEAR_MANUAL, engine, (42,)).get(42)
        if source and _GEAR_SENTENCE in clean(source["text"]):
            result["evidence"].append(dict(source, excerpt=_GEAR_SENTENCE, text=_GEAR_EXPLANATION, kind="fact",
                                           applicability=[f"{engine}|all"], work=None))
    # В текстовом слое таблицы исходный порядок сохраняет фразу в одной ячейке.
    operating_page = verified_pdf_pages(registry, root, _GEAR_OPERATING_MANUAL, engine, (7,), sort=False).get(7)
    if operating_page and (spec := re.search(_GEAR_SPECIFICATION, clean(operating_page["text"]), re.I)):
        text = ("В технической характеристике указан общий для обоих рядов цилиндров распределительный вал "
                "с шестерёнчатым приводом.") if result["evidence"] else _GEAR_EXPLANATION
        result["evidence"].append(dict(operating_page, excerpt=spec.group(), text=text, kind="fact",
                                       applicability=[f"{engine}|all"], work=None))
