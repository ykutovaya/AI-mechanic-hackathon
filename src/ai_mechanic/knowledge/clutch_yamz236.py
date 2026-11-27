"""Уточняет шум сцепления ЯМЗ-236М2 и подбирает работу по установленному узлу."""

from dataclasses import dataclass, replace
import re

from .expert_answers import generic_diagnostic_question
from .generator_gearbox import confirmed_gearbox_model, gearbox_observation_question, gearbox_work, select_gearbox_inspection
from .sources import verified_pdf_pages


CLUTCH_CHECK_ID = "S-0024"
CLUTCH_TOPIC = "yamz236-clutch"
_MANUAL = "yamz-236m2-re-2010"
_FREE_PLAY = "Проверить свободный ход муфты выключения сцепления"
_DRIVE_CHECK = "Проверить работу сцепления и регулировку привода выключения"
_REPLACE_BEARING = "Заменить муфту выключения сцепления с подшипником"
_MODEL_PROMPT = ("Какое сцепление установлено — ЯМЗ-236К, ЯМЗ-181 или другое? "
                 "Перепишите обозначение с маркировки или из документации машины.")
_PEDAL_PROMPT = ("На холостом ходу шум слышен при отпущенной педали? Что происходит при нажатии и при полностью "
                 "выжатой педали — шум появляется, усиливается, ослабевает, исчезает или остаётся прежним?")
_MODEL = re.compile(r"(?<!\w)(?:ямз[- ]*)?(236[кk]|181)(?:-\d+)?(?![\w-])")
_PEDAL = re.compile(r"при\s+(?:полностью\s+)?(?:нажати\w*|нажат\w*|выжат\w*|выжим\w*)")
_SHORT_PEDAL_OUTCOME = re.compile(
    r"(?:(?:шум|гул|стук|звук)\s+)?(?:появляется|возникает|усилива?ется|громче|"
    r"ослабевает|затихает|тише|исчезает|пропадает|остается прежним|"
    r"не меняется|без изменений|сохраняется|не исчезает|не пропадает)")
_UNKNOWN = re.compile(r"не (?:зна\w*|увер\w*|помн\w*|провер\w*|свер\w*|уточн\w*|наблюда\w*)|неизвестн|не подтвержд")
_UNCERTAIN = re.compile(r"\b(?:если|или|кажется|вроде|возможно|может быть|наверное|предположим|допустим|напиши|ответь)\b")
_NOISE = re.compile(r"\b(?:шум|гул|стук|звук)\w*\b")
_GEARBOX_LOCATION = re.compile(
    r"(?:шум|гул|стук|источник).{0,60}(?:в|из|от)\s+(?:коробк\w*(?:\s+передач)?|кпп?)\b")
_CLUTCH_LOCATION = re.compile(
    r"(?:шум|гул|стук|источник).{0,60}(?:в|из|от)\s+(?:картер\w*\s+)?сцеплени\w*\b")


@dataclass(frozen=True)
class ClutchObservation:
    """Хранит подтверждения механика внутри текущего диалога по проверке."""

    model: str = ""
    pedal: str = ""
    gearbox: str = ""
    worn_bearing: bool = False
    noise_after_lubrication: bool = False

    @classmethod
    def read(cls, topic):
        family, *fields = topic.split(":")
        if family != CLUTCH_TOPIC or len(fields) != 5:
            return cls()
        model, pedal, gearbox, wear, noise = fields
        return cls(model, pedal, gearbox, wear == "1", noise == "1")

    def topic(self):
        return ":".join((CLUTCH_TOPIC, self.model, self.pedal, self.gearbox,
                         str(int(self.worn_bearing)), str(int(self.noise_after_lubrication))))

    def proposed_work(self):
        if self.gearbox:
            return (gearbox_work(self.gearbox), (135,)) if self.gearbox != "unknown" else ("", ())
        if not self.pedal:
            return "", ()
        if self.model == "236К":
            return _FREE_PLAY, (63,)
        if self.model == "181":
            if self.worn_bearing and self.noise_after_lubrication:
                return _REPLACE_BEARING, (125,)
            return _DRIVE_CHECK, (120, 121)
        return "", ()


def _statements(question):
    text = question.casefold().translate(str.maketrans("ё‑–", "е--"))
    for statement in re.findall(r"[^.!?;\n]+\??", text):
        line = statement.strip(" ,")
        if line and not line.endswith("?") and not _UNCERTAIN.search(line):
            yield line


def _clutch_model(line):
    models = set(_MODEL.findall(line))
    if "сцеплен" in line and _UNKNOWN.search(line) and not (_PEDAL.search(line) or "реакци" in line):
        return ""
    label = re.sub(r"^(?:это|установлен[ао]?|стоит)\s+", "", line)
    if len(models) == 1 and (_MODEL.fullmatch(label) or re.search(r"сцеплен|маркировк|табличк|паспорт|документаци", line)):
        if not re.search(r"\b(?:не|сняли|раньше|прежнее|старое)\b", line):
            return models.pop().replace("k", "к").upper()
        return ""
    if line in {"другое", "другая модель"} or re.search(r"(?:сцеплен\w* [^,]{0,30}(?:другой модели|другое|сакс|sachs|valeo))", line):
        return ""
    return None


