"""Выбирает первичную проверку клапанов или ремней по руководству выбранного двигателя."""

import re

from .qdrant_chunks import clean
from .sources import verified_pdf_pages


HYDRAULIC = (r"Гидротолкатели обеспечивают беззазорный контакт кулачка распределительного вала "
             r"с торцом клапана за сч[её]т давления масла и действия пружины гидрокомпенсатора\.")
COLD_236 = r"Тепловые зазоры регулировать на холодном двигателе\b"
WORK_236 = "Проверить щупом зазоры впускного и выпускного клапанов между торцом клапана и носком коромысла."
GAUGE_236 = (r"Вставив щуп в зазор между торцом клапана и носком коромысла, проверить зазоры у впускного и "
             r"выпускного клапанов первого цилиндра и, если необходимо, отрегулировать их в пределах "
             r"\d+[,.]\d+\s*[-–—]\s*\d+[,.]\d+\s*мм\.")
VALVE_TOPIC = "valve-clearance-diagnostic"
BELT_TOPIC = "accessory-belt"


def _include_measurement_conditions(evidence, engine):
    """Связывает одну работу в заявке со страницами режима и измерения щупом."""
    if engine not in {"ЯМЗ-236М2", "ЯМЗ-536"}:
        return
    method = next((item for item in evidence if item.get("work") and "щупом" in item["work"]), None)
    if not method or not method["work"].startswith("Проверить"):
        return
    condition = next((item for item in evidence if item["document_id"] == method["document_id"] and
                      "на холодном двигателе" in item["excerpt"].casefold()), None)
    if condition:
        mode = "На холодном двигателе"
        if "один час после его остановки" in condition["excerpt"]:
            mode += " или не ранее чем через час после его остановки"
        work = mode + " " + method["work"][0].lower() + method["work"][1:]
        condition["work"] = method["work"] = work


def select_valve_check(search):
    """Оставляет регулировку и замену за пределами первой диагностической работы."""
    if search.engine in {"ЗМЗ-40904.10", "ЗМЗ-40905.10"}:
        search.passage("Гидротолкатели обеспечивают беззазорный контакт кулачка с торцом клапана", HYDRAULIC)
        work = ("Если стук сохраняется после указанного в руководстве прогрева, "
                "проверить подачу масла к гидротолкателям и гидронатяжителям.")
        found = search.passage(
            "Если после прогрева стуки не исчезнут проверить подачу масла к гидротолкателям",
            r"По мере прогрева двигателя до температуры охлаждающей жидкости [^.!?]{5,180}стуки должны исчезнуть\.\s*"
            r"Если стуки не исчезнут, необходимо проверить подачу масла к гидротолкателям и гидронатяжителям",
            explain=lambda excerpt: excerpt.replace("\uf0b0", "°").rstrip(".") + ".", work=work)
    elif search.engine == "ЯМЗ-236М2":
        search.passage("Тепловые зазоры регулировать на холодном двигателе ЯМЗ-236М2", COLD_236,
                       document_id="yamz-236m2-re-2010")
        found = search.passage(
            "Вставив щуп в зазор между торцом клапана и носком коромысла проверить зазоры в пределах",
            GAUGE_236, work=WORK_236, document_id="yamz-236m2-re-2010")
    elif search.engine == "ЯМЗ-536":
        search.passage(
            "Регулировку тепловых зазоров на холодном двигателе один час после остановки",
            r"Регулировку (?:тепловых )?зазоров в клапанном механизме проводить на холодном двигателе или "
            r"не ранее,? чем через один час после его остановки", document_id="yamz-536-re-2016")
        search.passage(
            "Величины зазоров между чашкой коромысла и торцом траверсы для впускных выпускных клапанов",
            r"Величины зазоров между чашкой коромысла и торцом траверсы клапанов должны быть:"
            r".{0,16}для впускных клапанов:\s*\d+[,.]\d+\s*[-–—]\s*\d+[,.]\d+\s*мм,"
            r".{0,16}для выпускных клапанов:\s*\d+[,.]\d+\s*[-–—]\s*\d+[,.]\d+\s*мм\.",
            explain=lambda excerpt: excerpt.replace("\uf02d", "–"))
        found = search.passage(
            "вставить в зазор траверса чашка коромысла щуп соответствующей толщины",
            r"вставить в зазор «траверса \d+\s*[-–—]\s*чашка коромысла \d+» щуп,\s*соответствующей толщины",
            work="Проверить щупом зазор между торцом траверсы клапанов и чашкой коромысла.")
    else:
        return None
    _include_measurement_conditions(search.evidence, search.engine)
    return "" if found else "В найденных фрагментах нет порядка первичной проверки клапанного механизма."


