"""Контрольный замер давления масла и первичные проверки для ЯМЗ-536."""

from decimal import Decimal
import re

from .yamz236_oil import (
    OIL_MEASUREMENT_QUESTION, OIL_MEASUREMENT_REQUEST, measured_oil_pressure, oil_manual_excerpt, oil_measurement_text,
)


YAMZ536_CHECK_ID = "S-0096"
YAMZ536_OIL_TOPIC = "yamz536-oil-observation"
YAMZ536_OIL_CONFIRMED_TOPIC = YAMZ536_OIL_TOPIC + ":confirmed"
_MANUAL = "yamz-536-re-2016"
YAMZ536_OIL_NORM_SOURCE = (_MANUAL, 49)
YAMZ536_OIL_WORK_SOURCES = frozenset({YAMZ536_OIL_NORM_SOURCE, (_MANUAL, 218), (_MANUAL, 219)})
YAMZ536_OIL_WORKS = (
    "Проверить уровень масла в картере",
    "Проверить соединения и уплотнения маслопроводов",
)
_LIMIT = Decimal("400")
_CHECK = (
    "Проверьте уровень масла и охлаждающей жидкости. "
    "Согласуйте с механиком возможность контрольного замера."
)
_METHOD = "Проверить давление масла эталонным манометром или диагностическим прибором."
_METHOD_MISSING = "Процедура контрольного замера давления масла в найденных фрагментах не указана."
_NORM_QUERY = "Давление масла в системе смазки двигателя, прогретого до 80–100 °С, на номинальной частоте вращения 400–635 кПа"
_NORM = (
    r"Давление масла в системе смазки двигателя,\s*прогретого до 80\s*[-–—…]\s*100\s*[°º]\s*[СC],"
    r".{0,130}?при номинальной частоте вращения\s*(?:\|\s*)?400\s*[-–—…]\s*635"
)
_NORM_TEXT = "Для двигателя, прогретого до 80–100 °C, давление масла на номинальных оборотах составляет 400–635 кПа."
_LEVEL_QUERY = "14.1 Недостаточно масла в масляном картере ниже нижней метки маслощупа долить масло до нормы"
_LEVEL = (
    r"14\.1\s+Недостаточно масла в масляном картере\s*\(ниже нижней метки маслощупа\)"
    r"\s*(?:\|\s*)?Долить масло до нормы"
)
_LEAK_QUERY = "14.7 Негерметичны соединения маслопроводов Проверить соединения и уплотнения"
_LEAK = (
    r"14\.7\s+Негерметичны соединения маслопроводов\s*(?:\|\s*)?"
    r"Проверить соединения и уплотнения"
)
_METHOD_QUERY = "14.2 Проверить давление масла эталонным манометром или диагностическим прибором"
_METHOD_LINE = (
    r"14\.2\s+Неисправен датчик или указатель давления\s*\(дублирующие приборы\)\s*"
    r"(?:\|\s*)?Проверить давление масла эталонным манометром или диагностическим прибором"
    r"(?:,\s*при необходимости,?\s*заменить датчик или указатель давления)?"
)
_TEMPERATURE = re.compile(
    r"(?<![\w.,<>≤≥+−–—-])(-?\d+(?:[.,]\d+)?)\s*(?:[°º]\s*[сc]|градус\w*)\b"
)
_APPROXIMATE = re.compile(
    r"(?:\b(?:выше|ниже|больше|меньше|около|порядка|не менее|не более)|[<>≤≥])\s*"
    r"-?\d+(?:[.,]\d+)?\s*(?:кпа|мпа|[°º]\s*[сc]|градус\w*)\b"
    r"|\d\s*[-–—…]\s*\d+(?:[.,]\d+)?\s*(?:кпа|мпа|[°º]\s*[сc]|градус\w*)\b"
)
_OTHER_TEMPERATURE = re.compile(r"\bтемператур\w*\s+(?:окружающ\w*|воздух\w*|масл\w*|топлив\w*)")
_SENSOR_REPORT = re.compile(r"\b(?:сигнал\w*|показани\w*)\s+датчик\w*|\bдатчик\s+показал\w*|\bпо\s+датчик\w*")
_CONTROL_MEASUREMENT = re.compile(r"\b(?:измер\w*|замер\w*|манометр\w*|диагностическ\w+\s+прибор\w*)")
_CHANGE = re.compile(
    r"\b(?:на холост\w*|не\s+(?:на|при)\s+номинальн\w*|двигател\w*\s+(?:был\s+)?не\s+прогрет\w*)\b"
)
_UNKNOWN_CONDITION = re.compile(
    r"(?:^|[.!;,]\s*)(?:(?:температур\w*|обороты)(?:\s+(?:замера|при замере|прогрева|двигателя))?"
    r"\s+(?:не знаю|неизвест\w*)|не знаю\s+(?:температур\w*|обороты))\b"
)


