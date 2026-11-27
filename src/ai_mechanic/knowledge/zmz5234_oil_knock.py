"""Уточнение аварийной лампы и стука для ЗМЗ-5234.10."""

import re

from .qdrant_chunks import clean


ZMZ5234_CHECK_ID = "S-0139"
ZMZ5234_OIL_KNOCK_TOPIC = "zmz5234-oil-knock"
ZMZ5234_REPAIR_CONFIRMED_TOPIC = ZMZ5234_OIL_KNOCK_TOPIC + ":repair"
ZMZ5234_REPAIR_WORK = "Выполнить ремонт двигателя"
_MANUAL = "zmz-5234-10-rk-2025"
ZMZ5234_REPAIR_SOURCE = (_MANUAL, 56)

_QUESTION = (
    "На каких оборотах загорелся сигнализатор — на минимальном холостом ходу, на средних или на высоких? "
    "Глухой стук был слышен одновременно с сигналом или отдельно?"
)
_STOP = "Немедленно заглушите двигатель. Повторный пуск — после устранения неисправности."
_KNOCK_QUESTION = "Уточните, откуда идёт стук и при каких оборотах и нагрузке он слышен."
_OIL_LEVEL_WORK = "Проверить уровень масла по руководству."
_KNOCK_HEADING = "стуки в двигателе"
_KNOCK_TABLE_QUERY = "Стуки в двигателе. Сильный износ вкладышей. Задир стержня клапана. Ослабло крепление шкива"
_REPAIR_CAUSE = re.compile(
    r"сильный износ вкладышей коренных или шатунных подшипников коленчатого вала", re.I
)
_KNOCK_CONTINUATION = re.compile(r"задир стержня клапана в направляющей втулке", re.I)

_QUESTION_WORDS = re.compile(
    r"^(?:а\s+)?(?:что|как|почему|когда|где|како[ей]|сколько|(?:при|на)\s+каких|(?:можно|нужно|горит|горела|загорелся)\s+ли)\b"
)
_UNCONFIRMED = re.compile(
    r"\b(?:если|допустим|предположим|предположительно|возможно|кажется|по (?:данным |сигналу )?ml|по руководству)\b"
)
_LAMP = r"(?:ламп\w*|сигнализатор\w*)"
_OTHER_LAMP = re.compile(
    r"\b(?:диагностическ\w+\s+ламп\w*|(?:ламп\w*|сигнализатор\w*)\s+(?:перегрев|заряд|аккумулятор)|check engine)", re.I
)
_LIGHT_ON = re.compile(r"\b(?:горит|горел\w*|загора\w*|загорел\w*|не (?:гаснет|погас\w*))\b")
_LIGHT_OFF = re.compile(r"\b(?:не (?:горит|горел\w*|загора\w*|загорел\w*)|погас\w*|потух\w*)\b")
_UNSAFE_SPEED = re.compile(
    r"\b(?:средн\w*|высок\w*|больш\w*|номинальн\w*)"
    r"(?:\s+и\s+(?:средн\w*|высок\w*|больш\w*))?\s+(?:оборот\w*|частот\w+\s+вращени\w*)\b"
)
_IDLE_ONLY = re.compile(r"\bтолько\s+на\s+(?:минимальн\w*\s+)?холост\w*\b")
_AMBIGUOUS_SPEED = re.compile(
    r"^\s*на (?:средних|высоких|больших|номинальных)(?: оборотах)?[.!]?\s*$"
    r"|\bна оборотах выше холост\w*"
)
_UNKNOWN_SPEED = re.compile(
    r"(?:не (?:знаю|помню|уверен\w*).{0,80}(?:оборот\w*|режим\w*)|(?:оборот\w*|режим\w*).{0,60}(?:не (?:знаю|помню)|неизвест\w*))"
)
_KNOCK = re.compile(
    r"\b(?:стук\w*.{0,60}(?:слыш\w*|сохраня\w*|продолжа\w*|был\w*|есть|остал\w*|возник\w*)"
    r"|(?:слыш\w*|зафиксир\w*|наблюдал\w*)[^.!?]{0,45}\bстук\w*)\b"
)
_NO_KNOCK = re.compile(
    r"\b(?:стук\w*\s+(?:нет|исчез\w*|пропал\w*|не слыш\w*|не подтверд\w*)|(?:нет|не слыш\w*)\s+стук\w*)\b"
)
_SERIOUS_WEAR = re.compile(r"\b(?:сильн\w*|чрезмерн\w*)\s+износ\w*")
_BEARING = re.compile(
    r"\bвкладыш\w*[^.!?]{0,75}\b(?:коренн\w*|шатунн\w*)[^.!?]{0,50}подшипник\w*"
    r"[^.!?]{0,45}(?:коленчат\w+\s+вал\w*|коленвал\w*)"
)
_HEARD_WEAR = re.compile(r"\b(?:на слух|по стуку|по шуму)\b")
_CONFIRM = re.compile(
    r"\b(?:подтвердил[аи]?|подтвердил(?:ся|ась|ось)|подтвержден[аоы]?|"
    r"(?:обнаружил|выявил|установил)[аи]?|(?:обнаружен|выявлен|установлен)[аоы]?)\b"
)
_DENY_WEAR = re.compile(r"\b(?:не (?:подтвер\w*|обнаруж\w*|выяв\w*|установл\w*)|(?:износ\w*|его)\s+нет)\b")


