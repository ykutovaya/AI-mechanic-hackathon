"""Отбор наблюдаемых признаков и проверки контактов зажигания для F12."""

import re


# В описание ML попадают и служебные замечания. Для F12 передаём дальше
# только целые фразы о наблюдаемых признаках из диагностического описания.
_CYLINDER = (r"(?:в (?:одном|первом|втором|третьем|четв[её]ртом|пятом|шестом|седьмом|восьмом) цилиндре"
             r"|в цилиндре (?:№\s*)?[1-8])")
_MISFIRES = rf"(?:пропуски(?: воспламенения| зажигания)?|неравномерная работа с пропусками) {_CYLINDER}"
_OBSERVATION = re.compile(
    rf"{_MISFIRES}|(?:неровный|неустойчивый) холостой ход|двигатель тряс[её]т(?:ся)? на холостом ходу"
    rf"|пропусков (?:воспламенения |зажигания )?{_CYLINDER} не обнаружено", re.I)
_CONTACTS = "Проверить чистоту и плотность контактов в системе зажигания."


def ignition_observations(description):
    """Оставляет целые диагностические фразы, сохраняя отрицания и режим работы."""
    clauses = (part.strip() for part in re.split(r"[,;.!?\n]+", description))
    return ", ".join(dict.fromkeys(part for part in clauses if _OBSERVATION.fullmatch(part)))


def select_ignition_contacts(search, question, symptoms, *, diagnostic):
    """Подбирает проверку контактов при перебоях в одном цилиндре по пункту 2.1."""
    if search.engine not in {"ЗМЗ-40904.10", "ЗМЗ-40905.10"}:
        return None
    about_ignition = diagnostic or re.search(r"как проверить (?:систему )?зажигани[ея]\b", question)
    if about_ignition and not re.search(_MISFIRES, symptoms, re.I):
        return "Уточните, есть ли пропуски воспламенения и в каком цилиндре они наблюдаются."
    if not (about_ignition
            or re.search(r"провер\w*.*контакт\w* в системе зажигания", question)):
        return None

    hits = search.find("Перебои или отказ в работе одного из цилиндров двигателя. "
                       "Нарушение или загрязнение контактов в системе зажигания.")
    for hit in hits:
        if not re.search(r"\b2\.1\.?\s+Перебои или отказ в работе одного из цилиндров",
                         hit.get("section", ""), re.I):
            continue
        for table in hit.get("tables", ()):
            for row in table.get("rows", ()):
                if len(row) != 2:
                    continue
                cause, action = (str(cell or "").casefold() for cell in row)
                if ("загрязнение контактов в системе зажигания" in cause
                        and "загрязнение или неплотности контактов" in action):
                    search.add(hit, " | ".join(row),
                               text="Пункт 2.1 указывает возможную причину перебоев в одном цилиндре — "
                                    "загрязнение или нарушение контакта в системе зажигания. " + _CONTACTS,
                               work=_CONTACTS)
                    return ""
    return "В найденных строках руководства нет проверки контактов зажигания для перебоев в одном цилиндре."
