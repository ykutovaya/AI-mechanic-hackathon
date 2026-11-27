"""Находит в одном пункте руководства действие, его предмет и условия выполнения."""

import re

from py_rust_stemmers import SnowballStemmer

from .qdrant_chunks import clean


_STEMMER = SnowballStemmer("russian")
_WORD = r"[а-яё]+(?:[-‑][а-яё]+)*"
_INSTALL = r"(?:установк[а-яё]*|монтаж[а-яё]*)"
_VERSION = re.compile(r"\b(?:стар|нов)[а-яё]*\s+образца\b", re.I)
_GROUP = re.compile(r"\b(?:груб|тонк)[а-яё]*\s+очистки\b", re.I)
_FIX = re.compile(rf"\b(?:прихватить|закрепить|удержать|зафиксировать)\s+({_WORD})\b")
_SEAL = re.compile(rf"\b(?:стык|соединение)\s+({_WORD})\s+(?:с|со|и)\s+({_WORD})\b")
_SUBJECT = re.compile(rf"\b(?:перед|при|до)\s+{_INSTALL}\s+({_WORD})(?:\s+({_WORD}\s+{_WORD}))?")
_MARKERS = re.compile(r"(?m)(?:^[ \t]*[–—-][ \t]+|^[ \t]*\d+[.)][ \t]+|\n[ \t]*\n)")
_SENTENCES = re.compile(r"(?<=[.!?;])\s+(?=[А-ЯЁ])")


def _terms(text):
    return {_STEMMER.stem_word(w) for w in re.findall(r"[а-я]+", text.lower().replace("ё", "е"))}


def _joint_terms(text):
    # Обычные падежные окончания у составных названий: стакана-отстойника / стаканом-отстойником.
    return {re.sub(r"(?:ами|ями|ом|ем|ам|ям|ах|ях|ов|ев|а|я|у|ю|е|ы|и)$", "", word)
            for word in re.findall(r"[а-яё]+", text.casefold()) if len(word) > 2}


def _sentences(text):
    # Граница нумерованного пункта или маркера отделяет операции над разными деталями.
    for step in _MARKERS.split(text):
        yield from (s for s in _SENTENCES.split(clean(step)) if s)


def _variant_fits(question, context):
    for pattern in (_VERSION, _GROUP):
        for match in pattern.finditer(question):
            if not _terms(match[0]) <= _terms(context):
                return False
    return True


def _sealing_context(search, hit, question, context):
    construction = re.search(r"\b(?:не)?разборн[а-яё]+", question)
    if construction and not _terms(construction[0]) <= _terms(context):
        return False
    if _variant_fits(question, context):
        return True
    if not construction or _GROUP.search(context) or _VERSION.search(question):
        return False
    # Вид фильтра иногда указан перед процедурой. Подтверждение должно называть
    # ту же конструкцию в том же разделе, на том же листе и при том же условии.
    key = ("document_id", "page", "section", "condition")
    for neighbour in search.primary:
        if all(neighbour.get(k) == hit.get(k) for k in key):
            for sentence in _sentences(neighbour["text"]):
                if _terms(construction[0]) <= _terms(sentence) and _variant_fits(question, sentence):
                    return True
    return False


def _symptom_and_tool(search, question):
    subject = re.search(r"\b(?:негерметичност[ьи]|теч[ьи])\s+(.+?)\s+и\s+", question)
    if not subject:
        return False
    components = _terms(_VERSION.sub("", subject[1]))
    for hit in search.primary:
        if hit.get("kind", "text") not in ("text", "procedure"):
            continue
        # Учитываем подзаголовки внутри фрагмента, даже если раздел в метаданных более общий.
        for block in re.split(r"(?m)(?=^[^\n.!;:]{3,100}\b(?:старого|нового) образца[ \t]*$)", hit["text"]):
            if not block.strip() or not _variant_fits(question, block + " " + hit.get("section", "")):
                continue
            lines = block.splitlines()
            if lines and _VERSION.search(lines[0]) and not lines[0].rstrip().endswith("."):
                lines = lines[1:]
            sentences = list(_sentences("\n".join(lines)))
            symptom = next((s for s in sentences if components <= _terms(s)
                            and re.search(r"\b(?:обнаруж|проявля|выявля|определя)", s, re.I)), None)
            if not symptom:
                continue
            for i, sentence in enumerate(sentences):
                if not re.search(r"\bразб(?:ор|ир)|\bразобра", sentence, re.I):
                    continue
                tool = sentence
                if i + 1 < len(sentences) and re.match(
                        r"(?:необходимо|следует|нужно|рекомендуется)\s+(?:использовать|применять)\b",
                        sentences[i + 1], re.I):
                    tool += " " + sentences[i + 1]
                if not re.search(r"(?:использовать|применять|при помощи|с помощью)\s+\w+", tool, re.I):
                    continue
                search.add(hit, symptom)
                search.add(hit, tool)
                return True
    return False


