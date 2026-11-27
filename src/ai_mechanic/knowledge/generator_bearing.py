"""Уточнение по подшипнику генератора ЗМЗ-5234.10 и проверка источника работы."""

import re

from .qdrant_chunks import clean


GENERATOR_BEARING_TOPIC = "generator-noise-observation"
BEARING_CONFIRMED = GENERATOR_BEARING_TOPIC + ":bearing"
ROTOR_CONFIRMED = GENERATOR_BEARING_TOPIC + ":rotor"
_ROTOR_WITHOUT_BEARING = GENERATOR_BEARING_TOPIC + ":rotor-only"
GENERATOR_BEARING_WORK = "Заменить подшипник генератора"
GENERATOR_BEARING_SOURCE = ("zmz-5234-10-rk-2025", 57)
GENERATOR_ROTOR_REASON = "Механик сообщил, что при проверке обнаружено задевание ротора генератора."
_BEARING_QUERY = "Неисправен подшипник генератора заменить подшипник"
_BEARING_ROW = re.compile(r"Неисправен подшипник генератора\s*(?:\|\s*)?Заменить подшипник\b", re.I)
_BEARING_QUESTION = (
    "Удалось ли локализовать шум в генераторе? "
    "Удалось ли подтвердить, что шум идёт от подшипника генератора и подшипник неисправен?"
)
_CHECK_GENERATOR = "Рекомендую проверить генератор, локализовать шум и исключить ослабление крепления."


_GENERATOR_OR_END = r"(?:\s+генератора\b|(?=\s*(?:$|!|и\b)))"
_TENTATIVE = re.compile(
    r"\b(?:если|возможно|кажется|мог|могло|может|подозреваю|сомневаюсь|предположительно|"
    r"не (?:(?:мог|смог|удал)\w*\s+)?исключ\w*|не уверен|не думаю|не подтверждено, что|удалось ли)\b"
)


def _reported_observations(question):
    bearing = rotor = None
    for sentence in re.findall(r"[^.!?;]+[!?]?", question.casefold().replace("ё", "е")):
        if "?" in sentence or _TENTATIVE.search(sentence):
            continue
        for clause in re.split(r",|\bно\b", sentence):
            if re.search(r"\b(?:задевани\w*\s+ротора(?:\s+генератора)?\s+(?:нет|не обнаруж\w*|не выяв\w*|не подтверж\w*)|"
                         rf"(?:не обнаруж\w*|не выяв\w*)\s+задевани\w*\s+ротора{_GENERATOR_OR_END}|"
                         r"ротор(?:\s+генератора)?\s+не\s+задевает)\b", clause):
                rotor = False
            elif re.search(r"\b(?:ротор(?:\s+генератора)?\s+задевает|"
                           rf"(?:обнаружил[аи]?|выявил[аи]?|подтвердил[аи]?|есть)\s+"
                           rf"задевани\w*\s+ротора{_GENERATOR_OR_END}|"
                           r"задевани\w*\s+ротора(?:\s+генератора)?\s+(?:подтвержд\w*|обнаруж\w*))\b", clause):
                rotor = True
            unconfirmed = re.search(
                r"\b(?:неисправност\w*|дефект)\s+(?:этого\s+)?подшипник\w*(?:\s+генератора)?\s+не\s+подтвер\w*|"
                r"\bподшипник\w*(?:\s+генератора)?\s+(?:еще\s+)?не\s+(?:провер\w*|неисправ\w*)|"
                rf"\bне\s+(?:(?:удалось|смогли)\s+)?подтверд\w*\b.{{0,70}}\bподшипник\w*{_GENERATOR_OR_END}|"
                r"\bподшипник(?:\s+генератора)?\s+исправен\b", clause)
            if unconfirmed or re.fullmatch(r"\s*не (?:(?:удалось|смогли) подтвердить|подтвердили)\s*!?\s*", clause):
                bearing = False
            elif re.search(
                rf"\b(?:(?:подтвердил[аи]?|обнаружил[аи]?|выявил[аи]?|установил[аи]?)\s+"
                rf"(?:неисправност\w*|дефект)\s+подшипник\w*{_GENERATOR_OR_END}|"
                r"(?:неисправност\w*|дефект)\s+подшипник\w*(?:\s+генератора)?\s+(?:подтвержд|обнаруж|выяв)\w*|"
                r"подшипник(?:\s+генератора)?\s+неисправен)\b", clause):
                bearing = True
    return bearing, rotor


