"""Уточнение сигнала давления масла для ЗМЗ-40904.10 и ссылки на проверки."""

from decimal import Decimal
import re

from .qdrant_chunks import clean
from .sources import verified_pdf_pages
from .yamz236_oil import OIL_MEASUREMENT_QUESTION, oil_manual_excerpt, split_oil_observation_facts


ZMZ40904_CHECK_ID = "S-0189"
ZMZ40904_OIL_TOPIC = "zmz40904-oil-observation"
ZMZ40904_OIL_CONFIRMED_TOPIC = ZMZ40904_OIL_TOPIC + ":confirmed"
_MANUAL = "zmz-40904-40905-rk-2025"
ZMZ40904_OIL_WORK_SOURCES = frozenset({(_MANUAL, 95), (_MANUAL, 111)})
ZMZ40904_OIL_WORKS = ("Проверить уровень масла", "Проверить датчик аварийного давления масла и его цепь")
_MISSING_WORKS = "В найденных источниках не подтверждены проверки по физическим стр. 95 и 111 PDF."
_QUESTION = (
    "Уточните, при каких оборотах горит лампа и горит ли она постоянно. "
    "Есть ли стрелочный указатель давления масла? "
    f"{OIL_MEASUREMENT_QUESTION} Подойдёт показание стрелочного указателя или контрольного манометра. "
    "После прогрева слышен отчётливый стук? Уточните, где он слышен."
)
_CHECK = (
    "Фактическое давление масла неизвестно без показания прибора. Рекомендую проверить уровень масла "
    "и согласовать с механиком контрольный замер."
)

_WARNING_QUERY = "Не допускается эксплуатация двигателя с горящим сигнализатором аварийного давления масла"
_WARNING = (
    r"Не допускается эксплуатация двигателя с горящим сигнализатором аварийного давления масла\."
    r"(?: Это [^.!?]{1,300}\.)? Загорание сигнализатора допускается при [^.!?]{1,300}\."
)
_NOISE_QUERY = "выделяющиеся шумы и стуки двигатель не эксплуатировать гидротолкатели клапанов"
_NOISE = (
    r"При появлении в работающем двигателе выделяющихся шумов и стуков[^.!?]{1,200}?"
    r"двигатель не эксплуатировать\."
)
_LOUD_NOISE = (
    "При отчётливом шуме или стуке в работающем двигателе выясните причину "
    "и до устранения неисправности прекратите эксплуатацию."
)
_WARM_NOISE = (
    r"После запуска холодного двигателя возможно появление стука гидротолкателей клапанов, "
    r"который должен исчезнуть по мере прогрева двигателя до температуры охлаждающей жидкости "
    r"плюс 80\s*[…–-]+\s*90.{1,12}?\. Если стук не исчезает более чем через 30 мин "
    r"после достижения указанной температуры, необходимо проверить исправность гидротолкателей"
)
_WARM_TEXT = (
    "Краткий стук гидротолкателей после холодного пуска возможен. Если стук сохраняется более 30 минут "
    "после достижения температуры охлаждающей жидкости 80–90 °C, руководство предусматривает "
    "проверку исправности гидротолкателей. При аварийном сигнале или отчётливом стуке действует предупреждение о прекращении эксплуатации."
)
_HYDRAULIC_LOCATE = r"при помощи стетоскопа \(или другого прибора, усиливающего звук\) локализовать источник стука;"
_HYDRAULIC_FEED = r"Проверить подачу масла к гидротолкателям"
_HYDRAULIC_CHECK = _HYDRAULIC_LOCATE + r".{1,2500}?" + _HYDRAULIC_FEED
_HYDRAULIC_TEXT = (
    "После выполнения условий руководства источник стука локализуют стетоскопом. "
    "При проверке гидротолкателей проверяют подачу к ним масла."
)
_LEVEL_QUERY = "Проверку уровня производить на ровной горизонтальной площадке через 5 минут указатель уровня масла"
_LEVEL = (
    r"Проверку уровня производить при нахождении автомобиля на ровной горизонтальной площадке\."
    r".{1,500}?снова вынуть указатель и проверить уровень масла на указателе\."
)
_SENSOR_QUERY = "Постоянное горение лампы сигнализатора контрольный манометр давление масла выше 1 кгс/см2"
_SENSOR = (
    r"Постоянное горение лампы сигнализатора при работе двигателя \(стрелка указателя давления "
    r"или контрольный манометр показывают давление масла выше 1\s*кгс/см[2²]\) может быть следствием "
    r"неисправности датчика и замыкания на массу в цепи от датчика до лампы сигнализатора\."
    r".{0,320}?контрольная лампа на панели приборов должна погаснуть\."
)
_SENSOR_TEXT = (
    "При постоянном горении аварийной лампы и показании выше 1 кгс/см² возможна неисправность датчика "
    "аварийного давления масла или замыкание на массу в его цепи. "
    "Проверьте датчик аварийного давления масла и его цепь."
)
_ARROW_QUERY = "Датчик указателя давления масла 23.3829010 устанавливается на часть двигателей"
_ARROW = r"Датчик указателя давления масла 23\.3829010 \(рис\.\s*78\) устанавливается на часть двигателей\."

