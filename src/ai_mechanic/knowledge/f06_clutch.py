"""Связывает шум сцепления ЗМЗ-5234.10 с режимом, указанным в таблице руководства."""

import re


_WORK = "Проверить детали демпферного устройства ведомого диска на поломку и износ."
_HEADING = re.compile(r"Вибрация,\s*шумы и металлическое дребезжание\s+в трансмиссии при движении", re.I)
_CAUSE = "поломка или износ деталей демпферного устройства ведомого диска"
_DRIVING = re.compile(r"\b(?:при движении|в движении|на ходу|во время движения|(?:при|во время) езды)\b")
_SOUND = re.compile(r"дребез|шум|стук")
_PEDAL = re.compile(r"педал|выжима\w* сцеплен|выжат\w* сцеплен")
_UNCERTAIN = re.compile(r"не провер|не знаю|не уверен|не помню|не могу сказать|возможно|может быть|\bесли\b")
_OTHER_UNIT = re.compile(r"масл\w* насос|насос\w* (?:гур|масл)|генератор|стартер|карбюратор|форсунк|ремн[яеюь]|турбокомпрессор")
_TOPIC = re.compile(r"clutch-noise(?::(unknown|gone|same|changed):(unknown|moving|stationary))?$")


def clutch_observation_question(question):
    """Распознаёт явное возвращение механика к шуму сцепления после вопроса на другую тему."""
    q = question.casefold()
    return bool(not _OTHER_UNIT.search(q) and (
        _PEDAL.search(q) or _DRIVING.search(q) or _SOUND.search(q) or "сцеплен" in q
        or q.rstrip(" .!?") == _WORK.casefold().rstrip(".")))


def _pedal_observation(text):
    if not _PEDAL.search(text) or _UNCERTAIN.search(text):
        return None
    if re.search(r"не (?:пропада|исчез|меня)|без изменени|неизмен|остает|остаёт|сохраня", text):
        return "same"
    if re.search(r"исчез|пропада|пропал|прекраща|затиха|уходит|нет шум|не слыш", text):
        return "gone"
    if re.search(r"меня|усилива|становится (?:тише|громче)|ослабева", text):
        return "changed"
    return None


def _motion_observation(text):
    if _UNCERTAIN.search(text) or re.search(r"не (?:ездил|выезжал)", text):
        return "unknown"
    for clause in re.split(r"[,;]|\s+(?:но|а|зато)\s+", text):
        if re.search(r"только (?:на )?холост\w*", clause):
            return "stationary"
        if not _DRIVING.search(clause):
            continue
        if re.search(r"только (?:свист|гул|скрип)|не (?:слыш|проявля|возника|заметил|дребезж|шумит|было|наблюда)"
                     r"|\bнет\b|\bтихо\b|отсутств", clause):
            return "stationary"
        if re.search(r"свист|гудит|гул|скрип|писк|вой|другой звук", clause):
            return "unknown"
        if not _SOUND.search(clause):
            # Короткое «на ходу тоже слышно» относится к заданному вопросу, «есть запах» — к другому признаку.
            remainder = _DRIVING.sub(" ", clause).replace("под нагрузкой", " ")
            if not re.fullmatch(r"(?:\s*(?:да|и|тоже|также|там|его|это|слышно|слышен|слышу|есть|"
                                r"не исчезает|не пропадает|сохраняется)\b)+\s*", remainder):
                return "unknown"
        if re.search(r"не (?:исчез|пропа|прекращ)", clause):
            return "moving"
        if re.search(r"исчез|пропа|прекращ", clause):
            return "stationary"
        if re.search(r"слыш|сохраня|дребезжит|шумит|остается|остаётся|\bесть\b|\bтоже\b", clause):
            return "moving"
        return "unknown"
    return None


def _table_evidence(search, *, moving):
    heading_hit = None
    for hit in [*search.fault_hits, *search.primary]:
        if not (heading := _HEADING.search(hit["text"])):
            continue
        if heading_hit is None:
            heading_hit = hit, heading.group()
        if not moving:
            break
        for table in hit.get("tables", ()):
            in_group = False
            for cause, remedy, *rest in (row for row in table.get("rows", ()) if len(row) >= 2):
                if not remedy:
                    in_group = bool(_HEADING.fullmatch((cause or "").strip()))
                elif in_group and " ".join((cause or "").split()).casefold() == _CAUSE:
                    search.add(hit, heading.group() + ". " + cause,
                               text="При таком дребезжании в трансмиссии при движении возможны поломка или "
                                    "износ деталей демпферного устройства ведомого диска. " + _WORK, work=_WORK)
                    return True
    if heading_hit:
        hit, heading = heading_hit
        search.add(hit, heading, text="В таблице описано металлическое дребезжание в трансмиссии при движении.")
    return False


