"""Выделяет диагностическую работу из применимой строки руководства."""

import re

from .qdrant_chunks import clean
from .sources import verified_pdf_pages


_INTAKE = "Проверить крепление соединительных рукавов и состояние воздухопроводов после турбокомпрессора."
_GAP = "Проверить зазор между электродами свечи круглым щупом."
_PLUG = "Осмотреть свечу и проверить зазор между электродами круглым щупом."
_COLOUR = "Проверить цвет изолятора свечи."
_CHAIN = "Измерить удлинение цепей привода ГРМ."
_CHAIN_INSPECTION = "Проверить удлинение цепей привода ГРМ"
CHAIN_CHECK_ID = "S-0152"
CHAIN_INSPECTION_TOPIC = "f14-chain-assessment"
CHAIN_REPLACEMENT_TOPIC = CHAIN_INSPECTION_TOPIC + ":replacement"
CHAIN_REPLACEMENT_WORK = "Заменить цепи ГРМ со звёздочками комплектом"
_CHAIN_MANUAL = "zmz-40904-40905-rk-2025"
CHAIN_SOURCE = (_CHAIN_MANUAL, 107)
_CHAIN_NOISE = re.compile(r"равномерный шум в передней части двигателя.{0,40}усилива\w* на средних оборотах", re.I)


def _page_hits(search, query, document, page, *, exact_code=None):
    hits = search.find(query)
    for hit in sorted(hits, key=lambda item: not item.get("expert_rows")):
        if hit["document_id"] == document and hit["page"] == page and search.engine in hit["engine_models"]:
            yield hit
    if exact_code:
        yield from sorted(search.exact_page(document, page, exact_code), key=lambda item: not item.get("expert_rows"))


def _pairs(hit):
    for table in hit.get("tables", ()):
        for row in table.get("rows", ()):
            if len(row) == 2 and all(isinstance(cell, str) for cell in row):
                yield clean(row[0]), clean(row[1])


def _intake(search):
    for hit in _page_hits(search, "11.6 шум в районе турбокомпрессора проверить крепление соединительных рукавов "
                          "и состояние воздухопроводов", "yamz-536-re-2016", 216, exact_code="F09"):
        for cause, remedy in _pairs(hit):
            if not re.match(r"11\.6\s+Шум в районе турбокомпрессора\b", cause, re.I):
                continue
            leak = re.search(r"б\) негерметична система впуска после турбокомпрессора\b", cause, re.I)
            inspection = re.search(r"Проверить крепление соединительных рукавов и состояние воздухопроводов\b", remedy, re.I)
            if leak and inspection:
                search.add(hit, "11.6 " + leak.group() + ". " + inspection.group(), work=_INTAKE,
                           text="Пункт 11.6, подпункт б. Возможна негерметичность системы впуска после турбокомпрессора. " + _INTAKE)
                return ""
    # Другая экспертная выдержка может описывать осмотр при том же признаке.
    # Цитируем отдельное условное предложение, сохраняя работу по п. 11.6 только за его строкой.
    for hit in search.fault_hits:
        if (hit.get("fault_code") != "F09" or not hit.get("expert_rows") or search.engine not in hit["engine_models"]
                or hit.get("tables") or hit.get("kind") == "table"):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", clean(hit["text"])):
            if re.fullmatch(r"(?:Проверить|Осмотреть) крепление турбокомпрессора при "
                            r"(?:свисте|подвывании|шуме)(?: и (?:свисте|подвывании|шуме))?[.!]?", sentence, re.I):
                search.add(hit, sentence)
                return "В найденных фрагментах нет проверки впуска по пункту 11.6."
    return "В найденных фрагментах нет проверки впуска по пункту 11.6 для шума в районе турбокомпрессора."


