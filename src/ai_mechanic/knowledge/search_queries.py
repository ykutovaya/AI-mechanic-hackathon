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


def inspection_followup(code, question):
    """Узнаёт общий вопрос о текущем отклонении, сохраняя явно заданную тему."""
    subjects = {
        "F02": ("давлен", "масл", "низк", "понижен", "паден", "предупрежд"),
        "F06": ("сцеплен", "признак", "неисправност", "симптом", "случа", "моём", "моем"),
        "F03": ("перегре", "охлаждающ", "жидкост", "подозрен", "масл", "антифриз", "ож"),
        "F08": ("дизел", "топлив", "подач", "систем"),
        "F13": ("бензин", "топлив", "подач", "поступ", "подаёт", "подает", "систем", "питани", "моём", "моем", "случае"),
    }
    if code not in subjects or not re.search(r"провер|осмотр|предупрежд|с чего начать", question):
        return False
    if code == "F02" and not ("давлен" in question and "масл" in question
                              and (mentions_pressure_drop(question) or "предупрежд" in question)):
        return False
    if code == "F06" and "сцеплен" not in question:
        return False
    if code == "F03" and "перегре" not in question:
        return False
    if code == "F08" and not ("дизел" in question and "топлив" in question):
        return False
    if code == "F13" and not re.search(r"бензин|топливоподач|подач\w*\s+топлив|систем\w*\s+питания", question):
        return False
    common = ("провер", "осмотр", "начать", "двигател", "машин", "диагност", "результат",
              "перв", "очеред", "сначал", "нужн", "требуют", "рекоменд", "подскаж", "каки", "како")
    words = re.findall(r"[а-яёa-z0-9]+", question)
    service = {"а", "и", "в", "во", "на", "по", "к", "у", "с", "со", "о", "об", "для", "при",
               "как", "что", "чего", "если", "даны", "этой", "этот", "этом", "этого", "эту"}
    return all(word in service or word.startswith(subjects[code] + common) for word in words)


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