def clarify_idle_clutch_noise(search, question, symptoms, *, topic="", diagnostic=True):
    """Учитывает только сообщённые признаки и уточняет оставшиеся условия применения таблицы."""
    if search.engine != "ЗМЗ-5234.10" or _OTHER_UNIT.search(question):
        return None
    q = question.casefold().strip()
    symptoms = symptoms.casefold()
    state = _TOPIC.fullmatch(topic)
    pedal, motion = (tuple(value or "unknown" for value in state.groups()) if state else ("unknown", "unknown"))
    clutch_check = re.fullmatch(r"(?:как\s+)?проверить сцепление(?: при (?:признаках|симптомах) "
                                r"(?:этой )?неисправности)?[?!. ]*", q)
    same_work = q.rstrip(".") == _WORK.casefold().rstrip(".")
    observed_motion = diagnostic and not state and _DRIVING.search(symptoms) and _SOUND.search(symptoms)
    fresh = bool(_SOUND.search(symptoms) and "холост" in symptoms and (diagnostic or clutch_check))
    if observed_motion and "холост" not in symptoms:
        # ML уже указал режим в самом описании, но дефект диска всё ещё требует проверки.
        motion = _motion_observation(symptoms) or "unknown"
        if motion == "unknown" and re.fullmatch(r"(?:вибрация и )?металлическое дребезжание "
                                               r"в трансмиссии при движении[. ]*", symptoms):
            motion = "moving"
    mentions_mode = _SOUND.search(q) and ("холост" in q or _DRIVING.search(q))
    short = q.strip(".! ")
    relevant = state and (clutch_observation_question(q) or _UNCERTAIN.search(q)
                          or short in {"да", "нет"} or diagnostic or clutch_check)
    if not (fresh or observed_motion or relevant or mentions_mode or same_work):
        return None

    if same_work and motion != "moving":
        return "clutch-noise", "Для этой проверки нужно подтвердить дребезжание в трансмиссии при движении.", "", False
    # Вопрос о возможном шуме и предположение не подтверждают наблюдения. Принимаем утверждения по одному.
    if state or mentions_mode:
        observations = re.sub(r",\s*(?:а|но)\s+", ". ", q)
        for statement in re.findall(r"[^.!?;]+[?]?", observations):
            if _UNCERTAIN.search(statement) and (_DRIVING.search(statement) or "не ездил" in statement):
                motion = "unknown"
                continue
            if statement.endswith("?") or re.search(r"\b(?:ли|если)\b", statement):
                continue
            pedal = _pedal_observation(statement) or pedal
            new_motion = _motion_observation(statement)
            if new_motion is not None:
                motion = new_motion
        # Ответ «да» относится к движению только когда про педаль уже ответили и остался один вопрос.
        if pedal != "unknown" and motion == "unknown" and short in {"да", "нет"}:
            motion = "moving" if short == "да" else "stationary"

    topic = "clutch-noise" if pedal == motion == "unknown" else f"clutch-noise:{pedal}:{motion}"
    grounded = _table_evidence(search, moving=motion == "moving")
    if motion == "moving":
        missing = "" if grounded else "В найденных строках нет основания для проверки демпферного устройства ведомого диска."
        return topic, missing, "", False
    if motion == "stationary" and pedal != "unknown":
        return (topic, "На ходу этот дребезг не подтверждён. В найденных разделах руководства "
                "причина шума только на холостом ходу не установлена.", "", False)
    if motion == "stationary":
        prompt = "На ходу этого дребезга нет. Меняется ли шум на холостом ходу при нажатии педали сцепления?"
    elif pedal != "unknown":
        prompt = "Слышен ли тот же металлический дребезг при движении под нагрузкой?"
    else:
        prompt = ("По шуму на холостом ходу причину установить пока нельзя. Уточните, меняется ли шум "
                  "при нажатии педали сцепления и слышен ли он при движении под нагрузкой.")
    return topic, "", prompt, True


def select_clutch_mounting(search, question, symptoms, diagnostic):
    """Выбирает проверки, которые руководство предписывает до дефектации деталей сцепления."""
    if search.engine not in {"ЗМЗ-40904.10", "ЗМЗ-40905.10"}:
        return None
    direct = "провер" in question and "сцеплен" in question and re.search(r"привод|креплен|перед разборкой", question)
    if not (direct or diagnostic and "сцеплен" in symptoms and re.search(r"дребезж|шум", symptoms)):
        return None
    document = "zmz-40904-40905-rk-2025"
    job = "Проверить крепление коробки передач к блоку цилиндров двигателя."
    found_mount = search.passage(
        "Техническое обслуживание сцепления проверка крепления коробки передач к блоку цилиндров двигателя",
        r"Уход за сцеплением заключается в периодической проверке крепления коробки передач к блоку "
        r"цилиндров двигателя и степени изношенности фрикционных накладок\.",
        document_id=document, work=job, explain=lambda _: job)
    job = "Перед разборкой проверить работу привода выключения сцепления."
    found_drive = search.passage(
        "Перед проведением проверки деталей сцепления проверить работу привода выключения сцепления",
        r"Перед проведением проверки деталей сцепления проверить работу и отрегулировать привод выключения сцепления\.",
        document_id=document, work=job, explain=lambda _: job)
    clarify = ("Дребезжание, которое меняется при нажатии педали, само по себе не устанавливает причину. "
               "Уточните, при каких оборотах и в каком режиме автомобиля слышен звук.")
    missing = []
    if not found_mount:
        missing.append("В найденных источниках не подтверждена проверка крепления коробки передач.")
    if not found_drive:
        missing.append("В найденных источниках не подтверждена проверка привода выключения сцепления.")
    return "clutch-mounting", " ".join(missing), clarify, False