def _observe(question):
    observed = {}
    for sentence in re.split(r"(?<=[.!?;])\s+", question.casefold().replace("ё", "е").strip()):
        sentence = re.sub(r"^(?:уточняю|уточнение|поправка)\s*:\s*", "", sentence)
        if not sentence or sentence.endswith("?") or _QUESTION_WORDS.search(sentence) or _UNCONFIRMED.search(sentence):
            continue
        unknown_speed = bool(_UNKNOWN_SPEED.search(sentence))
        if unknown_speed:
            observed["lamp"] = ""
        clauses = re.split(
            r",\s*(?=[^\d])|\s+но\s+|\s+а\s+(?=стук|ламп|сигнализатор)"
            r"|\s+(?:и затем|а потом|после чего)\s+|\s+и\s+(?=погас|потух)",
            sentence,
        )
        for clause in clauses:
            if not unknown_speed and not _OTHER_LAMP.search(clause):
                lamp = re.search(rf"\b{_LAMP}\b", clause)
                if lamp and _IDLE_ONLY.search(clause) and _LIGHT_ON.search(clause) and not _LIGHT_OFF.search(clause):
                    observed["lamp"] = "normal"
                elif lamp and _UNSAFE_SPEED.search(clause):
                    if _LIGHT_OFF.search(clause) and not re.search(r"\bне (?:гаснет|погас\w*)\b", clause):
                        observed["lamp"] = "normal"
                    elif _LIGHT_ON.search(clause):
                        observed["lamp"] = "danger"
                elif _AMBIGUOUS_SPEED.search(clause):
                    observed["ambiguous_speed"] = True
            if _NO_KNOCK.search(clause):
                observed["knock"] = False
            elif _KNOCK.search(clause):
                observed["knock"] = True
            if _SERIOUS_WEAR.search(clause) and _BEARING.search(clause):
                if _DENY_WEAR.search(clause):
                    observed["repair"] = False
                elif _CONFIRM.search(clause) and not _HEARD_WEAR.search(sentence):
                    observed["repair"] = True
            elif _SERIOUS_WEAR.search(clause) and _DENY_WEAR.search(clause) and not re.search(r"подшипник|вкладыш", clause):
                observed["repair"] = False
    return observed


def zmz5234_oil_observation_question(question):
    """Распознаёт сообщения механика о сигнализаторе, стуке или износе вкладышей."""
    return bool(_observe(question))


def _knock_rows(hits, page):
    for hit in hits:
        if (hit["document_id"], hit["page"]) != (_MANUAL, page):
            continue
        for table in hit.get("tables", ()):
            inside = False
            rows = []
            for row in table.get("rows", ()):
                if len(row) != 2:
                    inside = False
                    continue
                cause, action = (clean(cell or "") for cell in row)
                if not action:
                    inside = cause.casefold().rstrip(":") == _KNOCK_HEADING
                elif page == 57 and _KNOCK_CONTINUATION.fullmatch(cause):
                    inside = True
                if inside and cause and action:
                    rows.append((cause, action))
            if rows:
                return hit, rows
    return None


