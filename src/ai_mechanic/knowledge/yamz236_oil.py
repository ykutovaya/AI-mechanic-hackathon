"""Проверяет фактическое давление масла в диалоге по ЯМЗ-236М2."""

from decimal import Decimal
import re

from .qdrant_chunks import clean


OIL_CHECK_ID = "S-0011"
OIL_MEASUREMENT_TOPIC = "yamz236-oil-observation"
OIL_MEASUREMENT_CONFIRMED_TOPIC = OIL_MEASUREMENT_TOPIC + ":confirmed"
OIL_WORK = "Проверить систему смазки"
_MANUAL = "yamz-236m2-re-2010"
OIL_WORK_SOURCES = frozenset({(_MANUAL, 4), (_MANUAL, 53)})
_LIMIT = Decimal("300")

_VALUE = re.compile(r"(?<![\w.,<>≤≥+−-])(\d+(?:[.,]\d+)?)\s*(кпа|мпа)\b")
_NOMINAL = re.compile(
    r"\b(?:(?:на|при)\s+номинальн\w*\s+(?:оборот\w*|частот\w*\s+вращени\w*)"
    r"|оборот\w*\s+(?:были\s+)?номинальн\w*)\b"
)
_OTHER_SPEED = re.compile(r"\bхолост\w*|\bне\s+(?:на|при)\s+номинальн\w*|\bне номинальн\w*")
_NO_MEASUREMENT = re.compile(r"\bне\s+(?:был[оаи]\s+)?(?:измер\w*|замер\w*)|\b(?:замера|измерения)\s+не\s+было")
_REPORTED = re.compile(r"\b(?:измер\w*|замер\w*|давлени\w*|манометр\w*|показани\w*)\b")
_OUT_OF_SCOPE = re.compile(
    r"\b(?:топлив\w*|шин\w*|воздух\w*|наддув\w*|рампе|уставк\w*|по руководств\w*|"
    r"по инструкци\w*|норматив\w*|сигнализатор\w*|ml)\b"
)
_UNCERTAIN = re.compile(
    r"\b(?:если|допустим|предположим|например|возможно|кажется|должно|не уверен\w*|не зна\w*)\b"
)
_SENTENCE = re.compile(r"\S.*?(?:[.!?;](?=\s|$)|$)", re.S)
_QUESTION_START = re.compile(
    r"^\s*(?:а\s+)?(?:(?:при|до|на|в)\s+)?(?:что\s+(?:делать|значит|означает)|"
    r"как\w*|чем|где|когда|зачем|почему|сколько|"
    r"(?:можно|нужно|надо|стоит|должен|должна|допустимо|измеряли|проверяли)\s+ли)\b"
)
_CLAUSE_BOUNDARY = re.compile(
    r"\s*,\s*(?:а|но)\s+|\s+но\s+"
    r"|\s*(?:,|\bи\b|\bпри этом\b)\s*(?=(?:ламп\w*|сигнализатор\w*|"
    r"(?:(?:контрольн\w*|эталонн\w*)\s+)?манометр\w*|стрелочн\w*\s+указател\w*)\b)"
    r"|\s*(?:,|\bи\b)\s*(?=(?:на|при)\s+(?:номинальн|средн|минимальн|холост|высок)\w*\s+"
    r"[^,;!?]{0,105}\b(?:манометр\w*|указател\w*|давлени\w*|измер\w*|замер\w*)\b)"
)
_LAMP_CLAUSE = re.compile(r"\b(?:ламп\w*|сигнализатор\w*)\b")
OIL_MEASUREMENT_QUESTION = (
    "Измеряли ли фактическое давление масла? Если да, каким прибором, при каких оборотах "
    "и при какой температуре двигателя? Укажите показание с единицами."
)
OIL_MEASUREMENT_REQUEST = (
    "Укажите в одном сообщении прибор, показание давления с единицами, обороты и температуру двигателя при замере."
)
_CHECK = "Убедитесь, что в картере достаточно масла и в системе достаточно охлаждающей жидкости."
_MEASURE = ("Согласуйте контрольный замер с механиком. При сохраняющемся стуке и сигнале "
            "возможность замера определяет механик.")
_METHOD_MISSING = "В найденных разделах не описан порядок контрольного замера давления масла на работающем двигателе."

_WARNING_QUERY = "падение давления масла номинальные обороты предупреждение износ вкладышей"
_WARNING = (
    r"падение давления масла ниже 300\s*кПа\s*\([\d,.]+\s*кгс/см[2²]\)\s*при номинальных оборотах "
    r"свидетельствует о неисправностях в системе смазки или повышенных износах "
    r"вкладышей подшипников коленчатого вала\."
)
_LEVEL_QUERY = "Пониженное давление масла убедиться в наличии масла в картере и охлаждающей жидкости"
_LEVEL = (
    r"Прежде чем искать причину неисправности, убедиться в наличии "
    r"достаточного количества масла в картере и охлаждающей жидкости"
)
_LEAK_QUERY = "Негерметичность соединения маслопроводов проверить соединения прокладки фильтров и трубок"
_LEAK = (
    r"Негерметичность соединения (?:маслопроводов\s*(?:\|\s*)?Проверить соединения, особенно"
    r"|Проверить соединения, особенно маслопроводов) прокладки фильтров, "
    r"отводящих и всасывающей трубок масляного насоса и прокладку фланца фильтра "
    r"центробежной очистки масла\."
)
_LEAK_CHECK = (
    "При подозрении на негерметичность проверьте соединения маслопроводов, особенно "
    "прокладки фильтров, отводящих и всасывающей трубок масляного насоса "
    "и прокладку фланца фильтра центробежной очистки масла."
)