def select_workshop_instruction(search, question):
    """Выбирает исходное предложение только при совпадении предмета и вида работы."""
    question = question.casefold()
    if "инструмент" in question and re.search(r"признак|обнаруж|определ", question):
        return _symptom_and_tool(search, question)
    seal = _SEAL.search(question) if re.search(r"\bуплотн|герметизир", question) else None
    fitting = _SUBJECT.search(question) if "смаз" in question else None
    fixing = _FIX.search(question) if "сторон" in question else None
    if not (seal or fitting or fixing):
        return False

    if seal:
        required = _joint_terms(seal[1] + " " + seal[2])
        relation = re.compile(r"\bмежду\b", re.I)
        operation = re.compile(r"\b(?:прокладк|шайб|манжет|кольц|герметик|уплотнител)", re.I)
    elif fitting:
        required = _terms(fitting[1])
        relation = re.compile(r"\b(?:предварительно|перед|до\s+установ|при\s+установ)", re.I)
        operation = re.compile(r"\bсмаз", re.I)
    else:
        required = _terms(fixing[1])
        relation = re.compile(r"\b(?:со?\s+стороны|снаружи|изнутри|сверху|снизу)", re.I)
        operation = re.compile(r"\b(?:смаз|нанес|креп|фиксир|прикле|прижа|приподж|прихват)", re.I)

    for hit in search.primary:
        if hit.get("kind", "text") not in ("text", "procedure"):
            continue
        context = hit.get("section", "") + " " + clean(hit["text"])
        if seal:
            if not _sealing_context(search, hit, question, context):
                continue
        elif not _variant_fits(question, context):
            continue
        if fitting and fitting[2] and not _terms(fitting[2]) <= _terms(context):
            continue
        for sentence in _sentences(hit["text"]):
            words = _joint_terms(sentence) if seal else _terms(sentence)
            if required <= words and relation.search(sentence) and operation.search(sentence):
                search.add(hit, sentence)
                return True
    return False


def installation_torque(search, question):
    """Связывает момент с пронумерованным крепежом внутри одного шага установки."""
    match = re.search(r"\bпри установке\s+([^?,]{3,60}?)\s*,?\s+(?:какой|каков|с каким)\s+момент"
                      r"(?:ом)?\s+затяжки\s+(болтов|гаек|винтов)\s+(.+?)[?!.]*$", question, re.I)
    if not match:
        return None
    assembly, fastener, subject = match.groups()
    def part_names(text):
        return {word.replace("патрубок", "патрубк") for word in _terms(text)}

    required = part_names(subject) - {"на", "к", "при", "в"}
    part = {"болтов": "болт", "гаек": "гайк", "винтов": "винт"}[fastener.lower()]
    number = re.compile(rf"\b{part}[а-я]*\s*\((\d+)\)", re.I)
    unit = re.compile(r"\b\d+(?:[.,]\d+)?(?:\s*[-–—…]\s*\d+(?:[.,]\d+)?)?\s*Н\s*[·⋅.]?\s*м\b", re.I)
    matches = {}
    for hit in search.primary:
        if not _terms(assembly) <= _terms(hit.get("section", "")):
            continue
        for step in re.split(r"(?m)^\s*\d+[.)]\s+", hit["text"]):
            sentences = _SENTENCES.split(clean(step))
            for index, sentence in enumerate(sentences):
                tags = set(number.findall(sentence))
                if len(tags) != 1 or not required <= part_names(sentence):
                    continue
                for end in range(index, min(index + 3, len(sentences))):
                    window = " ".join(sentences[index:end + 1])
                    if set(number.findall(window)) != tags:
                        break
                    # При смене крепежа отсчёт момента для первого болта заканчивается.
                    if end > index and re.search(r"\b(?:болт|гайк|винт)[а-я]*\b", sentences[end], re.I):
                        if not tags.intersection(number.findall(sentences[end])):
                            break
                    values = unit.findall(window)
                    if len(values) == 1 and re.search(r"затя(?:н|ж)|завернуть", window, re.I):
                        matches.setdefault(clean(values[0]), (hit, window))
                        break
    if len(matches) == 1:
        search.add(*next(iter(matches.values())))
        return ""
    return "В найденных шагах установки не удалось однозначно связать момент затяжки с указанным крепежом."