def _spark_plug(search):
    document = "zmz-5234-10-rk-2025"
    table_row = None
    for hit in _page_hits(search, "Зазор между электродами свечи не соответствует норме проверить зазор круглым щупом",
                          document, 54):
        for cause, remedy in _pairs(hit):
            if (cause.casefold() == "зазор между электродами свечи не соответствует норме"
                    and remedy.casefold().startswith("проверить зазор круглым щупом")):
                table_row = hit, cause + ". " + remedy
                break
        if table_row:
            break

    colour_fact = gap_fact = None
    for hit in _page_hits(search, "Проверить цвет изолятора свечи и зазор между электродами. Величина зазора, круглый щуп",
                          document, 65):
        passage = clean(hit["text"])
        colour = re.search(r"проверить цвет изолятора свечи и зазор между электродами\.\s*"
                           r"На правильно отрегулированном двигателе после длительной работы двигателя под "
                           r"нагрузкой цвет изолятора свечи должен быть светло-серым\.", passage, re.I)
        gap = re.search(r"Величина зазора между электродами свечи\s*[–—-]\s*(\d+,\d+\s*…\s*\d+,\d+ мм)\.\s*"
                        r"Зазор измеряется круглым щупом", passage, re.I)
        if colour:
            colour_fact = hit, colour.group()
        if gap:
            gap_fact = hit, gap.group()
        if colour_fact and gap_fact:
            break

    work = _PLUG if colour_fact and (table_row or gap_fact) else (_COLOUR if colour_fact else _GAP)
    if table_row:
        search.add(*table_row, work=work, text=_GAP)
    if colour_fact:
        search.add(*colour_fact, work=work, text="Проверить цвет изолятора свечи. Светло-серый цвет оценивают "
                   "после длительной работы правильно отрегулированного двигателя под нагрузкой.")
    if gap_fact:
        search.add(*gap_fact, work=work)
    return "" if search.evidence else "В найденных фрагментах нет процедуры проверки свечи и межэлектродного зазора."


def select_tensioner_source(search, *, confirmed=False):
    """Различает справку о характере стука и работу после подтверждения признаков."""
    work = "Прослушать стук у пробки крышки верхнего или нижнего гидронатяжителя стетофонендоскопом."
    explanation = ("Гидронатяжитель проверяют при стуке в зоне передней крышки головки цилиндров и крышки цепи. "
                   "Стук отчётливо слышен при резком сбросе оборотов. Его прослушивают стетофонендоскопом у пробки "
                   "крышки верхнего или нижнего гидронатяжителя. Возможны и другие причины — износ звёздочек, "
                   "повышенная вытяжка цепи или разрушение успокоителя цепи.")
    if not confirmed:
        explanation += " Вы слышите отдельный стук у крышки цепи при резком сбросе оборотов или только равномерный шум?"
    return search.passage(
        "Гидронатяжитель. Стук у крышки цепи при резком сбросе частоты вращения",
        r"Гидронатяжитель подлежит проверке и ремонту при обнаружении стука в зоне передней крышки "
        r"головки цилиндров и крышки цепи\.\s*Стук отч[её]тливо слышен при резком сбросе частоты вращения"
        r".{0,150}?стетофонендоскопа.{0,180}?к пробке крышки верхнего или нижнего гидронатяжителя"
        r".{0,500}?износ зв[её]здочек, повышенная вытяжка цепи, разрушение успокоителя цепи\.",
        explain=lambda _: explanation, work=work if confirmed else None)