def _pedal_observation(line):
    if re.search(r"педал\w*\s+(?:газ|тормоз|акселератор)|"
                 r"при нажати\w*\s+(?:на\s+)?(?:газ|тормоз|акселератор|кнопк)", line):
        return None
    if ("педал" in line or "сцеплен" in line) and _UNKNOWN.search(line) and re.search(
            r"реакци|педал|меня", line):
        return ""
    pressed = _PEDAL.search(line)
    if pressed:
        before = re.split(r"[,;]|\s+(?:а|но)\s+", line[:pressed.start()])[-1]
        after = re.split(r"[,;]|\s+(?:а|но)\s+", line[pressed.end():], maxsplit=1)[0]
        phrases = (after, before)
    elif _SHORT_PEDAL_OUTCOME.fullmatch(line):
        phrases = (line,)
    else:
        return None
    for phrase in phrases:
        if re.search(r"вибрац|запах|нагрев|температур|толч|включен", phrase) and not _NOISE.search(phrase):
            continue
        if re.search(r"не (?:меня|измен)|без изменен|остае?тся прежн", phrase):
            return "без изменений"
        if re.search(r"не (?:исчез|пропада)|сохраня", phrase):
            return "сохраняется"
        for reaction, pattern in (("исчезает", r"исчез|пропа|затиха|не слыш|стих"),
                                  ("ослабевает", r"ослаб|тише|уменьша"),
                                  ("усиливается", r"усил|громче"),
                                  ("появляется", r"появ|возника")):
            if re.search(pattern, phrase) and not re.search(rf"\bне\s+(?:\w+\s+)?(?:{pattern})", phrase):
                return reaction
    return None

def _apply_reports(state, question):
    statements = list(_statements(question))
    short_reactions = {_pedal_observation(line) for line in statements if _SHORT_PEDAL_OUTCOME.fullmatch(line)}
    conflicting_reactions = len(short_reactions) > 1
    if conflicting_reactions:
        state = replace(state, pedal="")
    for line in statements:
        model = _clutch_model(line)
        if model is not None and model != state.model:
            keep_bearing = not state.model and not state.gearbox
            state = replace(state, model=model, worn_bearing=state.worn_bearing and keep_bearing,
                            noise_after_lubrication=state.noise_after_lubrication and keep_bearing)
        pedal = _pedal_observation(line)
        if pedal is not None and not (conflicting_reactions and _SHORT_PEDAL_OUTCOME.fullmatch(line)):
            state = replace(state, pedal=pedal)
        clauses = re.split(r"[,;]|\s+а\s+", line)
        if any(_GEARBOX_LOCATION.search(part) and not re.search(r"\bне\b", part) for part in clauses):
            if not state.gearbox:
                state = replace(state, gearbox="unknown", worn_bearing=False, noise_after_lubrication=False)
        elif (any(_CLUTCH_LOCATION.search(part) and not re.search(r"\bне\b", part) for part in clauses)
              or re.search(r"шум не (?:в|из|от) коробк", line)):
            state = replace(state, gearbox="")
        if state.gearbox and not _GEARBOX_LOCATION.search(line) and not re.search(r"сцеплен|педал|смазк", line):
            if (gearbox_model := confirmed_gearbox_model(line)) or gearbox_observation_question(line):
                state = replace(state, gearbox=gearbox_model or "unknown")
        if re.search(r"подшипник\w*.{0,35}муфт\w*\s+выключени", line):
            if re.search(r"\bне\s+(?:подтверд|обнаруж|выяв)|\bисправен\b|износ.{0,70}отсутству", line):
                state = replace(state, worn_bearing=False)
            elif re.search(r"износ|неисправ", line) and re.search(r"подтверд|обнаруж|выяв", line):
                state = replace(state, worn_bearing=True)
        lubrication = re.search(r"после смазки (?:муфт\w*|подшипник\w*)", line)
        if lubrication and _NOISE.search(line):
            if re.search(r"не (?:исчез|ушел|пропал|устранил)|не измен", line):
                state = replace(state, noise_after_lubrication=True)
            elif re.search(r"не (?:сохран|остал)|исчез|ушел|пропал|устранил", line):
                state = replace(state, noise_after_lubrication=False)
            elif re.search(r"сохран|остал", line):
                state = replace(state, noise_after_lubrication=True)
    return state


def clutch_observation_report(question):
    """Отличает факты о шуме и маркировке от запроса или гипотезы."""
    return any(
        _clutch_model(line) is not None or _pedal_observation(line) is not None
        or _GEARBOX_LOCATION.search(line) or _CLUTCH_LOCATION.search(line)
        or re.search(r"шум не (?:в|из|от) коробк|подшипник\w*.{0,35}муфт\w*\s+выключени|"
                     r"после смазки (?:муфт|подшипник)", line)
        or gearbox_observation_question(line) and re.search(r"коробк|кпп?|табличк|документац|паспорт|zf|236", line)
        for line in _statements(question)
    )


