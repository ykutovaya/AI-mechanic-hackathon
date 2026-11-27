"""Формирует дополнительные запросы по вопросу механика и найденным причинам."""

import re


OIL_SIGNAL = "сигнализатор аварийного давления"
OIL_LEVEL = "Проверка уровня масла в двигателе"
OIL_CAUSES = "низкое давление масла возможные причины неисправности"
FUEL_SYSTEM = "Система питания. Тип топливного насоса высокого давления и регулятора частоты вращения"


def mentions_pressure_drop(text):
    direction = r"(?:понижен\w*|низк\w*|недостаточн\w*|падени\w*|упал\w*|пада\w*|пропал\w*|снизи\w*)"
    pressure = r"(?:давлени\w*|напор\w*)"
    return bool(re.search(rf"\b{direction}\s+{pressure}\b|\b{pressure}(?:\s+масла)?\s+(?:{direction}|ниже)\b", text))


def followup_queries(question):
    """Дополняет широкий вопрос поиском по отдельным темам в том же руководстве."""
    q = question.casefold().replace("ё", "е")
    disassembly = re.match(r"\s*(?:(?:подскажите,?\s*)?как\s+(?:правильно\s+)?)?"
                          r"(?:разобрать|разбирать)\s+([^?!.\n]{3,100})(?:[?!.\n]|$)", q)
    if disassembly:
        return ("Разборка " + disassembly[1].strip(),)
    if (re.search(r"вспышк|диагностическ\w*\s+кнопк", q)
            and re.search(r"топлив|тнвд", q) and re.search(r"\bкод\w*\b", q)):
        return (FUEL_SYSTEM,)
    inspection = re.search(r"что\s+(?:\w+\s+){0,2}(?:провер|уточн)|расспрос|начать\s+(?:провер|осмотр)"
                           r"|с чего начать|в первую очередь", q)
    if inspection:
        for clause in re.split(r"[.!?;\n]", q):
            oil = re.search(r"\b(?:давлени\w*|напор\w*)\s+(?:моторного\s+)?масла\b|\bсистем\w*\s+смазки\b", clause)
            if oil and mentions_pressure_drop(clause) and not re.search(r"\bне\s+(?:понижен|низк|упал|пада|снизи)", clause):
                return (OIL_SIGNAL, OIL_LEVEL, OIL_CAUSES)
    return ()

def pump_inspection_queries(cause_hits):
    """Ищет проверку деталей, когда руководство прямо связывает падение давления с износом насоса."""
    wear = r"\bизнос\w*\s+(?:шестер[её]н\w*|детал\w*)[^.!?;|]{0,80}\bмаслян\w*\s+насос\w*"
    for hit in cause_hits:
        text = " ".join(hit.get("text", "").split())
        if re.search(wear, text, re.I):
            return ("Проверка износа деталей масляного насоса при снижении давления масла",
                    "Масляный насос. Проверка деталей на износ")
    return ()