_QUESTIONS = re.compile(
    r"^(?:а\s+)?(?:что|как|почему|когда|где|како[ей]|сколько|"
    r"(?:можно|нужно|проверили|замерили|горит|показывает|установлен|есть)\s+ли)\b"
)
_UNCERTAIN = re.compile(r"\b(?:если|допустим|предположим|возможно|кажется|должно|по (?:данным )?ml|по руководству)\b")
_LAMP = re.compile(r"\b(?:ламп\w*|сигнализатор\w*)\b")
_OTHER_LAMP = re.compile(r"\b(?:диагностическ\w+\s+ламп\w*|(?:ламп\w*|сигнализатор\w*)\s+перегрев\w*|check engine)\b")
_STOPPED = re.compile(
    r"\b(?:двигател\w*\s+(?:заглуш\w*|останов\w*|не\s+(?:работ\w*|запущ\w*))"
    r"|(?:на\s+)?неработающ\w*\s+двигател\w*)\b"
)
_RUNNING = re.compile(r"\b(?:двигатель работает|(?:на\s+)?работающ\w*\s+двигател\w*)\b")
_ON = re.compile(r"\b(?:горит|горел[аио]?|загора\w*|загор\w*|не гаснет|не погасл\w*|не тухнет)\b")
_OFF = re.compile(r"\b(?:погасл\w*|потухл\w*|погас|прекратил\w* гореть|не гор\w*)\b")
_STEADY = re.compile(r"\b(?:постоянно|непрерывно|не гаснет|не погасл\w*|не тухнет|все время)\b")
_INTERMITTENT = re.compile(r"\b(?:не постоянно|не\s+горит\s+постоянно|только|периодически|мига\w*|мерца\w*)\b")
_EXCEPTION = re.compile(r"\b(?:минимальн\w*[^.!?]{0,55}холост\w*|резк\w*\s+тормож\w*)\b")
_UNSAFE_SPEED = re.compile(
    r"\b(?:(?:средн\w*|высок\w*|номинальн\w*|повышенн\w*|рабоч\w*)\s+"
    r"(?:оборот\w*|частот\w*\s+вращени\w*)|при разгоне|вне холостого хода)\b"
)
_INSTRUMENT = re.compile(r"\b(?:манометр\w*|стрелочн\w*\s+указател\w*|стрелк\w*)\b")
_OTHER_PRESSURE = re.compile(
    r"\b(?:давлени\w*\s+(?:топлив\w*|шин\w*|наддув\w*|воздух\w*)|в рампе|манометр\w*\s+(?:шин\w*|турбин\w*))\b"
)
_READING_LOST = re.compile(
    r"\b(?:не\s+(?:измер\w*|замер\w*)|показани\w*\s+(?:неизвест\w*|нет)|нет\s+показани\w*|"
    r"(?:показани\w*|замер)\s+(?:неверн\w*|ошибочн\w*)|ошиб\w*\s+(?:в\s+)?(?:показани\w*|замер\w*))\b"
)
_ARROW_ABSENT = re.compile(r"\b(?:(?:нет|отсутствует)\s+стрелочн\w*\s+указател\w*|стрелочн\w*\s+указател\w*\s+(?:нет|не установлен\w*|отсутствует))\b")
_UNIT = r"(?:кгс\s*/\s*см(?:\^?2|²)|кг\s+силы\s+на\s+квадратн\w*\s+см|кпа|мпа)"
_VALUE = re.compile(rf"(?<![\w.,<>≤≥+\-−])(\d+(?:[.,]\d+)?)\s*({_UNIT})(?!\w)")
_COMPARISON = re.compile(rf"(?:\b(?:выше|больше)\s+|>\s*)(\d+(?:[.,]\d+)?)\s*({_UNIT})(?!\w)")
_APPROXIMATE = re.compile(r"\b(?:около|примерно|порядка|от)\s+\d|\d\s*[-–—…]\s*\d")
_NOISE_WORD = re.compile(r"\b(?:стук\w*|шум\w*)\b")
_NOISE_NEGATED = re.compile(
    r"\b(?:(?:стук\w*|шум\w*)\s+(?:исчез\w*|прекратил\w*|не слыш\w*|нет)|нет\s+(?:стук\w*|шум\w*))\b"
)
_NOISE_PRONOUNCED = re.compile(r"\b(?:отчетлив\w*|выраженн\w*|выделяющ\w*|усилил\w*|громк\w*|сильн\w*)\b")
_WARM = re.compile(r"\b(?:после\s+прогрева|на\s+прогрет\w*|прогрет\w*\s+двигател\w*)\b")