def generator_observation_question(question):
    """Распознаёт сообщения механика, которые меняют основание для заявки."""
    return any(value is not None for value in _reported_observations(question))


def _add_bearing_source(search, hits, confirmed):
    for hit in hits:
        if (hit["document_id"], hit["page"]) != GENERATOR_BEARING_SOURCE:
            continue
        excerpt = _BEARING_ROW.search(clean(hit["text"]))
        if excerpt:
            statement = GENERATOR_BEARING_WORK + "." if confirmed else (
                "В таблице замена подшипника относится к установленной неисправности подшипника.")
            search.add(hit, excerpt.group(), text=statement, work=GENERATOR_BEARING_WORK if confirmed else None)
            return True
    return False


def select_generator_bearing(search, question, diagnostic, topic):
    """Выбирает следующий шаг по наблюдениям механика и применимой строке руководства."""
    q = question.casefold()
    related_topic = topic.partition(":")[0] == GENERATOR_BEARING_TOPIC
    bearing, rotor = _reported_observations(question) if related_topic else (None, None)
    observed = bearing is not None or rotor is not None
    rotor_ruled_out = rotor is False
    work_question = q.strip(" .!?") == GENERATOR_BEARING_WORK.casefold()
    voltage = "напряжени" in q and bool(re.search(r"измер|замер|услови|оборот|нагрузк", q))
    if not (diagnostic or "генератор" in q and (voltage or re.search(r"провер|шум|гул", q))
            or observed or work_question):
        return None

    # Для первичной диагностики и справочных вопросов строка таблицы служит пояснением.
    confirmed = False
    if related_topic and (observed or work_question):
        confirmed = topic in {BEARING_CONFIRMED, ROTOR_CONFIRMED} if bearing is None else bearing
        if rotor is None:
            rotor = topic in {ROTOR_CONFIRMED, _ROTOR_WITHOUT_BEARING}
        if bearing is False:
            rotor = False
        topic = (ROTOR_CONFIRMED if rotor else BEARING_CONFIRMED) if confirmed else (
            _ROTOR_WITHOUT_BEARING if rotor else GENERATOR_BEARING_TOPIC)
    else:
        topic = GENERATOR_BEARING_TOPIC

    found = _add_bearing_source(search, search.find(_BEARING_QUERY), confirmed)
    if not found:
        found = _add_bearing_source(search, search.exact_page(*GENERATOR_BEARING_SOURCE, "F05"), confirmed)
    missing = "" if found else "В найденных фрагментах нет строки о проверке или замене подшипника генератора."
    prompt = _CHECK_GENERATOR if bearing is False else _BEARING_QUESTION + " Укажите результат проверки. " + _CHECK_GENERATOR
    if confirmed:
        prompt = "Механик подтвердил неисправность подшипника генератора."
        if rotor:
            prompt += " " + GENERATOR_ROTOR_REASON
        elif not rotor_ruled_out:
            prompt += " При проверке обнаружили задевание ротора?"
    if voltage:
        search.passage("Электрооборудование постоянного тока. Номинальное напряжение в системе 12 В",
                       r"(?:Номинальное напряжение в системе|Однопроводное,\s*номинальным напряжением)\s+12\s*В\b\.?",
                       explain=lambda _: "Номинальное напряжение бортовой сети — 12 В.",
                       document_id=GENERATOR_BEARING_SOURCE[0])
        missing = (missing + " В руководстве ЗМЗ-5234.10 не найдены условия измерения напряжения генератора "
                   "под нагрузкой, частота вращения и допустимый диапазон. "
                   "Уточните обозначение установленного генератора, чтобы сверить эти данные по его документации.").strip()
    return topic, missing, prompt, not (confirmed and found and (rotor or rotor_ruled_out))