def _observation(question):
    question = question.casefold().replace("ё", "е").strip()
    if not question.endswith("?") and _UNKNOWN_CONDITION.search(question):
        return "incomplete", None, None
    statement = oil_measurement_text(question)
    measurement = measured_oil_pressure(statement, _LIMIT)
    if not measurement:
        if _TEMPERATURE.search(statement) or _CHANGE.search(statement):
            return "incomplete", None, None
        return None
    status, pressure = measurement
    if status in {"unmeasured", "incomplete", "regime"}:
        return status, pressure, None
    if _SENSOR_REPORT.search(statement) and not _CONTROL_MEASUREMENT.search(statement):
        return "incomplete", pressure, None
    temperatures = {Decimal(value.replace(",", ".")) for value in _TEMPERATURE.findall(statement)}
    if len(temperatures) != 1 or _APPROXIMATE.search(statement) or _OTHER_TEMPERATURE.search(statement):
        return "incomplete", pressure, None
    temperature = next(iter(temperatures))
    if not 80 <= temperature <= 100:
        return "temperature", pressure, temperature
    return status, pressure, temperature


def yamz536_oil_observation_question(question):
    """Распознаёт сообщение механика о фактическом замере и его условиях."""
    return _observation(question) is not None


def _format_reading(value):
    return format(value, "f").replace(".", ",")


def _measurement_reply(state, pressure, temperature):
    if state in {"confirmed", "sufficient", "temperature"}:
        prompt = (
            "Механик сообщил о замере на номинальных оборотах. "
            f"Давление масла составило {_format_reading(pressure)} кПа "
            f"при температуре прогрева {_format_reading(temperature)} °C."
        )
        if state == "sufficient":
            return prompt + " Условие «ниже 400 кПа» не выполнено. Уточните источник гула."
        if state == "temperature":
            return prompt + " Для сравнения с порогом 400 кПа нужен замер при прогреве до 80–100 °C."
        return prompt
    if state in {"regime", "incomplete"}:
        return OIL_MEASUREMENT_REQUEST
    if state == "unmeasured":
        return _CHECK
    return f"{OIL_MEASUREMENT_QUESTION} {_CHECK}"


def select_yamz536_oil_measurement(search, question, topic, *, general, asks_measurement):
    """Связывает первичные работы с фактом замера и конкретными строками руководства."""
    related = topic.partition(":")[0] == YAMZ536_OIL_TOPIC
    observation = _observation(question) if related else None
    named_work = next((work for work in YAMZ536_OIL_WORKS
                       if question.strip(" .!?").casefold() == work.casefold()), None)
    verified_work = named_work if topic == YAMZ536_OIL_CONFIRMED_TOPIC else None
    if not (observation or general or asks_measurement or verified_work):
        return None

    state, pressure, temperature = observation or ("initial", None, None)
    confirmed = state == "confirmed" or bool(verified_work)
    norm = oil_manual_excerpt(search, _MANUAL, 49, _NORM_QUERY, _NORM)
    works = {}
    if confirmed and verified_work in (None, YAMZ536_OIL_WORKS[0]):
        level = oil_manual_excerpt(search, _MANUAL, 218, _LEVEL_QUERY, _LEVEL)
        if level:
            works[YAMZ536_OIL_WORKS[0]] = level
    if confirmed and verified_work in (None, YAMZ536_OIL_WORKS[1]):
        leak = oil_manual_excerpt(search, _MANUAL, 219, _LEAK_QUERY, _LEAK)
        if leak:
            works[YAMZ536_OIL_WORKS[1]] = leak

    supported = confirmed and norm and len(works) == (1 if verified_work else 2)
    if norm:
        search.add(*norm, text=_NORM_TEXT, work=next(iter(works)) if supported else None)
    for work, source in works.items():
        search.add(*source, text=work + ".", work=work if supported else None)

    if supported:
        prompt = "" if verified_work else _measurement_reply(state, pressure, temperature)
        return YAMZ536_OIL_CONFIRMED_TOPIC, "", prompt, False
    if confirmed:
        return (YAMZ536_OIL_TOPIC, "В найденных фрагментах не подтверждены нормы и работы для черновика.",
                _measurement_reply(state, pressure, temperature), True)

    method = oil_manual_excerpt(search, _MANUAL, 218, _METHOD_QUERY, _METHOD_LINE)
    if method:
        text = _METHOD
        if asks_measurement and "заменить" in method[1].casefold():
            text += " Если проверка подтвердит неисправность датчика или указателя, замените неисправный прибор."
        search.add(*method, text=text)
    missing = "" if method else _METHOD_MISSING
    return YAMZ536_OIL_TOPIC, missing, _measurement_reply(state, pressure, temperature), True