def _statements(question):
    for sentence in re.split(r"(?<=[.!?;])\s+", question.casefold().replace("ё", "е").strip()):
        sentence = re.sub(r"^(?:уточн(?:яю|ение)|поправка)\s*:\s*", "", sentence)
        if not sentence or sentence.endswith("?") or _QUESTIONS.search(sentence) or _UNCERTAIN.search(sentence):
            continue
        for fact in split_oil_observation_facts(sentence):
            yield from re.split(r"(?<!\d),|,(?!\d)|\s+но\s+", fact)


def _pressure_confirmation(statement):
    if not _INSTRUMENT.search(statement) or _OTHER_PRESSURE.search(statement):
        return None
    if _READING_LOST.search(statement) or _APPROXIMATE.search(statement):
        return "missing"
    if re.search(r"\b(?:ниже|меньше|не\s+(?:выше|больше|более))\b|[<≤≥]", statement):
        return "not_above"
    comparison = _COMPARISON.search(statement)
    matches = list(_VALUE.finditer(statement))
    if comparison is None and len(matches) != 1:
        return "missing"
    value, unit = (comparison or matches[0]).groups()
    pressure = Decimal(value.replace(",", "."))
    if unit in {"кпа", "мпа"}:
        threshold = Decimal("98.0665") if unit == "кпа" else Decimal("0.0980665")
    else:
        threshold = Decimal("1")
    above = pressure >= threshold if comparison else pressure > threshold
    return "above" if above else "not_above"


def _observe(question):
    observed = {}
    for statement in _statements(question):
        if _STOPPED.search(statement):
            observed["stopped"] = True
        elif _RUNNING.search(statement):
            observed["stopped"] = False
        lamp = _LAMP.search(statement) or re.fullmatch(
            r"\s*(?:горит постоянно|горела постоянно|горит|не гаснет|погасла)\s*[.!]?", statement)
        if lamp and not _OTHER_LAMP.search(statement):
            if _OFF.search(statement) and (_INTERMITTENT.search(statement) or not _STEADY.search(statement)):
                observed.update(on=False, steady=False)
            elif _ON.search(statement):
                observed["on"] = True
                if _INTERMITTENT.search(statement):
                    observed["steady"] = False
                elif _STEADY.search(statement):
                    observed["steady"] = True
            if _EXCEPTION.search(statement):
                observed["speed"] = False
            elif _UNSAFE_SPEED.search(statement):
                observed["speed"] = True
        if _NOISE_WORD.search(statement):
            if _NOISE_NEGATED.search(statement):
                observed.update(noise=False, warm=False)
            elif _NOISE_PRONOUNCED.search(statement) or _WARM.search(statement):
                observed["noise"] = True
                if _WARM.search(statement):
                    observed["warm"] = True
        if (_READING_LOST.search(statement) or _ARROW_ABSENT.search(statement)) and not _OTHER_PRESSURE.search(statement):
            observed.update(pressure=False, known=False)
        elif (pressure := _pressure_confirmation(statement)) is not None:
            observed.update(pressure=pressure == "above", known=pressure != "missing")
    return observed


def zmz40904_oil_observation_question(question):
    """Распознаёт сообщения механика о работе лампы, стуке или показании прибора."""
    return bool(_observe(question))


def _facts(topic, observation):
    suffix = topic.partition(":")[2]
    state = {"on", "steady", "pressure", "known"} if suffix == "confirmed" else set(filter(None, suffix.split("+")))
    for fact, value in observation.items():
        if value:
            state.add(fact)
        else:
            state.discard(fact)
    return state


def _hydraulic_requested(question, state):
    return "warm" in state or bool(re.search(r"гидротолкател|(?:стук|шум).{0,60}(?:клапан|после прогрев)", question, re.I))


def _chosen_works(question):
    named = question.strip(" .!?").casefold()
    return tuple(work for work in ZMZ40904_OIL_WORKS if work.casefold() == named)


_WORK_DETAILS = {
    ZMZ40904_OIL_WORKS[0]: (95, _LEVEL_QUERY, _LEVEL, None),
    ZMZ40904_OIL_WORKS[1]: (111, _SENSOR_QUERY, _SENSOR, _SENSOR_TEXT),
}


