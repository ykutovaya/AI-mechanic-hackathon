"""Выбирает проверки генератора и коробки по признакам и строкам руководства."""

import re

from .expert_answers import generic_diagnostic_question


_MANUAL_236 = "yamz-236m2-re-2010"
_SCOPE_236_SHA256 = "4268a9519e02a668a826fe6d154de0360f6767015868ca2de7410ba570d73dd1"
_MANUAL_536 = "yamz-536-re-2016"
_MANUAL_ZMZ = "zmz-40904-40905-rk-2025"
_GEARBOX = re.compile(r"(?<![А-ЯA-Z0-9])(?:ЯМЗ[ -]*)?236(ПО|УО|П|Н|Л|У)(?![А-ЯA-Z0-9])", re.I)
_CHARGE = "Проверить целостность цепи возбуждения генератора и надёжность соединений в местах контактов."
_MOUNT = "Проверить надёжность крепления генератора к двигателю и соединений проводов с выводами генератора."
_DRIVE = "Проверить исправность и правильность установки генератора."
_NOISE = "Проверить, нет ли в генераторе постороннего предмета и признаков износа или разрушения подшипников."
_GEAR_MOUNT = "Проверить затяжку болтов крепления картера сцепления к картеру маховика и болтов крепления задней опоры коробки передач"
_GEAR_MOUNT_236P = _GEAR_MOUNT + ". При ослаблении подтянуть"
_GEAR_PROMPT = ("Какая модель коробки передач установлена? "
                "Перепишите полное обозначение с таблички коробки или из документации машины.")
_GEAR_REPORT_SOURCE = r"(?:по|на) табличке|(?:по|в) документации(?: машины)?|по паспорту|в паспорте"



def _clean(text):
    return " ".join(re.sub(r"(?<=\w)\s*-\s*\n\s*(?=\w)", "", text).split())


def _hits(search, query, document, page):
    return (hit for hit in search.find(query) if hit["document_id"] == document and hit["page"] == page
            and search.engine in hit["engine_models"])