def clutch_overview_question(question):
    q = question.casefold()
    return bool(generic_diagnostic_question(q) or re.fullmatch(r"как проверить сцепление[?!. ]*", q))


def select_yamz_clutch(search, question, *, diagnostic, topic):
    """Возвращает вопрос о недостающем факте или применимую проверку."""
    active = topic.partition(":")[0] == CLUTCH_TOPIC
    state = ClutchObservation.read(topic)
    work = state.proposed_work()[0]
    question_work = work and question.strip(" .").casefold() == work.rstrip(" .").casefold()
    if not (diagnostic or active and (clutch_observation_report(question) or question_work or
            clutch_overview_question(question))):
        return None
    if not diagnostic:
        state = _apply_reports(state, question)
    if state.gearbox:
        _, missing, prompt, clarify = select_gearbox_inspection(search, None if state.gearbox == "unknown" else state.gearbox)
        return state.topic(), missing, prompt, clarify
    prompts = []
    if not state.model:
        prompts.append(_MODEL_PROMPT)
    if not state.pedal:
        prompts.append(_PEDAL_PROMPT)
    if prompts:
        return state.topic(), "", " ".join(prompts), True
    reaction = "остаётся прежним" if state.pedal == "без изменений" else state.pedal
    prompts.append(f"По вашему наблюдению, при нажатии педали шум {reaction}.")
    if state.model == "181" and state.worn_bearing and not state.noise_after_lubrication:
        prompts.append("Сохранился ли шум после смазки муфты выключения сцепления?")
    if state.model == "236К" and state.worn_bearing:
        prompts.append("Для замены подшипника нужна применимая процедура для ЯМЗ-236К. Руководство подтверждает проверку свободного хода.")
    return state.topic(), "", " ".join(prompts), False


_REFERENCE = {
    63: (r"двухдискового сцепления.{0,920}между упорным кольцом и подшипником муфты\s*"
         r"\(3,2\s*[-–…]\s*4,\s*0\s*мм\)",
         "Проверьте свободный ход муфты. Зазор между упорным кольцом и подшипником муфты — 3,2–4,0 мм. "
         "Порядок регулировки приведён в руководстве машины."),
    120: (r"проверка работы сцепления на транспортном средстве включает проверку на наличие пробуксовки,"
          r".{0,30}ведения.{0,40}посторонних шумов и стуков",
          "Проверьте работу сцепления на машине. Отметьте посторонний шум, ведение и пробуксовку."),
    121: (r"контролировать правильность регулировки привода сцепления.{0,215}руководством по эксплуатации транспортного средства",
          "Проверьте регулировку привода выключения сцепления по руководству машины."),
    122: (r"проверка качества установки сцепления.{0,250}на неработающем двигателе.{0,340}"
          r"перемещению муфты на 11\s*(?:\.{3}|…|[-–])\s*13\s*мм",
          "11–13 мм — рабочее перемещение муфты при проверке качества установки на остановленном двигателе."),
    125: (r"отсутствие смазки в подшипнике.{0,110}муфты выключения или его износ"
          r".{0,340}шум не устранился\s*[-–—]\s*замените муфту с подшипником",
          "Для ЯМЗ-181 подтверждены износ подшипника и сохранение шума после смазки. "
          "Руководство предусматривает замену муфты выключения сцепления в сборе с подшипником."),
}


def complete_clutch_sources(result, registry, root, engine):
    """Сверяет полную процедуру по физическим страницам проверенной редакции РЭ."""
    state = ClutchObservation.read(result["topic"])
    work, required = state.proposed_work()
    if not work or state.gearbox:
        return
    pages = (*required, 122) if work == _DRIVE_CHECK else required
    originals = verified_pdf_pages(registry, root, _MANUAL, engine, pages)
    evidence = {}
    for page, hit in originals.items():
        pattern, description = _REFERENCE[page]
        text = " ".join(re.sub(r"(?<=\w)-[ \t]*\r?\n\s*(?=\w)", "", hit["text"]).split())
        text = text.replace("зазоро м", "зазором")
        if anchor := re.search(pattern, text, re.I):
            evidence[page] = dict(hit, excerpt=anchor.group(), text=description, kind="fact", work=None,
                                  applicability=[f"{engine}|all"])
    missing = set(required) - evidence.keys()
    if missing:
        refs = ", ".join(map(str, sorted(missing)))
        result["missing"] = f"Не удалось подтвердить применимую проверку на страницах PDF {refs}."
    else:
        for page in required:
            evidence[page]["work"] = work
    result["evidence"].extend(evidence.values())


def clutch_order_basis(topic, works, cited):
    """Допускает черновик только по наблюдению и страницам, подтверждающим выбранную работу."""
    if topic.partition(":")[0] != CLUTCH_TOPIC:
        return False
    work, pages = ClutchObservation.read(topic).proposed_work()
    return bool(work and works == [work] and all((_MANUAL, page) in cited for page in pages))