def select_zmz40904_oil_measurement(search, question, topic, *, general, asks_measurement):
    """Разделяет предупреждения, наблюдения механика и работы для черновика."""
    related = topic.partition(":")[0] == ZMZ40904_OIL_TOPIC
    observation = _observe(question) if related else {}
    named = _chosen_works(question) if topic == ZMZ40904_OIL_CONFIRMED_TOPIC else ()
    state = _facts(topic, observation) if related else set()
    hydraulic = _hydraulic_requested(question, state)
    if not (observation or named or general or asks_measurement or (hydraulic and related)):
        return None

    confirmed = bool(named or observation and {"steady", "pressure"} <= state and "stopped" not in state)
    unsafe_light = {"on", "speed"} <= state and "stopped" not in state
    loud = "noise" in state
    warning = oil_manual_excerpt(search, _MANUAL, 91, _WARNING_QUERY, _WARNING)
    if warning:
        search.add(*warning, kind="risk")
    noise = oil_manual_excerpt(search, _MANUAL, 91, _NOISE_QUERY, _NOISE)
    if noise:
        search.add(*noise, kind="risk", text=_LOUD_NOISE)

    if hydraulic:
        warm = oil_manual_excerpt(search, _MANUAL, 142, "стук гидротолкателей 30 мин прогрев", _WARM_NOISE)
        if warm:
            search.add(*warm, text=_WARM_TEXT)
        locate = oil_manual_excerpt(search, _MANUAL, 143, "локализовать источник стука стетоскопом", _HYDRAULIC_LOCATE)
        feed = oil_manual_excerpt(search, _MANUAL, 143, "проверить подачу масла к гидротолкателям", _HYDRAULIC_FEED)
        if locate and feed and locate[0].get("source_sha256") == feed[0].get("source_sha256"):
            search.add(locate[0], locate[1] + " … " + feed[1], text=_HYDRAULIC_TEXT)

    if confirmed:
        expected = named or ZMZ40904_OIL_WORKS
        found = 0
        for work in expected:
            page, lookup, pattern, explanation = _WORK_DETAILS[work]
            evidence = oil_manual_excerpt(search, _MANUAL, page, lookup, pattern)
            if evidence:
                search.add(*evidence, text=explanation, work=work)
                found += 1
        topic, missing = ZMZ40904_OIL_CONFIRMED_TOPIC, "" if found == len(expected) else _MISSING_WORKS
        clarification = "" if named else (
            "Механик сообщил, что аварийная лампа горит постоянно, а показание давления выше 1 кгс/см²."
        )
    else:
        if level := oil_manual_excerpt(search, _MANUAL, 95, _LEVEL_QUERY, _LEVEL):
            search.add(*level)
        if arrow := oil_manual_excerpt(search, _MANUAL, 89, _ARROW_QUERY, _ARROW):
            search.add(*arrow)
        topic = ZMZ40904_OIL_TOPIC + (":" + "+".join(sorted(state)) if state else "")
        missing = ""
        clarification = _QUESTION
        if "known" not in state:
            clarification = _CHECK + " " + clarification
        elif "pressure" not in state:
            clarification = "Показание выше 1 кгс/см² пока не подтверждено. " + clarification
        if "stopped" in state:
            clarification = "Условие проверки датчика относится к показанию на работающем двигателе. " + clarification
    if ((unsafe_light and warning) or (loud and noise)) and not named:
        clarification = "Прекратите эксплуатацию до выяснения причины. " + clarification
    return topic, missing, clarification.strip(), bool(missing or not confirmed)


def complete_zmz40904_oil_sources(result, registry, root, engine, question):
    """Добавляет недостающую выдержку из оригинала с проверкой SHA-256."""
    topic = result.get("topic", "")
    expected = {}
    if topic == ZMZ40904_OIL_CONFIRMED_TOPIC:
        works = _chosen_works(question) or ZMZ40904_OIL_WORKS
        expected.update({page: (pattern, text, work)
                         for work in works for page, _, pattern, text in [_WORK_DETAILS[work]]})
    if _hydraulic_requested(question, _facts(topic, _observe(question))):
        expected[142] = (_WARM_NOISE, _WARM_TEXT, None)
        expected[143] = (_HYDRAULIC_CHECK, _HYDRAULIC_TEXT, None)
    evidence = result["evidence"]
    cited = {row["page"] for row in evidence if row["document_id"] == _MANUAL}
    pages = tuple(page for page in expected if page not in cited)
    originals = verified_pdf_pages(registry, root, _MANUAL, engine, pages)
    for page, source in originals.items():
        pattern, statement, work = expected[page]
        match = re.search(pattern, clean(source["text"]), re.I)
        if match:
            evidence.append(dict(source, excerpt=match.group(), text=statement or match.group(), kind="fact",
                                 work=work, required=True, source_origin="original_pdf",
                                 applicability=[f"{engine}|all"]))
    if result.get("missing") == _MISSING_WORKS:
        needed = {page for page, (_, _, work) in expected.items() if work}
        present = {row["page"] for row in evidence if row["document_id"] == _MANUAL and row.get("work")}
        if needed <= present:
            result["missing"] = ""
            result["needs_clarification"] = False