def _chain_length(search, *, dialogue=False, replacement=False):
    for hit in _page_hits(search, "равномерный шум в передней части двигателя средние обороты переменные нагрузки "
                          "проверить увеличение длины цепей", _CHAIN_MANUAL, 107,
                          exact_code="F14" if dialogue else None):
        for cause, remedy in _pairs(hit):
            if not re.search(r"повышенное удлинение цепи\s*[–—-]\s*равномерный шум в передней части двигателя, "
                             r"усиливающийся при средних оборотах и переменных нагрузках", cause, re.I):
                continue
            if not remedy.casefold().startswith("проверить увеличение длины цепей"):
                continue
            can_replace = "при необходимости заменить цепи со звездочками комплектом" in remedy.casefold().replace("ё", "е")
            if replacement:
                if not can_replace:
                    continue
                search.add(hit, cause + " " + remedy, work=CHAIN_REPLACEMENT_WORK,
                           text="По результатам дефектовки подтверждена необходимость замены. " + CHAIN_REPLACEMENT_WORK + ".")
                return ""
            if dialogue:
                explanation = ("Руководство рекомендует проверить удлинение цепей при равномерном шуме спереди, "
                               "усиливающемся на средних оборотах и при переменных нагрузках. "
                               "В этой редакции руководства предельное удлинение в миллиметрах или процентах не найдено.")
                if can_replace:
                    explanation += " Замену цепей со звёздочками комплектом назначают по результатам дефектовки."
                search.add(hit, cause + " " + remedy, work=_CHAIN_INSPECTION, text=explanation)
                return ""
            after_measurement = ""
            if can_replace:
                after_measurement = " Если измерение подтвердит необходимость замены, цепи меняют со звёздочками комплектом."
            search.add(hit, cause + " " + remedy, work=_CHAIN,
                       text="В руководстве описан равномерный шум спереди, усиливающийся при средних оборотах "
                            "и переменных нагрузках. Уточните, как меняется шум под нагрузкой. " + _CHAIN + after_measurement)
            return ""
    if replacement:
        return "В найденных строках руководства нет указания о замене цепей со звёздочками комплектом."
    return "В найденных строках нет проверки длины цепей при таком шуме."


_CHAIN_PHASES = {
    135: (
        r"в результате удлинения цепей и износа зубьев зв[её]здочек возможно значительное "
        r"отклонение фаз газораспределения",
        "Удлинение цепей и износ зубьев звёздочек могут вызывать отклонение фаз газораспределения.",
    ),
    138: (
        r"втулочными цепями.{0,600}зубчатыми цепями.{0,600}Рис\.\s*104.{0,300}ВМТ такта сжатия"
        r".{0,1800}ведущая ветвь цепи.{0,100}должна быть натянута",
        "Углы первых кулачков впускного и выпускного распредвалов измеряют сектором и шаблоном "
        "в ВМТ такта сжатия первого цилиндра при натянутой ведущей ветви цепи. "
        "Номиналы впуска и выпуска по рис. 104: для втулочных цепей — 20°30′ и 19°30′, "
        "для зубчатых — 20° и 19° соответственно.",
    ),
    139: (
        r"превышают допустимые\s*±\s*2\s*°\s*40\s*[′’'\uf0a2]?\s*от номинального значения.{0,90}"
        r"корректировку установки фаз газораспределения",
        "Допуск для каждого угла составляет ±2°40′ от номинала. При выходе за допуск руководство "
        "предписывает корректировку фаз газораспределения. Этот допуск относится к углам кулачков.",
    ),
}
_CHAIN_MARKS = {
    141: (
        r"При установке зв[её]здочки на дополнительное отверстие заводская установочная метка"
        r".{0,210}не будет совпадать с верхней плоскостью головки цилиндров.{0,80}ВМТ",
        "По меткам выставляют исходное положение. Если при корректировке звёздочку установили "
        "на дополнительное отверстие, заводская метка может не совпасть с плоскостью головки цилиндров в ВМТ.",
    ),
    142: (
        r"Повернуть коленчатый вал по ходу вращения на два оборота.{0,1100}"
        r"Проверить установку фаз газораспределения с помощью сектора и шаблона кулачка 252° со стрелкой",
        "После корректировки и двух оборотов коленвала руководство предписывает снова измерить углы "
        "сектором и шаблоном. По одному смещению меток нельзя определить удлинение цепей.",
    ),
}
_CHAIN_REFERENCES = {"chain-phases": _CHAIN_PHASES, "chain-marks": _CHAIN_MARKS}
_CHAIN_QUESTION = (
    "Удалось ли проверить удлинение цепей ГРМ? Если проверяли фазы, какие углы получили на впускном "
    "и выпускном распредвалах и какие цепи установлены — втулочные или зубчатые?"
)


def _chain_fact(hit, pattern):
    # В текстовом слое оригинала отдельные слова перенесены на соседнюю строку.
    return re.search(pattern, clean(re.sub(r"(?<=\w)-[ \t]*\r?\n[ \t]*(?=\w)", "", hit["text"])), re.I)


