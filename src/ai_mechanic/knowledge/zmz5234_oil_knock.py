"""Уточнение аварийной лампы и стука для ЗМЗ-5234.10."""

import re

from .qdrant_chunks import clean


ZMZ5234_CHECK_ID = "S-0139"
ZMZ5234_OIL_KNOCK_TOPIC = "zmz5234-oil-knock"
ZMZ5234_REPAIR_CONFIRMED_TOPIC = ZMZ5234_OIL_KNOCK_TOPIC + ":repair"
ZMZ5234_REPAIR_WORK = "Выполнить ремонт двигателя"
_MANUAL = "zmz-5234-10-rk-2025"
ZMZ5234_REPAIR_SOURCE = (_MANUAL, 56)

_LAMP_QUESTION = "На каких оборотах загорелся сигнализатор — на минимальном холостом ходу, на средних или на высоких?"
_HIGHER_SPEED_QUESTION = "Горел ли сигнализатор на средних или высоких оборотах?"
_KNOCK_TIMING = "Глухой стук был слышен одновременно с сигналом или отдельно?"
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
    r"(?:\s+(?:и|или)\s+(?:средн\w*|высок\w*|больш\w*))?\s+(?:оборот\w*|частот\w+\s+вращени\w*)\b"
)
_IDLE_ONLY = re.compile(r"\bтолько\s+на\s+(?:минимальн\w*\s+)?холост\w*\b")
_SHORT_SPEED = re.compile(r"на\s+(?P<speed>средних|высоких|больших|номинальных)(?:\s+оборотах)?[.!]?", re.I)
_AMBIGUOUS_SPEED = re.compile(r"\bна оборотах выше холост\w*")
_IDLE_SPEED = re.compile(r"на\s+(?:минимальн\w*\s+)?(?:холостом(?:\s+ходу)?|холостых(?:\s+оборотах)?)")
_LAMP_FLAGS = ("danger", "normal", "idle", "lamp-unknown")
_TIMING_PATTERNS = {
    "separate": re.compile(r"\b(?:отдельно(?:\s+от\s+сигнала)?|не одновременно|(?:до|после)\s+сигнала|раньше|позже)\b"),
    "together": re.compile(r"\b(?:одновременно|вместе)(?:\s+с\s+(?:сигналом|лампой))?\b"),
}
_TIMING_CONFIRMATIONS = {
    "together": "Стук совпал по времени с сигналом.",
    "separate": "Стук был слышен отдельно от сигнала.",
    "absent": "По уточнению механика стука не было.",
}
_KNOCK_DETAILS = {
    "where": re.compile(r"\b(?:спереди|сзади|сверху|снизу|справа|слева"
                        r"|(?:в|из)\s+(?:передней|задней|нижней|верхней)\s+части\s+(?:двигателя|блока)"
                        r"|(?:у|около)\s+(?:поддона|головки(?:\s+блока)?|картера))\b"),
    "rpm": re.compile(rf"\b(?:на\s+(?:средних|высоких|больших|номинальных|малых)\s+оборотах"
                      rf"|{_IDLE_SPEED.pattern}|\d{{3,4}}\s*(?:об\s*/\s*мин|оборотов\s+в\s+минуту))\b"),
    "load": re.compile(r"\b(?:под\s+нагрузкой|без\s+нагрузки|при\s+разгоне|в\s+движении|на\s+стоянке)\b"),
}
_KNOCK_DETAIL_QUESTIONS = {
    "where": "Откуда идёт стук?",
    "rpm": "При каких оборотах он слышен?",
    "load": "При какой нагрузке он слышен?",
}
_PAST_KNOCK_QUESTIONS = {
    "where": "Откуда тогда шёл стук?",
    "rpm": "На каких оборотах его слышали?",
    "load": "При какой нагрузке его слышали?",
}
_UNKNOWN_SPEED = re.compile(
    r"(?:не (?:знаю|помню|уверен\w*).{0,80}(?:оборот\w*|режим\w*)|(?:оборот\w*|режим\w*).{0,60}(?:не (?:знаю|помню)|неизвест\w*))"
)
_KNOCK = re.compile(
    r"\b(?:стук\w*.{0,60}(?:слыш\w*|сохраня\w*|продолжа\w*|был\w*|есть|остал\w*|возник\w*|идёт|идет|донос\w*|исход\w*)"
    r"|(?:слыш\w*|зафиксир\w*|наблюдал\w*)[^.!?]{0,45}\bстук\w*)\b"
)
_NO_KNOCK = re.compile(
    r"\b(?:стук\w*\s+(?:нет|не был\w*|исчез\w*|пропал\w*|не слыш\w*|не подтверд\w*)"
    r"|(?:нет|не было|не слыш\w*)\s+стук\w*)\b"
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
        if unknown_speed and not re.search(r"\bстук\w*", sentence):
            observed["lamp"] = "lamp-unknown"
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
                elif lamp and _IDLE_SPEED.search(clause) and _LIGHT_ON.search(clause) and not _LIGHT_OFF.search(clause):
                    observed["lamp"] = "idle"
                elif _SHORT_SPEED.fullmatch(clause.strip()) or _AMBIGUOUS_SPEED.search(clause):
                    observed["ambiguous_speed"] = True
            if _NO_KNOCK.search(clause):
                observed.update(knock=False, timing="absent")
            elif _KNOCK.search(clause):
                observed["knock"] = True
                for key, pattern in _TIMING_PATTERNS.items():
                    if pattern.search(sentence):
                        observed["timing"] = key
                        break
                for key, pattern in _KNOCK_DETAILS.items():
                    if detail := pattern.search(clause):
                        observed[key] = detail.group()
            if _SERIOUS_WEAR.search(clause) and _BEARING.search(clause):
                if _DENY_WEAR.search(clause):
                    observed["repair"] = False
                elif _CONFIRM.search(clause) and not _HEARD_WEAR.search(sentence):
                    observed["repair"] = True
            elif _SERIOUS_WEAR.search(clause) and _DENY_WEAR.search(clause) and not re.search(r"подшипник|вкладыш", clause):
                observed["repair"] = False
    return observed


def _dialog_facts(topic):
    family, _, context = topic.partition(":")
    flags = set(context.split("+")) if family == ZMZ5234_OIL_KNOCK_TOPIC else set()
    lamp = next((name for name in _LAMP_FLAGS if name in flags), "")
    timing = next((name for name in _TIMING_CONFIRMATIONS if name in flags), "")
    details = dict(flag.split("=", 1) for flag in flags if flag.partition("=")[0] in _KNOCK_DETAILS and "=" in flag)
    return flags, lamp, timing, details


def _ask_about_knock(lamp, knock, timing):
    return knock and (lamp == "normal" or lamp == "danger" and timing == "separate")


def zmz5234_contextual_answer(question, topic):
    """Привязывает короткую реплику к вопросам текущего диалога о лампе и стуке."""
    if topic.partition(":")[0] != ZMZ5234_OIL_KNOCK_TOPIC or topic == ZMZ5234_REPAIR_CONFIRMED_TOPIC:
        return question
    flags, lamp, timing, _ = _dialog_facts(topic)
    need_lamp = lamp in ("", "idle")
    knock_details = _ask_about_knock(lamp, "knock" in flags, timing)
    need_timing = not timing and not knock_details

    expanded = []
    interpreted = False
    for part in re.split(r"\s*[,;]\s*|(?<=[.!?])\s+", question.strip()):
        if not part.strip():
            continue
        short = part.casefold().strip().rstrip(".!")
        speed = _SHORT_SPEED.fullmatch(short)
        if need_lamp and speed:
            expanded.append(f"Сигнализатор аварийного давления масла загорелся на {speed['speed']} оборотах")
        elif need_lamp and _IDLE_SPEED.fullmatch(short):
            expanded.append(f"Сигнализатор аварийного давления масла загорелся {short}")
        elif lamp == "idle" and short in {"да", "нет"}:
            state = "загорелся" if short == "да" else "не загорался"
            expanded.append(f"Сигнализатор аварийного давления масла {state} на средних или высоких оборотах")
        elif need_timing and any(pattern.fullmatch(short) for pattern in _TIMING_PATTERNS.values()):
            expanded.append(f"Глухой стук был слышен {short}")
        elif knock_details and (speed or any(pattern.fullmatch(short) for pattern in _KNOCK_DETAILS.values())):
            wording = f"на {speed['speed']} оборотах" if speed else short
            expanded.append(f"Глухой стук слышен {wording}")
        elif _observe(part):
            expanded.append(part.rstrip(".!"))
            continue
        else:
            return question
        interpreted = True
    return ". ".join(expanded) if interpreted else question



_MODEL_KNOCK_WORDING = {
    "timing": {"separate": "отдельно", "together": "одновременно"},
    "where": {"top": "сверху", "bottom": "снизу", "front": "спереди", "rear": "сзади",
              "left": "слева", "right": "справа"},
    "rpm": {"idle": "на холостых оборотах", "medium": "на средних оборотах",
            "high": "на высоких оборотах"},
    "load": {"loaded": "под нагрузкой", "unloaded": "без нагрузки"},
}


def zmz5234_model_answer(raw_question, local_question, topic, findings):
    """Дополняет наблюдения о стуке фактами модели с цитатами из текущей реплики."""
    if not findings or topic.partition(":")[0] != ZMZ5234_OIL_KNOCK_TOPIC or topic == ZMZ5234_REPAIR_CONFIRMED_TOPIC:
        return local_question
    observed = _observe(local_question)
    if observed.get("knock") is False or observed.get("repair") is not None:
        return local_question
    flags, lamp, timing, _ = _dialog_facts(topic)
    asking_knock_details = _ask_about_knock(lamp, "knock" in flags, timing)
    asking_timing = not timing and not asking_knock_details
    clauses = []
    for sentence in re.split(r"(?<=[.!?;])\s+", raw_question.strip()):
        normalized = sentence.casefold().replace("ё", "е")
        if (not normalized or normalized.endswith("?") or _QUESTION_WORDS.search(normalized)
                or _UNCONFIRMED.search(normalized)):
            continue
        clauses.extend(re.split(r",\s*(?=[^\d])|\s+(?:а|но)\s+", sentence))

    additions = {}
    for finding in findings:
        field, value, quote = finding["field"], finding["value"], finding["quote"]
        wording = _MODEL_KNOCK_WORDING.get(field, {}).get(value)
        if not wording or field in observed or field in additions or not quote.strip():
            continue
        quoted_clauses = [clause.casefold().replace("ё", "е") for clause in clauses if quote in clause]
        if not quoted_clauses or any(re.search(rf"\b{_LAMP}\b", clause) or _OTHER_LAMP.search(clause)
                                     for clause in quoted_clauses):
            continue
        mentions_knock = all(re.search(r"\b(?:стук|шум|звук)\w*\b", clause) for clause in quoted_clauses)
        quoted = quote.casefold().strip(" .!?").replace("ё", "е")
        if quoted in {"да", "нет"}:
            continue
        if field == "timing":
            if not mentions_knock and not asking_timing:
                continue
            if (_SHORT_SPEED.fullmatch(quoted) or _KNOCK_DETAILS["rpm"].fullmatch(quoted)
                    or re.fullmatch(r"\d{3,4}", quoted)):
                continue
        elif not mentions_knock and not asking_knock_details:
            continue
        additions[field] = wording

    if not additions:
        return local_question
    separator = " " if local_question.endswith((".", "!", "?", ";")) else ". "
    return local_question + separator + " ".join(f"Глухой стук был слышен {value}." for value in additions.values())

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
    suffix, lamp, timing, details = _dialog_facts(topic)
    lamp = observation.get("lamp", lamp)
    if lamp == "lamp-unknown" and "danger" in suffix:
        lamp = "danger"
    knock = observation.get("knock", "knock" in suffix)
    if observation.get("knock") and timing == "absent":
        timing = ""
    timing = observation.get("timing", timing)
    if observation.get("knock") is False:
        details.clear()
    details.update({key: observation[key] for key in _KNOCK_DETAILS if key in observation})
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
    lamp_question = _LAMP_QUESTION if not lamp else _HIGHER_SPEED_QUESTION if lamp == "idle" else ""
    questions = [part for part in (lamp_question, _KNOCK_TIMING if not timing else "") if part]
    if lamp in ("danger", "normal", "idle") or repair:
        warning_level(search)
    else:
        low_pressure(search)

    report = []
    if lamp == "danger" and any((row["document_id"], row["page"]) == (_MANUAL, 23) for row in search.evidence):
        report.append(_STOP)
    if _ask_about_knock(lamp, knock, timing):
        prompts = _PAST_KNOCK_QUESTIONS if lamp == "danger" else _KNOCK_DETAIL_QUESTIONS
        questions = [prompt for key, prompt in prompts.items() if key not in details]
        if lamp == "normal" and not details:
            questions = [_KNOCK_QUESTION]
        elif lamp == "normal" and set(details) == {"rpm"}:
            questions = ["Уточните, откуда идёт стук и при какой нагрузке он слышен."]
        if details:
            hearing = "был слышен" if lamp == "danger" else "слышен"
            report.append(f"По уточнению механика стук {hearing} "
                          + ", ".join(details[key] for key in _KNOCK_DETAILS if key in details) + ".")
        if lamp == "danger" and questions:
            report.append("Уточните по уже сделанным наблюдениям.")
        if not _add_knock_causes(search):
            missing = "Не все причины из раздела «Стуки в двигателе» подтверждены на физических стр. 56–57 PDF."
    elif lamp == "normal":
        report.append("По уточнению механика сигнализатор на средних и высоких оборотах не загорался.")
    if timing and (timing == observation.get("timing") or not questions):
        index = 1 if report and report[0] == _STOP else 0
        report.insert(index, _TIMING_CONFIRMATIONS[timing])
    clarification = " ".join([*report, *questions])

    if repair:
        if _add_bearing_repair(search):
            return ZMZ5234_REPAIR_CONFIRMED_TOPIC, missing, clarification, False
        missing = "В найденных фрагментах не подтверждено основание ремонта на физической стр. 56 PDF."

    flags = [flag for flag in (lamp, "knock" if knock else "", timing) if flag]
    flags.extend(f"{key}={details[key]}" for key in _KNOCK_DETAILS if key in details)
    topic = ZMZ5234_OIL_KNOCK_TOPIC + (":" + "+".join(flags) if flags else "")
    return topic, missing, clarification, bool(questions or missing)
