"""Подбирает численную норму по обозначению детали или объектам измерения."""

import re

from py_rust_stemmers import SnowballStemmer

from .qdrant_chunks import clean


_STEMMER = SnowballStemmer("russian")
_PART_NUMBER = re.compile(r"(?<![\w-])\d{2,}(?:\.\d{2,})+(?:[-–]\d+)?(?![\w]|[.–-]\d)")
_TORQUE = re.compile(r"\d\s*[нh]\s*[·∙⋅*.]?\s*[мm]\b", re.I)
_ANGULAR_STEP = re.compile(r"^(?:(?:затем|далее|после (?:этого|чего))\s+)?довернуть\s+"
                           r"(?P<subject>[а-яё\s]*?)\bна\s+(?:угол\s+)?\d+(?:[,.]\d+)?\s*[°º]", re.I)
_LIMIT = re.compile(r"^сопротивление\s+(?:(?:должно|может)\s+быть\s+)?"
                    r"(?:не (?:более|менее|выше|ниже)|не превышает)\s+\d+(?:[,.]\d+)?\s*[км]?ом\b", re.I)


def _sentences(text):
    return re.split(r"(?<=[.!?])\s+(?=[А-ЯЁ])", clean(text))


def _terms(text):
    return {_STEMMER.stem_word(word) for word in re.findall(r"[а-яё]{3,}", text.casefold())}


def _torque_excerpts(hit, part):
    sentences = _sentences(hit["text"])
    for index, sentence in enumerate(sentences):
        numbers = [match.replace("–", "-") for match in _PART_NUMBER.findall(sentence)]
        if numbers != [part] or not _TORQUE.search(sentence):
            continue
        following = sentences[index + 1] if index + 1 < len(sentences) else ""
        steps = [sentence]
        angle = _ANGULAR_STEP.match(following)
        if angle and _terms(angle["subject"]) <= _terms(sentence):
            steps.append(following)
        warning = sentences[index + len(steps):index + len(steps) + 2]
        if (len(warning) == 2 and warning[0].casefold() == "внимание!"
                and [n.replace("–", "-") for n in _PART_NUMBER.findall(warning[1])] == [part]):
            steps.extend(warning)
        yield " ".join(steps)


def _resistance_excerpts(hit, subjects):
    sentences = _sentences(hit["text"])
    section = _terms(hit.get("section", ""))
    first_line = hit["text"].strip().partition("\n")[0].strip()
    if len(first_line) <= 100 and re.match(r"^(?:осмотр|контроль|проверка|испытание)\b", first_line, re.I):
        section.update(_terms(first_line))
    for probe, reading in zip(sentences, sentences[1:]):
        if not _LIMIT.match(reading) or "сопротивлен" not in probe.casefold():
            continue
        names = _terms(probe)
        if len(subjects & names) >= 2 and subjects <= names | section:
            yield probe + " " + reading


def select_component_limit(search, question):
    """Требует совпадения обозначения или названных деталей с условиями нормы."""
    parts = _PART_NUMBER.findall(question)
    if len(parts) == 1 and re.search(r"момент|затя(?:ж|ну)", question):
        part = parts[0].replace("–", "-")
        extract = lambda hit: _torque_excerpts(hit, part)
        failure = "В руководстве не найден однозначный момент затяжки для указанного обозначения."
    elif "омметр" in question and "сопротивлен" in question:
        objects = re.search(r"\bпроверить\s+([^?.!;]+)", question)
        if not objects:
            return None
        subjects = _terms(objects[1]) - _terms("с помощью омметра сопротивление")
        if len(subjects) < 2:
            return None
        extract = lambda hit: _resistance_excerpts(hit, subjects)
        failure = "В руководстве не найден предел сопротивления для указанных деталей."
    else:
        return None

    matches = {}
    for hit in search.primary:
        for excerpt in extract(hit):
            matches[(hit["document_id"], hit["page"], hit.get("condition"), excerpt)] = hit
    if len(matches) != 1:
        return failure
    (_, _, _, excerpt), hit = next(iter(matches.items()))
    search.add(hit, excerpt)
    return ""