def split_oil_observation_facts(statement):
    """Отделяет сообщение о лампе от замера, сохраняя условия одиночного замера."""
    return _CLAUSE_BOUNDARY.split(statement) if _LAMP_CLAUSE.search(statement) else [statement]


def oil_observation_clauses(question):
    """Отделяет утверждения механика и независимые сообщения о замере и лампе."""
    q = question.casefold().replace("ё", "е")
    for part in _SENTENCE.findall(q):
        if part.endswith("?") or _QUESTION_START.search(part) or _UNCERTAIN.search(part):
            continue
        yield from split_oil_observation_facts(part)


def oil_measurement_text(question):
    """Оставляет только относящиеся к фактическому замеру сообщения."""
    return " ".join(clause for clause in oil_observation_clauses(question)
                    if not (_LAMP_CLAUSE.search(clause) or _OUT_OF_SCOPE.search(clause)))


def measured_oil_pressure(q, limit):
    """Распознаёт одиночное показание и подтверждение номинальных оборотов."""
    if _NO_MEASUREMENT.search(q):
        return "unmeasured", None

    readings = {Decimal(value.replace(",", ".") + ("e3" if unit == "мпа" else ""))
                for value, unit in _VALUE.findall(q)}
    if not readings and not re.search(r"\b(?:измерили|измерено|замерили|замерено)\b", q):
        return None
    if not (_REPORTED.search(q) or _NOMINAL.search(q) or _OTHER_SPEED.search(q)):
        return None
    if len(readings) != 1:
        return "incomplete", None
    pressure = next(iter(readings))
    if _OTHER_SPEED.search(q) or not _NOMINAL.search(q):
        return "regime", pressure
    return ("confirmed" if pressure < limit else "sufficient"), pressure


def _observation(question):
    return measured_oil_pressure(oil_measurement_text(question), _LIMIT)


def oil_observation_question(question):
    """Проверяет, меняет ли сообщение механика сведения о замере."""
    return _observation(question) is not None


def oil_manual_excerpt(search, document, page, query, pattern):
    """Возвращает совпавший текст из конкретной физической страницы."""
    def match(hits):
        for hit in hits:
            if (hit["document_id"], hit["page"]) == (document, page):
                found = re.search(pattern, clean(hit["text"]), re.I)
                if found:
                    return hit, found.group()
        return None

    return match(search.find(query)) or match(search.exact_page(document, page, "F02"))


def yamz236_low_pressure_evidence(search, *, confirmed=False, legacy=False):
    """Показывает предупреждение, первый шаг и условие проверки соединений."""
    warning = oil_manual_excerpt(search, _MANUAL, 4, _WARNING_QUERY, _WARNING)
    level = oil_manual_excerpt(search, _MANUAL, 53, _LEVEL_QUERY, _LEVEL)
    work = OIL_WORK if confirmed and warning and level else None
    if warning:
        fact = warning[1]
        search.add(*warning, text=fact[:1].upper() + fact[1:], kind="risk", work=work)
    if level:
        level_work = ("Проверить уровень масла в картере и уровень охлаждающей жидкости при пониженном давлении масла."
                      if legacy else work)
        text = (OIL_WORK + ". Сначала убедиться в наличии достаточного количества масла в картере "
                "и охлаждающей жидкости.") if confirmed and work else None
        search.add(*level, text=text, work=level_work)
    leak = oil_manual_excerpt(search, _MANUAL, 53, _LEAK_QUERY, _LEAK)
    if leak:
        leak_work = ("При пониженном давлении масла и подозрении на негерметичность проверить соединения маслопроводов."
                     if legacy else None)
        search.add(*leak, text=_LEAK_CHECK, work=leak_work)
    return bool(leak) if legacy else bool(warning and level)


def select_oil_measurement(search, question, topic, *, general, asks_measurement):
    """Привязывает работу к замеру механика на номинальных оборотах."""
    related = topic.partition(":")[0] == OIL_MEASUREMENT_TOPIC
    observation = _observation(question) if related else None
    work_question = question.strip(" .!?").casefold() == OIL_WORK.casefold()
    verified_work = work_question and topic == OIL_MEASUREMENT_CONFIRMED_TOPIC
    if not (observation or general or asks_measurement or verified_work):
        return None

    state, pressure = observation or ("confirmed" if verified_work else "initial", None)
    confirmed = state == "confirmed"
    found = yamz236_low_pressure_evidence(search, confirmed=confirmed)
    missing = _METHOD_MISSING if asks_measurement else ""
    if not found:
        missing += (" " if missing else "") + "Для заявки нужны обе выдержки по давлению масла со стр. 4 и 53."

    if state in {"confirmed", "sufficient"} and pressure is not None:
        value = format(pressure, "f").replace(".", ",")
        prompt = f"По замеру механика на номинальных оборотах давление масла составило {value} кПа."
        if state == "sufficient":
            prompt += " Для этого замера условие «ниже 300 кПа» не выполнено. Уточните источник стука."
    elif state == "regime":
        prompt = "Порог 300 кПа относится к номинальным оборотам. " + OIL_MEASUREMENT_REQUEST
    elif state == "incomplete":
        prompt = OIL_MEASUREMENT_REQUEST
    elif state == "unmeasured":
        prompt = f"{_CHECK} {_MEASURE}"
    elif not verified_work:
        prompt = f"{OIL_MEASUREMENT_QUESTION} {_CHECK} {_MEASURE}"
    else:
        prompt = ""

    if confirmed and found:
        return OIL_MEASUREMENT_CONFIRMED_TOPIC, missing, prompt, False
    return OIL_MEASUREMENT_TOPIC, missing, prompt, True