def _missing_chain_pages(pages, evidence):
    present = {hit["page"] for hit in evidence if hit["document_id"] == _CHAIN_MANUAL}
    absent = [str(page) for page in pages if page not in present]
    return "Не удалось подтвердить сведения на страницах PDF " + ", ".join(absent) + "." if absent else ""


def complete_chain_sources(result, registry, root, engine):
    """Дополняет ответ по рисункам и продолжениям, которые могли не войти в индекс."""
    pages = _CHAIN_REFERENCES.get(result["topic"])
    if not pages or not result["missing"]:
        return
    present = {hit["page"] for hit in result["evidence"]}
    originals = verified_pdf_pages(registry, root, _CHAIN_MANUAL, engine, tuple(page for page in pages if page not in present))
    for page, hit in originals.items():
        pattern, answer = pages[page]
        if anchor := _chain_fact(hit, pattern):
            result["evidence"].append(dict(hit, excerpt=anchor.group(), text=answer, kind="fact", work=None,
                                           applicability=[f"{engine}|all"]))
    result["evidence"].sort(key=lambda hit: hit["page"])
    result["missing"] = _missing_chain_pages(pages, result["evidence"])


def chain_defect_inspection(question):
    """Узнаёт сообщённый механиком вывод о необходимости замены цепей."""
    q = question.casefold()
    if "?" in q or re.search(r"\b(?:если|может|возможно|похоже|надо ли|кажется)\b|по (?:меткам|фазам)", q):
        return None
    chain = r"\bцеп(?:ь|и|ей|ям|ями|ях)\b(?:\s+(?:грм|привода грм))?"
    for statement in re.split(r"[.!;\n]", q):
        if not (re.search(r"дефектовк", statement) and re.search(chain, statement) and re.search(r"\bзамен", statement)):
            continue
        if re.search(r"\bне\s+(?:подтвер|треб|нуж|установ)", statement):
            return False
        if re.search(rf"{chain}\s+требу\w* замены|"
                     rf"\bподтвер\w*\s+необходимость замены\s+{chain}|"
                     rf"\bзамена\s+{chain}\s+(?:требуется|необходима|подтверждена)", statement):
            return True
    return None


def chain_observation_question(question):
    return chain_defect_inspection(question) is not None


def select_chain_dialogue(search, question, symptoms, diagnostic, topic):
    """Ведёт сценарий проверки цепей для подтверждённого признака S-0152."""
    if not _CHAIN_NOISE.search(symptoms):
        return None
    finding = chain_defect_inspection(question)
    if finding is True or (topic == CHAIN_REPLACEMENT_TOPIC
                           and question.strip(" .!?") == CHAIN_REPLACEMENT_WORK.casefold()):
        return CHAIN_REPLACEMENT_TOPIC, _chain_length(search, dialogue=True, replacement=True), ""
    if finding is False:
        return CHAIN_INSPECTION_TOPIC, _chain_length(search, dialogue=True), _CHAIN_QUESTION
    if "гидронатяжител" in question:
        return None
    related = topic in (*_CHAIN_REFERENCES, CHAIN_INSPECTION_TOPIC, CHAIN_REPLACEMENT_TOPIC, "chain-noise", "chain-tensioner")
    if re.search(r"\bметк\w*\b", question) and (related or re.search(r"грм|распредвал|зв[её]здоч|цеп", question)):
        branch = "chain-marks"
    elif re.search(r"\bфаз\w*\b", question) or (related and re.search(
            r"впуск\w*.{0,40}\d+\s*°|выпуск\w*.{0,40}\d+\s*°|втулочн\w*|зубчат\w*", question)):
        branch = "chain-phases"
    elif (diagnostic or question.strip(" .!?") == _CHAIN_INSPECTION.casefold()
          or re.search(r"(?:провер\w*|измер\w*)\s+(?:вытяжк\w*|удлинени\w*)\s+цеп", question)
          or related and re.search(r"\b(?:не проверяли|не измеряли|не замеряли|проверку не проводили|не удалось проверить)\b",
                                   question)):
        return CHAIN_INSPECTION_TOPIC, _chain_length(search, dialogue=True), _CHAIN_QUESTION
    else:
        return None
    pages = _CHAIN_REFERENCES[branch]
    for page, (pattern, answer) in pages.items():
        for hit in search.exact_page(_CHAIN_MANUAL, page, "F14"):
            if anchor := _chain_fact(hit, pattern):
                search.add(hit, anchor.group(), text=answer)
                break
    return branch, _missing_chain_pages(pages, search.evidence), ""


