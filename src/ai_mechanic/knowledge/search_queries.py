"""Формирует дополнительные запросы для первичного осмотра и проверки типа топливной аппаратуры."""

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