def _right_column(text, first, stop):
    """Читает правую колонку одной строки таблицы, если PDF сохранил колонки пробелами."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if first not in _clean(line).casefold() or "Проверить" not in line:
            continue
        start = line.index("Проверить")
        right = [line[start:]]
        for line in lines[i + 1:]:
            if not line.strip() or re.search(stop, _clean(line), re.I):
                break
            columns = list(re.finditer(r" {2,}(?=\S)", line))
            if columns:
                # Отступы в соседних печатных строках на несколько знаков различаются.
                split = min(columns, key=lambda gap: abs(gap.end() - start))
                right.append(line[split.end():])
        return _clean("\n".join(right))
    return ""


def _discharge(search):
    # При разряде таблица 4 рассматривает обрыв цепи на номинальных оборотах (PDF 55–56).
    for hit in _hits(search, "Обрыв или плохой контакт в цепи возбуждения генератора щеточный узел", _MANUAL_236, 56):
        right = _right_column(hit["text"], "обрыв или плохой", r"пробой|чрезмерно")
        direct = re.match(r"Проверить целостность в цепи возбуждения, щеточный узел", right, re.I)
        if direct:
            search.add(hit, direct.group(), text="Если амперметр показывает разряд при номинальных оборотах, "
                       "проверить целостность цепи возбуждения и щёточный узел генератора.")
            break

    for hit in _hits(search, "Колебание нагрузки генератора проверить целостность цепи возбуждения в местах контактов",
                     _MANUAL_236, 57):
        right = _right_column(hit["text"], "недостаточен контакт в цепи", r"^\d+$")
        direct = re.match(r"Проверить целостность цепи возбуждения и надежность соединения в местах контактов", right, re.I)
        if direct:
            search.add(hit, direct.group(), text="В строке о колебании нагрузки генератора дана проверка контактов. " + _CHARGE,
                       work=_CHARGE)
            return ""
    if search.evidence:
        # PDF 56 даёт самостоятельную проверку даже при отсутствии строки о контактах на PDF 57.
        search.evidence[-1]["work"] = "При разряде на номинальных оборотах проверить целостность цепи возбуждения и щёточный узел генератора."
        return ""
    return "В найденных фрагментах нет порядка проверки цепи возбуждения генератора при разряде."


def _generator_mount(search):
    for hit in _hits(search, "Генератор надежность его крепления к двигателю соединений проводов с выводами",
                     _MANUAL_ZMZ, 101):
        row = re.search(r"(?:очищать генератор от грязи, )?проверять надежность его крепления к двигателю и "
                        r"надежность соединений проводов с выводами генератора", _clean(hit["text"]), re.I)
        if row:
            search.add(hit, row.group(), text=_MOUNT, work=_MOUNT)
            return ""
    return "В найденных фрагментах нет проверки крепления генератора и соединений его проводов."


def _rows(hit):
    for table in hit.get("tables", ()):
        for row in table.get("rows", ()):
            if len(row) >= 2 and row[0] and row[1]:
                yield _clean(row[0]), _clean(row[1])


def _localized_noise(search):
    for hit in _hits(search, "11.5 неисправны или установлены неправильно генератор проверить исправность и правильность установки агрегата", _MANUAL_536, 215):
        found = False
        for cause, remedy in _rows(hit):
            if cause.startswith("11.5 ") and "генератор" in cause.casefold() and (action := re.match(
                    r"Проверить исправность и правильность установки агрегата\b", remedy, re.I)):
                search.add(hit, "д) генератор. " + action.group(),
                           text="По описанию ML шум исчез после снятия ремня. " + _DRIVE, work=_DRIVE)
                found = True
                break
        if found:
            break

    for hit in _hits(search, "31 шум или стук в генераторе посторонний предмет изношены или разрушены подшипники",
                     _MANUAL_536, 231):
        causes = [cause for cause, remedy in _rows(hit) if
                  cause.casefold() == "31.3 наличие в генераторе постороннего предмета"
                  or cause.casefold() == "31.4 изношены или разрушены подшипники"]
        if len(set(causes)) == 2:
            search.add(hit, ". ".join(causes), text=_NOISE, work=_NOISE)
            break
    return "" if search.evidence else "В найденных разделах нет применимой проверки генератора при таком шуме."


def confirmed_gearbox_model(question):
    """Возвращает одно явно подтверждённое механиком обозначение КП семейства ЯМЗ-236."""
    text = question.upper().replace("‑", "-").replace("–", "-").strip().rstrip(".!")
    # При исправлении механика разбираем только сведения об установленной сейчас коробке.
    current = re.split(r"\b(?:СЕЙЧАС|ТЕПЕРЬ|В НАСТОЯЩЕЕ ВРЕМЯ)\b", text)
    if len(current) > 1:
        text = current[-1].strip(" .,—-:")
    if ("?" in question or re.search(r"\b(?:НЕ|ИЛИ|ЕСЛИ|ВОЗМОЖНО|НАВЕРНОЕ|КАЖЕТСЯ|ПРЕДПОЛОЖИТЕЛЬНО)\b", text)
            or re.search(r"\b(?:ВОЗМОЖНА|МОЖЕТ|ВРОДЕ|ВЕРОЯТНО|УТОЧНИТЬ)\b", text)):
        return None
    models = set(_GEARBOX.findall(text))
    if len(models) != 1:
        return None
    prefix = text[:_GEARBOX.search(text).start()].strip(" .,—-:")
    statement = re.fullmatch(rf"(?:(?:на машине|у нас|{_GEAR_REPORT_SOURCE})[\s,:]*)?"
                             r"(?:(?:установлен[аы]?|стоит|указан[ао]?)[\s,:]*)?"
                             r"(?:(?:модель|маркировка|обозначение)[\s,:]*)?"
                             r"(?:(?:кпп?|коробк[аиу](?: передач)?)\s*)?", prefix, re.I)
    suffix = text[_GEARBOX.search(text).end():].strip(" .,—-:")
    if suffix not in {"", "СТОИТ", "УСТАНОВЛЕНА"} or not statement:
        return None
    return "ЯМЗ-236" + models.pop()


def gearbox_observation_question(question):
    """Отличает утверждение или отзыв обозначения КП от справочного вопроса."""
    q = question.casefold().strip(" .!")
    if "?" in q or re.match(r"(?:как|чем|почему|подойд[её]т|можно|есть ли|нужно ли|сколько|где)\b", q):
        return False
    if confirmed_gearbox_model(q) or re.match(rf"(?:{_GEAR_REPORT_SOURCE}|zf)\b", q):
        return True
    if q in {"не знаю", "не уверен", "не уверена", "не проверял", "не проверяла"}:
        return True
    subject = re.search(r"\b(?:кпп?|коробк\w*|модел\w*|обозначени\w*|zf)\b", q) or _GEARBOX.search(q)
    return bool(subject and re.search(r"не\b|неизвестн|ошиб|кажется|возможно|стоит|установлен|другая|уточн"
                                      r"|раньше|прошл|стар\w* коробк|сняли|заменили|теперь|сейчас", q))


def gearbox_overview_question(question):
    """Распознаёт общий вопрос по уже выбранной коробке и проверке."""
    q = question.casefold().strip(" .!?")
    overview = re.fullmatch(r"(?:как проверить|что проверить в) коробк[уе] передач(?: при (?:признаках|симптомах) "
                            r"(?:этой )?неисправности)?", q)
    return bool(overview or generic_diagnostic_question(q))


def gearbox_work(model, *, legacy_work=False):
    """Даёт формулировку работы для подтверждённой коробки семейства ЯМЗ-236."""
    return _GEAR_MOUNT_236P if model == "ЯМЗ-236П" and not legacy_work else f"{_GEAR_MOUNT} для КП {model}."


def select_gearbox_inspection(search, model, *, legacy_work=False):
    if model is None:
        prompt = _GEAR_PROMPT + " Для коробки другого семейства понадобится руководство по её модели."
        return "gearbox-noise:unknown", "", prompt, True
    # Эти исполнения перечислены в разделе «Коробки передач семейства ЯМЗ-236», PDF 128.
    scope = next((hit for hit in _hits(search, "Коробки передач семейства ЯМЗ-236 в зависимости от назначения модификации",
                                      _MANUAL_236, 128)
                  if model.removeprefix("ЯМЗ-236") in _GEARBOX.findall(_clean(hit["text"]).upper())), None)
    for hit in _hits(search, "Повышенный шум при работе коробки передач перекос болты крепления задней опоры",
                     _MANUAL_236, 135):
        # Этот экземпляр проверен по PDF 128, даже если лист с перечнем моделей не вошёл в индекс.
        if scope is None and hit.get("source_sha256") != _SCOPE_236_SHA256:
            continue
        if "повышенный шум при работе коробки передач" not in _clean(hit["text"]).casefold():
            continue
        action = _right_column(hit["text"], "перекос осей", r"самопроизвольн|износ|течь")
        if not re.search(r"Проверить затяжку и при необходимости подтянуть болты крепления картера сцепления к "
                         r"картеру маховика и болты крепления задней опоры коробки передач", action, re.I):
            continue
        if scope is not None:
            search.add(scope, model, text=f"Модель {model} входит в указанное в руководстве семейство коробок.")
        work = gearbox_work(model, legacy_work=legacy_work)
        text = f"Для КП {model}. {work}." if work == _GEAR_MOUNT_236P else work
        search.add(hit, action, text=text, work=work)
        return "gearbox-noise:" + model, "", "", False
    return "gearbox-noise:unknown", f"Для коробки {model} в найденных фрагментах нет подтверждённой строки о проверке креплений при шуме.", "", False


def select_generator_gearbox(search, code, question, symptoms, *, topic="", diagnostic=False):
    """Возвращает применимые проверки или уточнение, если для выбора нужна модель коробки."""
    q = question.casefold()
    symptoms = symptoms.casefold()
    if code == "F07":
        if search.engine != "ЯМЗ-236М2" or not ("шум" in symptoms and "нейтрал" in symptoms and "сцеплен" in symptoms):
            return None
        overview = gearbox_overview_question(q)
        model = confirmed_gearbox_model(q)
        stated = gearbox_observation_question(q)
        observed_model = (confirmed_gearbox_model(topic.partition(":")[2])
                          if topic.startswith("gearbox-noise:") else None)
        work_text = q.strip(" .!")
        # Старую формулировку проверяем при редактировании сохранённого черновика.
        legacy_work = bool(observed_model and work_text == f"{_GEAR_MOUNT} для КП {observed_model}".casefold())
        repeats_work = legacy_work or observed_model == "ЯМЗ-236П" and work_text == _GEAR_MOUNT_236P.casefold()
        if not (diagnostic or overview or model or repeats_work or topic.startswith("gearbox-noise") and stated):
            return None
        if (overview and not stated) or repeats_work:
            model = observed_model
        return select_gearbox_inspection(search, model, legacy_work=legacy_work)

    if code != "F05":
        return None
    overview = r"(?:как проверить генератор|что проверить (?:на генераторе|в генераторе))"
    first, _, detail = q.strip(" .!?").partition(" и ")
    about_generator = bool(re.fullmatch(overview, first))
    voltage = about_generator and "напряжени" in detail and re.search(r"измер|замер|услови|норматив", detail)
    same_work = q.rstrip(". ") in {work.casefold().rstrip(".") for work in (_CHARGE, _MOUNT, _DRIVE, _NOISE)}
    if not (diagnostic or same_work or about_generator and (not detail or voltage)):
        return None
    if search.engine == "ЯМЗ-236М2" and "разряд" in symptoms:
        missing = _discharge(search)
    elif search.engine in {"ЗМЗ-40904.10", "ЗМЗ-40905.10"} and "генератор" in symptoms and re.search(r"разряд|гул|шум", symptoms):
        missing = _generator_mount(search)
    elif search.engine == "ЯМЗ-536" and re.search(r"шум.{0,90}(?<!не )(?:исчез|пропа)\w* после снятия ремня", symptoms):
        missing = _localized_noise(search)
    else:
        return None
    if voltage:
        missing = (missing + " В найденных фрагментах о проверке генератора не указаны условия измерения напряжения.").strip()
    return "generator-inspection", missing, "", False