def complete_valve_sources(result, registry, root, engine):
    """Восстанавливает условия из применимого РЭ, если поиск вернул только часть процедуры."""
    if engine == "ЯМЗ-236М2":
        doc = "yamz-236m2-re-2010"
        passages = [(31, COLD_236, None), (33, GAUGE_236, WORK_236)]
    elif engine in {"ЗМЗ-40904.10", "ЗМЗ-40905.10"}:
        doc = "zmz-40904-40905-rk-2025"
        passages = [(44, HYDRAULIC, None)]
    else:
        return
    needed = [(page, pattern, work) for page, pattern, work in passages
              if not any(item["document_id"] == doc and item["page"] == page
                         and re.search(pattern, item["excerpt"], re.I) for item in result["evidence"])]
    if not needed:
        return
    pages = verified_pdf_pages(registry, root, doc, engine, [p for p, _, _ in needed])
    for page, pattern, work in needed:
        source = pages.get(page)
        match = re.search(pattern, clean(source["text"]), re.I) if source else None
        if not match:
            continue
        excerpt = match.group()
        item = dict(source, excerpt=excerpt, text=excerpt, work=work, kind="fact", required=True,
                    applicability=[f"{engine}|all"])
        if work:
            result["evidence"].append(item)
            result["missing"] = ""
        else:
            result["evidence"].insert(0, item)
    _include_measurement_conditions(result["evidence"], engine)


def check_zmz_belts(search):
    work = ("Визуально проверить состояние ремней привода вспомогательных агрегатов. "
            "Проверить подшипники натяжного и направляющих роликов.")
    return search.passage(
        "Проверка состояния ремней привода вспомогательных агрегатов и подшипников роликов",
        r"Проверку состояния ремней привода вспомогательных агрегатов и подшипников натяжного "
        r"и направляющих роликов производить при каждом техническом обслуживании\.\s*"
        r"Состояние ремней проверять визуально\.", explain=lambda _: work, work=work)


_DRIVES = {
    "водяного насоса": r"водян\w* насос\w*|помп\w*",
    "компрессора": r"компрессор\w*",
    "генератора": r"генератор\w*",
}


_DRIVE_ANSWER = re.compile(
    r"(?:(?:нет|уточняю|исправляю),?\s+)?(?:(?:это|свистит|шумит)\s+)?(?:рем(?:ень|ня|ни)\s+)?"
    r"(?:привод[а-яё]*\s+)?(?:" + "|".join(_DRIVES.values()) + r")[.! ]*", re.I)
_UNSURE = re.compile(r"\b(?:не знаю|не уверен\w*|неизвест\w*|не помню|кажется|возможно|наверное|"
                    r"вероятно|предположительно|или|не)\b", re.I)


def belt_drive_in(text):
    """Выделяет один привод по названию агрегата."""
    drives = [name for name, pattern in _DRIVES.items() if re.search(pattern, text, re.I)]
    return drives[0] if len(drives) == 1 else None


def belt_observation_question(question):
    """Отличает выбор или отзыв привода от вопроса о его обслуживании."""
    q = question.strip().casefold()
    if _DRIVE_ANSWER.fullmatch(q):
        return True
    if "?" in q or re.match(r"(?:как\w*|что|чем|где|зачем|можно|стоит|нужно|следует|сколько)\b", q):
        return False
    return bool(_UNSURE.search(q) and (
        re.search(r"рем(?:ень|н[еия])", q) or any(re.search(pattern, q) for pattern in _DRIVES.values())
        or re.fullmatch(r"(?:пока )?(?:не знаю|не уверен[а]?|не помню|неизвестно)[. !]*", q)))


def _belt_236(search, focus):
    """Выбирает из предложения норму только для одного явно названного привода."""
    query = "Нормально натянутые ремни усилие на середину ветви ремень компрессора водяного насоса генератора"
    drive = belt_drive_in(focus)
    if not drive:
        question = "Какой ремень свистит: водяного насоса, компрессора или генератора?"
        search.passage(query, r"Нормально натянутые ремни при нажатии на середину ветви с усилием \d+ Н "
                       r"\([^)]*кгс\) должны прогибаться:", explain=lambda s: s.split(" должны")[0] +
                       ". Допустимый прогиб зависит от привода.")
        return "", question, True
    pattern = (r"Нормально натянутые ремни при нажатии на середину ветви с усилием (?P<force>\d+ Н \([^)]*кгс\)) "
               r"должны прогибаться:.{0,230}?\bрем(?:ень|ни)(?: привода)? " + drive +
               r"\s*[-–—]\s*на (?P<deflection>\d+\s*[-–—]\s*\d+ мм(?: на короткой ветви)?)")
    work = f"Проверить натяжение ремня {drive} по прогибу при нажатии на середину ветви."
    def explain(excerpt):
        match = re.search(pattern, excerpt, re.I)
        return f"При нажатии на середину ветви с усилием {match['force']} прогиб ремня {drive} — {match['deflection']}."
    found = search.passage(query, pattern, explain=explain, work=work)
    return ("" if found else "Для названного привода не найден допуск с усилием измерения."), "", False