def _knock_table(search, page):
    return (_knock_rows(search.find(_KNOCK_TABLE_QUERY), page)
            or _knock_rows(search.exact_page(_MANUAL, page, "F02"), page))


def _add_knock_causes(search):
    found = True
    for page in (56, 57):
        table = _knock_table(search, page)
        if not table:
            found = False
            continue
        hit, rows = table
        causes = ". ".join(cause.rstrip(".; ") for cause, _ in rows) + "."
        search.add(hit, causes, kind="cause")
    return found


def _add_bearing_repair(search, *, confirmed=True):
    table = _knock_table(search, 56)
    if not table:
        return False
    hit, rows = table
    for cause, action in rows:
        if _REPAIR_CAUSE.fullmatch(cause) and action.casefold().rstrip(".") == "произвести ремонт двигателя":
            statement = (f"{cause} подтверждён. {ZMZ5234_REPAIR_WORK}." if confirmed else
                         f"Руководство предусматривает ремонт двигателя при подтверждённом сильном износе "
                         f"вкладышей коренных или шатунных подшипников коленчатого вала.")
            search.add(hit, f"{cause} | {action}", text=statement, work=ZMZ5234_REPAIR_WORK if confirmed else None)
            return True
    return False


def select_zmz5234_oil_knock(search, question, topic, *, general, warning_level, low_pressure):
    """Учитывает только уточнения текущей диагностики и выбирает применимые строки PDF."""
    related = topic.partition(":")[0] == ZMZ5234_OIL_KNOCK_TOPIC
    observation = _observe(question) if related else {}
    suffix = set(topic.partition(":")[2].split("+")) if related else set()
    lamp = "danger" if "danger" in suffix else "normal" if "normal" in suffix else ""
    lamp = observation.get("lamp", lamp)
    knock = observation.get("knock", "knock" in suffix)
    repair = observation.get("repair", topic == ZMZ5234_REPAIR_CONFIRMED_TOPIC)
    named = question.strip(" .!?").casefold()
    named_repair = named == ZMZ5234_REPAIR_WORK.casefold() and repair and related
    named_level = named == _OIL_LEVEL_WORK.rstrip(".").casefold() and related
    if not observation and related and _SERIOUS_WEAR.search(question) and _BEARING.search(question):
        found = _add_bearing_repair(search, confirmed=False)
        missing = "" if found else "В найденных фрагментах не подтверждено основание ремонта на физической стр. 56 PDF."
        return topic, missing, "", not found
    if not (observation or named_repair or named_level or general):
        return None
    if not (observation or named_repair or named_level) and re.search(r"\b(?:какие причины|почему|из-за чего)\b", question):
        return None
    if named_repair:
        found = _add_bearing_repair(search)
        missing = "" if found else "В найденных фрагментах не подтверждено основание ремонта на физической стр. 56 PDF."
        return ZMZ5234_REPAIR_CONFIRMED_TOPIC, missing, "", not found
    if named_level:
        warning_level(search)
        return topic, "", "", False

    missing = ""
    clarification = _QUESTION
    if lamp == "danger" or lamp == "normal" and knock or repair:
        warning_level(search)
    else:
        low_pressure(search)

    if lamp == "danger" and any((row["document_id"], row["page"]) == (_MANUAL, 23) for row in search.evidence):
        clarification = _STOP
    elif lamp == "normal" and knock:
        clarification = _KNOCK_QUESTION
        if not _add_knock_causes(search):
            missing = "Не все причины из раздела «Стуки в двигателе» подтверждены на физических стр. 56–57 PDF."

    if repair:
        if _add_bearing_repair(search):
            return ZMZ5234_REPAIR_CONFIRMED_TOPIC, missing, clarification, False
        missing = "В найденных фрагментах не подтверждено основание ремонта на физической стр. 56 PDF."

    flags = [flag for flag in (lamp, "knock" if knock else "") if flag]
    topic = ZMZ5234_OIL_KNOCK_TOPIC + (":" + "+".join(flags) if flags else "")
    return topic, missing, clarification, True