def chain_knock_confirmed(question):
    """Для проверки нужны локализация стука и подтверждённый режим его появления."""
    if "?" in question or re.search(r"\b(?:может быть|возможно|кажется|если|или)\b", question):
        return False
    clauses = re.split(r"[,;.!?]|\bно\b", question)
    affirmative = " ".join(clause for clause in clauses if not re.search(r"\b(?:не|нет|без)\b", clause))
    return bool(re.search(r"\bкрышк\w*\s+(?:привода\s+)?цепи\b|передн\w*\s+крышк\w*\s+(?:головк\w*|гбц)", affirmative)
                and re.search(r"\bрезк\w*\s+(?:сброс\w*|снижен\w*)\s+(?:оборот\w*|частот\w*\s+вращен\w*)", affirmative))


def select_diagnostic_work(search, code, question, symptoms, *, diagnostic, topic=""):
    """Показывает проверку, если совпадают узел, признаки и строка о способе проверки."""
    q = question.casefold().strip(" .!?")
    symptoms = symptoms.casefold().replace("ё", "е")
    if code == "F09" and search.engine == "ЯМЗ-536":
        relevant = re.search(r"как провер\w* (?:систему наддува|турбокомпрессор)", q) or q == _INTAKE.casefold().rstrip(".")
        if diagnostic or relevant:
            if "турбокомпрессор" in symptoms and re.search(r"свист|подвыва|шум", symptoms):
                return "turbo-intake", _intake(search)
    if code == "F12" and search.engine == "ЗМЗ-5234.10":
        relevant = q in {_GAP.casefold().rstrip("."), _PLUG.casefold().rstrip("."), _COLOUR.casefold().rstrip(".")} or re.search(
            r"как провер\w* (?:систему зажигания|зажигание|свеч\w*)", q)
        if (diagnostic or relevant) and re.search(r"пропуск\w*(?: воспламенения| зажигания)? в одном цилиндре", symptoms):
            return "spark-plug-inspection", _spark_plug(search)
    if code == "F14" and search.engine in {"ЗМЗ-40904.10", "ЗМЗ-40905.10"}:
        relevant = q == _CHAIN.casefold().rstrip(".") or re.search(r"как провер\w* (?:цеп\w*|привод грм)", q)
        tensioner = "гидронатяжител" in q and re.search(r"провер|посмотр|услови|стук", q)
        asks_chain = bool(re.search(r"\bцеп[а-я]*\b|привод грм", q))
        if topic == "chain-tensioner" and not tensioner and "?" not in question:
            no_knock = re.search(r"стука\s+нет|нет\s+(?:отдельного\s+)?стука|без\s+стука|стук\w*\s+не\s+слыш", q)
            if no_knock and "равномерн" in q and "шум" in q:
                return "chain-noise", _chain_length(search)
            if not no_knock and re.search(r"\bстук\b", q) and "крышк" in q and "сброс" in q:
                found = select_tensioner_source(search, confirmed=chain_knock_confirmed(question.casefold()))
                return "chain-tensioner", "" if found else "В найденных фрагментах нет условий проверки гидронатяжителя."
        if diagnostic or relevant or tensioner:
            missing = []
            if not tensioner or asks_chain:
                if _CHAIN_NOISE.search(symptoms):
                    missing.append(_chain_length(search))
                else:
                    missing.append("Уточните, равномерный ли шум и усиливается ли он спереди двигателя на средних оборотах.")
            if tensioner and not select_tensioner_source(search):
                missing.append("В найденных фрагментах нет условий проверки гидронатяжителя.")
            return "chain-tensioner" if tensioner else "chain-noise", " ".join(part for part in missing if part)
    return None