def _belt_536(search, question, observation):
    """Различает перечень работ ТО и способ проверки натяжения на остановленном двигателе."""
    document = "yamz-536-re-2016"
    found = search.passage(
        "Проверить состояние и натяжение приводных ремней", r"Проверить состояние и натяжение приводных ремней",
        document_id=document, work="Проверить состояние и натяжение приводных ремней.")
    tensioner = search.passage(
        "проверить состояние натяжителя ремня на наличие посторонних шумов или люфта",
        r"При каждом втором ТО-2 проверить состояние натяжителя ремня на наличие посторонних шумов или люфта",
        document_id=document, work="Проверить натяжитель ремня на посторонние шумы и люфт.")
    missing = [] if found or tensioner else ["В найденных разделах нет перечня проверок ремённого привода."]
    if not (observation or re.search(r"натяж|прогиб|метк", question)):
        return " ".join(missing), "", False

    automatic = search.passage(
        "Натяжение ремней привода генератора и агрегатов осуществляется автоматически",
        r"Натяжение ремней привода генератора и агрегатов в процессе эксплуатации осуществляется "
        r"автоматически и не требует дополнительных регулировок\.", document_id=document)
    stopped = search.passage(
        "Контроль натяжения ремней выполнять только на неработающем двигателе",
        r"Контроль натяжения ремней выполнять только на неработающем двигателе\.", document_id=document,
        explain=lambda _: "Проверяйте натяжение только на неработающем двигателе.")
    marks = stopped and search.passage(
        "В рабочем положении метка поворотного корпуса должна находиться внутри зоны трех меток неподвижного корпуса",
        r"В рабочем положении метка поворотного корпуса должна находиться внутри зоны тр[её]х меток "
        r"неподвижного корпуса\.", document_id=document)
    if not automatic:
        missing.append("В найденных фрагментах не указано, предусмотрена ли регулировка натяжения.")
    if not marks:
        missing.append("Порядок безопасной проверки по меткам натяжителя в найденных фрагментах не подтверждён.")
    clarify = ""
    if not re.search(r"генератор|водян\w* насос|вентилятор|кондиционер", question):
        clarify = "Какой ремень свистит: ремень генератора или другого агрегата?"
    return " ".join(missing), clarify, False


def select_belt_check(search, question, symptoms, diagnostic, topic):
    """Уточняет привод, если от него зависит работа или норматив."""
    observation = topic == BELT_TOPIC and (belt_observation_question(question) or (
        search.engine == "ЯМЗ-536" and re.fullmatch(
            r"(?:это\s+)?(?:ремень\s+)?(?:привода\s+)?(?:вентилятора|компрессора кондиционера)[.! ]*", question.strip())))
    if not (diagnostic or observation or re.search(r"рем(?:ень|н[яеи])", question)
            and re.search(r"осмотр|провер|натяжени|прогиб|пробуксов", question)):
        return None
    if search.engine == "ЯМЗ-236М2":
        focus = symptoms if diagnostic else question
        if observation and not _DRIVE_ANSWER.fullmatch(question.strip()):
            focus = ""
        return _belt_236(search, focus)
    if search.engine in {"ЗМЗ-40904.10", "ЗМЗ-40905.10"}:
        found = check_zmz_belts(search)
    elif search.engine == "ЯМЗ-536":
        return _belt_536(search, question, observation)
    elif search.engine == "ЗМЗ-5234.10" and "вентилятор" in (question + " " + symptoms):
        work = "Проверить, есть ли пробуксовка ремня вентилятора."
        found = search.passage(
            "Пробуксовка ремня вентилятора отрегулировать натяжение ремня",
            r"Пробуксовка ремня вентилятора\s*(?:\|\s*)?Отрегулировать натяжение ремня",
            explain=lambda _: work + " Регулировка натяжения предусмотрена при подтверждённой пробуксовке.", work=work)
    else:
        return None
    return ("" if found else "В найденных разделах нет подходящей проверки ремённого привода."), "", False
