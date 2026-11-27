"""Ищет в индексе руководства применимые к вопросу факты и процедуры."""

from decimal import Decimal
from functools import lru_cache
import os
import re
import unicodedata

from qdrant_client.http.exceptions import ApiException

from .expert_answers import generic_diagnostic_question, select_fault_evidence
from .figure_legends import select_figure_positions
from .normative_tables import select_normative_rows
from .oil_maintenance import select_oil_maintenance
from .defect_card import select_defect_card
from .component_limits import select_component_limit
from .workshop_instructions import installation_torque, select_workshop_instruction
from .qdrant_chunks import extract_span
from ._qdrant_store import COLLECTION, URL, QdrantSearch
from .search_queries import FUEL_SYSTEM, OIL_LEVEL, OIL_SIGNAL, mentions_pressure_drop


class SearchUnavailable(RuntimeError):
    """Сервер поиска или локальные модели недоступны."""


@lru_cache(maxsize=4)
def configured_search(url=None, collection=None, mode="hybrid"):
    """Возвращает общий клиент для запросов чата и CLI."""
    return QdrantSearch(url=url or os.getenv("QDRANT_URL") or URL,
                        collection=collection or os.getenv("QDRANT_COLLECTION") or COLLECTION,
                        mode=mode)


def add_search_arguments(parser):
    """Задаёт одинаковые параметры поиска для команд CLI и чата."""
    parser.add_argument("--qdrant-url", default=os.getenv("QDRANT_URL") or URL)
    parser.add_argument("--collection", default=os.getenv("QDRANT_COLLECTION") or COLLECTION)
    parser.add_argument("--search-mode", choices=("dense", "hybrid"), default="hybrid")


def _clean(text):
    """Соединяет перенесённые слова и строки PDF."""
    return " ".join(re.sub(r"(?<=\w)-[ \t]*\n\s*(?=\w)", "", text).split())


class _Search:
    def __init__(self, backend, engine, variant, primary, fault_code=None):
        self.backend = backend
        self.engine = engine
        self.variant = variant
        self.queries = []
        self.evidence = []
        self.results = {}
        self.manual_type = None
        if re.search(r"руководств\w* по эксплуатации|\bпо РЭ\b", primary, re.I):
            self.manual_type = re.compile(r"эксплуатаци|\bРЭ\b", re.I)
        elif re.search(r"руководств\w* по ремонту|\bпо РК\b", primary, re.I):
            self.manual_type = re.compile(r"ремонт|\bРК\b", re.I)
        self.primary = self._lookup(primary)
        self.fault_hits = []
        if fault_code and callable(getattr(type(backend), "search_fault", None)):
            self.queries.append(f"{fault_code}: {primary}")
            try:
                hits = backend.search_fault(engine, primary, code=fault_code, variant=variant, limit=24)
            except (ApiException, OSError, ValueError, RuntimeError) as exc:
                raise SearchUnavailable("Поиск по экспертным ссылкам недоступен. Проверьте Qdrant.") from exc
            self.fault_hits = [h for h in hits if not self.manual_type or self.manual_type.search(h.get("title", ""))]

    def _lookup(self, query):
        if query not in self.results:
            self.queries.append(query)
            try:
                hits = self.backend.search(self.engine, query, variant=self.variant, limit=12)
            except (ApiException, OSError, ValueError, RuntimeError) as exc:
                raise SearchUnavailable("Поиск по руководствам недоступен. Проверьте Qdrant, коллекцию и локальные модели.") from exc
            if self.manual_type:
                hits = [hit for hit in hits if self.manual_type.search(hit.get("title", ""))]
            self.results[query] = hits
        return self.results[query]

    def find(self, query):
        # Общий вопрос сохраняет контекст, отдельный запрос находит условия и шаги разных разделов.
        hits = []
        seen = set()
        routed = {(h["document_id"], h["page"], h.get("id"), h["text"]): h for h in self.fault_hits}
        for hit in [*self._lookup(query), *self.fault_hits, *self.primary]:
            hit = routed.get((hit["document_id"], hit["page"], hit.get("id"), hit["text"]), hit)
            key = (hit["document_id"], hit["page"], hit.get("condition"), hit["text"])
            if key not in seen:
                seen.add(key)
                hits.append(hit)
        return hits

    def add(self, hit, excerpt, *, text=None, kind="fact", required=True, work=None, lamp=False):
        excerpt = _clean(excerpt)
        statement = text or excerpt
        if hit.get("condition"):
            statement = f"{hit['condition'].rstrip('.')}. {statement}"
        item = dict(hit, excerpt=excerpt, text=statement, kind=kind, required=required, work=work, lamp=lamp)
        if not any((r["document_id"], r["page"], r["excerpt"]) ==
                   (item["document_id"], item["page"], excerpt) for r in self.evidence):
            self.evidence.append(item)

    def add_procedure(self, hit, *, kind="fact", work=None):
        """Сохраняет условия и шаги размеченной процедуры в порядке исходных частей."""
        parts = hit.get("page_parts") or [{"page": hit["page"], "text": hit["text"]}]
        for part in parts:
            source = dict(hit, page=part["page"], text=part["text"])
            if part["page"] != hit["page"]:
                source.pop("expert_rows", None)
                source.pop("fault_code", None)
            self.add(source, part["text"], kind=kind, work=work)

    def passage(self, query, pattern, *, kind="fact", explain=None, work=None, lamp=False):
        for hit in self.find(query):
            match = re.search(pattern, _clean(hit["text"]), re.I)
            if match:
                excerpt = match.group()
                self.add(hit, excerpt, text=explain(excerpt) if explain else None, kind=kind, work=work, lamp=lamp)
                return True
        return False


def _asks_oil_grade(q):
    if not re.search(r"\b(?:масл[оаеу]|масел)\b", q) or re.search(r"трансмисси|гидравл|коробк", q):
        return False
    oil = r"(?:моторн\w+\s+)?масл[оа]\b"
    use = (r"(?:(?:нужно|следует|можно|лучше|стоит|надо)\s+)?"
           r"(?:использовать|применять|выбрать|залить|заливать|долить)\b")
    return bool(re.search(r"\b(?:марк[ауие]|марок|вязкост\w*|sae|api)\b"
                          rf"|\b(?:какое|какие|какого)\s+(?:{use}\s+)?{oil}"
                          rf"|\bчто\s+за\s+{oil}\s+{use}", q))


def _oil_grades(search):
    """Связывает перечень масел по ГОСТ с таблицей SAE на том же листе руководства."""
    hits = search.find("Моторное масло класс вязкости SAE эксплуатационные материалы, применяемые в двигателе")
    tables_for_oil = set()
    for hit in hits:
        body = hit["text"]
        grades = re.search(r"(?:Масла моторные|Моторные масла)\s+по ГОСТ[^\n:]*:"
                           r"(?:[ \t]*\n[ \t]*М[-–][^\n]+)+", body, re.I)
        if grades:
            search.add(hit, grades.group())
        if re.search(r"(?:масла моторные|моторные масла)\s+по SAE\b", body, re.I):
            tables_for_oil.add((hit["document_id"], hit["page"], hit.get("section")))

    for hit in hits:
        if (hit["document_id"], hit["page"], hit.get("section")) not in tables_for_oil:
            continue
        for table in hit.get("tables", []):
            rows = table["rows"]
            if not rows or [_clean(v or "").casefold() for v in rows[0]] != ["sae", "api", "ааи", "сезон применения"]:
                continue
            data = rows[1:]
            if not data or any(len(row) != 4 or not row[0] or not row[3] for row in data):
                continue
            # В объединённой колонке класс качества записан один раз для всех сезонов.
            common = {i for i in (1, 2) if data[0][i] and all(row[i] is None for row in data[1:])}
            lines = [f"{rows[0][i]} {_clean(data[0][i])}." for i in sorted(common)]
            for row in data:
                season = _clean(row[3])
                viscosity = ", ".join(row[0].split())
                specs = [f"{rows[0][i]} {_clean(row[i])}" for i in (1, 2) if i not in common and row[i]]
                lines.append(f"{season.capitalize()} — " + ", ".join([f"SAE {viscosity}", *specs]) + ".")
            search.add(hit, hit["text"], text=" ".join(lines))


def _level(search, kind="fact"):
    found = search.passage("обслуживание системы смазки",
                          r"Проверять уровень масла необходимо.{1,550}?не превышая её\.",
                          kind=kind, work="Проверить уровень масла по руководству.", explain=lambda excerpt: (
                              f"{excerpt} По уровню масла фактическое давление определить нельзя."))
    if found:
        return True
    for hit in search.find(OIL_LEVEL):
        title = _clean(hit.get("section", "")).casefold()
        if hit.get("kind") == "procedure" and "проверк" in title and "уровн" in title and "масл" in title:
            search.add_procedure(hit, kind=kind, work="Проверить уровень масла по руководству.")
            return True
    return False


def _lamp(search, *, kind="fact"):
    return search.passage(OIL_SIGNAL,
                          r"Сигнализатор аварийного давления масла,.{1,1250}?двигатель не заводить\.",
                          kind=kind, lamp=True, explain=lambda excerpt: (f"Порог относится к аварийному сигнализатору. {excerpt} "
                              "По одному сигнализатору численное значение давления определить нельзя."))


def _asks_pressure_measurement(q, oil_context):
    if re.search(r"\b(?:норм\w*|долж\w*|порог\w*|срабатыв\w*|сопротивлен\w*)\b", q):
        return False
    action = r"(?:измер\w*|замер\w*|помер\w*|мерить|проверить|проверя(?:ть|ют|ется)|контролир\w*|манометр\w*)"
    subject = r"\bдавлени\w*\s+(?:(?:моторного\s+)?масла\b|в системе смазки\b)"
    if re.search(rf"\b{action}\s+(?:(?:фактическое|реальное)\s+)?{subject}", q) or re.search(
            rf"{subject}\s+(?:в двигателе\s+)?(?:можно\s+)?{action}\b", q):
        return True
    # Короткое уточнение без названия жидкости допустимо только после вопроса о масле.
    return bool(oil_context and re.fullmatch(
        rf"(?:а\s+)?(?:как|чем|можно(?: ли)?)\s+{action}\s+(?:его\s+)?давление[?!. ]*", q.strip()))


def _pressure_measurement(search):
    """Возвращает процедуру замера либо описание штатных приборов из руководства."""
    subject = r"давления масла(?: в (?:двигателе|системе смазки(?: двигателя)?))?"
    for hit in search.find("Измерение давления масла контрольным манометром"):
        if hit.get("kind") == "procedure" and re.fullmatch(
                rf"(?:измерение|замер|проверка) {subject}[.]?", _clean(hit.get("section", "")), re.I):
            search.add_procedure(hit)
            return True
    return search.passage(
        "Контроль за давлением масла датчик указателя давления масла и датчик сигнализатора",
        r"Контроль за давлением масла в системе смазки осуществляется с помощью датчика\b[^.!?]{1,400}\.",
        explain=lambda excerpt: f"{excerpt} Порядок контрольного замера отдельным манометром в этом описании не приведён.")


def _causes_from_heading(search, hits):
    """Собирает строки одной темы и оставляет условия на их физической странице."""
    by_page = {}
    for hit in hits:
        section = _clean(hit.get("section", "")).casefold()
        if not re.search(r"(?:пониженн|низк|недостаточн)\w*\s+давлени\w*\s+масла\b"
                         r"|\bдавление масла ниже\b", section):
            continue
        for table in hit.get("tables", []):
            rows = table.get("rows", [])
            if not rows:
                continue
            column = next((i for i, cell in enumerate(rows[0])
                           if re.fullmatch(r"(?:возможн\w*\s+)?причин[а-яё]*(?:\s+неисправност[а-яё]*)?",
                                           _clean(cell or "").casefold())), None)
            if column is None:
                continue
            causes = [_clean(row[column]) for row in rows[1:]
                      if len(row) == len(rows[0]) and row[column] and sum(bool(c) for c in row) >= 2]
            if not causes:
                continue
            note = re.search(r"(?:^|\n)[ \t]*\*+\s+(.+)\Z", hit["text"], re.S)
            footnote = _clean(note[1]) if note else ""
            key = (hit["document_id"], hit["page"], section, hit.get("condition", ""), footnote)
            _, found = by_page.setdefault(key, (hit, []))
            found.extend(causes)
    for key, (hit, causes) in by_page.items():
        text = " ".join(c.rstrip(" .") + "." for c in dict.fromkeys(causes))
        if key[-1]:
            text += " " + key[-1]
        search.add(hit, text, kind="cause")


def _low_pressure(search, *, causes_only=False):
    if not causes_only:
        _lamp(search, kind="risk")
        _level(search)
    hits = search.find("низкое давление масла")
    _causes_from_heading(search, hits)
    _oil_pump_inspection(search, causes_only)
    for hit in hits:
        for table in hit.get("tables", []):
            causes = []
            in_section = False
            for row in table["rows"]:
                cells = [_clean(cell) for cell in row if cell and cell.strip()]
                if len(cells) == 1:
                    if in_section:
                        break
                    in_section = cells[0].lower() == "низкое давление масла"
                elif in_section and cells:
                    causes.append(cells[0])
            if causes:
                excerpt = "Низкое давление масла. " + "; ".join(causes) + "."
                statement = (". ".join(causes) + ". Если падение давления подтвердится, "
                             "назначьте дефектовку системы смазки для установления причины.")
                work = "При подтверждённом падении давления провести дефектовку системы смазки с учётом результатов проверки и таблицы причин."
                search.add(hit, excerpt, text=". ".join(causes) + "." if causes_only else statement, kind="cause",
                           work=None if causes_only else work)
                break
        else:
            continue
        break
    if causes_only:
        return
    search.passage("под давлением смазываются",
                   r"Под давлением смазываются коренные и шатунные подшипники[^.]{1,400}\.",
                   kind="risk", explain=lambda excerpt: (
                       "Если давление действительно понижено и двигатель продолжает работать, "
                       "возможно повреждение подшипников. Это вывод из описания смазки под давлением. "
                       "Повреждение подшипников можно подтвердить при дефектовке."))


def _oil_pump_inspection(search, causes_only):
    """Связывает причину падения давления с проверкой деталей масляного насоса."""
    cause = search.passage(
        "Падение давления в системе смазки может быть вызвано износом деталей масляного насоса",
        r"Падение давления в системе смазки может быть вызвано износом деталей "
        r"масляного насоса или подшипников коленчатого и распределительного валов\."
        r"(?: При значительных износах деталей насоса он начинает работать шумно\."
        r" Чтобы выявить неисправности насоса, его необходимо снять с двигателя и разобрать\.)?",
        kind="cause")
    if cause and not causes_only:
        search.passage(
            "Разборка масляного насоса проверить износ шестерен замерив их высоту",
            r"Проверить износ шестер[её]н, замерив их высоту\. Новые шестерни имеют размер высоты "
            r"\d+,\d+\s*(?:[…–—-]|\.{2,3})\s*\d+,\d+ мм\."
            r"(?: При значительном износе заменить дефектную шестерню\.)?",
            work="Проверить износ шестерен масляного насоса, измерив их высоту.")


def _diagram_codes(question):
    words = re.findall(r"\b[а-яё]{2,5}\b", question, re.I)
    return list(dict.fromkeys(w.upper() for w in words
                              if (w.isupper() or not re.search("[ауоыиэяюёе]", w, re.I))
                              and w.upper() not in {"ЯМЗ", "ЗМЗ", "ДВС"}))


def _diagram(search, codes):
    if not codes:
        return
    for hit in search.find(" ".join(codes)):
        labels = []
        for code in codes:
            match = re.search(rf"^\s*{re.escape(code)}\s+[–—-]\s*(.+?)\s*$", hit["text"], re.M | re.I)
            if match:
                labels.append(f"{code} — {match[1]}")
        if len(labels) == len(codes):
            search.add(hit, "\n".join(labels), text=". ".join(labels) + ".")
            return


def _valve(search, question):
    search.passage("редукционный клапан регулировке",
                   r"Редукционный клапан регулировке не подлежит\.(?: Детали клапана[^.]*\.)?")
    if "редукцион" in question and "масл" in question and re.search(r"промыв|промыт|промыва|промо|засор|чисти", question):
        search.passage("промывка редукционного клапана масляного насоса при засорении",
                       r"[^.!?]*\bзасорени\w* редукционного клапана масляного насоса\.\s*"
                       r"(?:В этом случае\s+)?редукционный клапан\b[^.!?]*\bпромы\w*[^.!?]*\.")
    if re.search(r"разбор|разобр|перебор|плунжер|пружин|дефектов", question):
        search.passage("выявить неисправности насоса", r"Чтобы выявить неисправности насоса[^.]*\.")
        search.passage("плунжер редукционного клапана",
                       r"Убедиться, что плунжер.{1,950}?шайб под пружину.{1,120}?запрещается\.")


_PRESSURE_UNIT = r"(?:кгс\s*/\s*см\s*\^?\s*2|[км]?па|мбар|бар|атм|psi)"


def _unit(text):
    return re.sub(r"[\s^]", "", text.casefold())


def _pressure(question):
    text = unicodedata.normalize("NFKC", question).casefold()
    match = re.search(r"(?<![\w.,-])(\d+(?:[.,]\d+)?)\s*(" + _PRESSURE_UNIT + r")(?!\w)", text)
    if match:
        return Decimal(match[1].replace(",", ".")), _unit(match[2])
    return None


def _sensor_terms(text):
    """Приводит названия валов и частоты вращения к формулировкам диагностических таблиц."""
    text = text.casefold().replace("ё", "е")
    for form, replacement in ((r"\bраср?предвал\w*", "распределительного вала"),
                              (r"\bколенвал\w*", "коленчатого вала"),
                              (r"\bшестерн\w*", "шестерня"),
                              (r"\bоборот\w*", "частоты"),
                              (r"\b(?:набира|развива)\w*", "развивает")):
        text = re.sub(form, replacement, text)
    return {_stem(w) for w in re.findall(r"[а-я0-9]+", text)
            if (len(w) >= 3 or w == "не") and w not in {"при", "для", "если", "двигатель"}}


def _diagnostic_sensor_gap(search, question):
    """Ищет диапазон зазора по паре деталей и условию в одной диагностической строке."""
    parts = re.search(r"\b(?:расстояние от|зазор между)\s+([^?.;]+?)\s+(?:до|и)\s+([^?.;]+)", question)
    if not parts or "датчик" not in parts[1]:
        return None
    condition = re.search(r"\bпри диагностике\s+([^.,;!?]+)", question[:parts.start()])
    if not condition or "ошибк" not in condition[1]:
        return None
    symptom = _sensor_terms(question[:condition.start()])
    diagnostic = _sensor_terms(condition[1])
    targets = _sensor_terms(parts[1] + " " + parts[2])
    # Уточнение «ошибка по датчику распредвала» определяет также вал во второй детали пары.
    targets.update(diagnostic & {"распределительн", "коленчат"})
    interval = re.compile(r"\bзазор\s*(?:в пределах\s+)?\d+(?:[.,]\d+)?\s*"
                          r"(?:…|\.{2,3}|[–—-])\s*\d+(?:[.,]\d+)?\s*мм\b", re.I)
    matches = {}
    for hit in search.find(parts[0]):
        if hit.get("kind") != "table" or not symptom <= _sensor_terms(hit.get("section", "")):
            continue
        for table in hit.get("tables", []):
            rows = table.get("rows", [])
            if not rows:
                continue
            header = [_clean(cell or "").casefold() for cell in rows[0]]
            if "причины неисправности" not in header or "способ устранения" not in header:
                continue
            for row in rows[1:]:
                if len(row) != len(header):
                    continue
                cause = _clean(row[header.index("причины неисправности")] or "")
                action = _clean(row[header.index("способ устранения")] or "")
                if "зазор" not in cause.casefold() or not targets <= _sensor_terms(cause):
                    continue
                observation, _, instruction = action.casefold().partition(".")
                if (not diagnostic <= _sensor_terms(observation)
                        or re.search(r"\b(?:не|нет|без|отсутств\w*)\b", observation)
                        or not re.search(r"\bпроверить\b", instruction)
                        or not re.search(r"\b(?:от)?регулировать\s+зазор\b", instruction)
                        or re.search(r"\bне\s+(?:проверять|проверить|(?:от)?регулировать)\b", instruction)
                        or len(interval.findall(instruction)) != 1):
                    continue
                matches[(hit["document_id"], hit["page"], cause, action, hit.get("condition"))] = hit
    if len(matches) != 1:
        return "В таблицах руководства не найден диапазон зазора для указанных деталей и условий."
    (_, _, cause, action, _), hit = next(iter(matches.items()))
    search.add(hit, cause + " | " + action, text=cause + ". " + action.rstrip(".") + ".")
    return ""


def _table_row(search, hits, code, pressure):
    """Проверяет код, физическую величину и единицу из заголовка таблицы."""
    value, unit = pressure
    for hit in hits:
        for table in hit.get("tables", []):
            rows = table["rows"]
            if len(rows) < 2:
                continue
            header = [_clean(h or "") for h in rows[0]]
            normalized = [unicodedata.normalize("NFKC", col).casefold() for col in header]
            if not any("сопротивлен" in col or re.search(r"\bом\b", col) for col in normalized):
                continue
            column = next((idx for idx, col in enumerate(normalized) if "давлен" in col
                           and any(_unit(u) == unit for u in re.findall(r"(?<!\w)" + _PRESSURE_UNIT + r"(?!\w)", col))), None)
            if column is None:
                continue
            current = ""
            for row in rows[1:]:
                if len(row) != len(header):
                    continue
                current = (row[0] or current).strip()
                measured = _clean(row[column] or "").replace(",", ".")
                if current != code or not re.fullmatch(r"\d+(?:\.\d+)?", measured) or Decimal(measured) != value:
                    continue
                headings = " | ".join(label or "—" for label in header)
                values = " | ".join([current, *(_clean(cell or "") or "—" for cell in row[1:])])
                excerpt = f"{headings}\n{values}"
                search.add(hit, excerpt, text=excerpt)
                return True
    return False


def _stem(term):
    """Выделяет основу для проверки совпадения внутри предложения."""
    if len(term) < 5 or not term.isalpha():
        return term
    return re.sub(r"(иями|ями|ого|его|ами|ему|ому|ах|ях|ов|ев|ая|ый|ой|ые|ых|ую|ая|ом|ем|а|я|е|у|ы|и|ю)$", "", term)


def _catalog_parts(question):
    """Связывает запрошенную колонку с названием детали в каждой части вопроса."""
    clauses = re.split(r",?\s+и\s+(?=(?:сколько|како[ей]|каков[оа]?)\b)", question.strip(" ?!."))
    requests = []
    if len(clauses) == 2:
        for clause in clauses:
            field = re.search(r"\b(сколько|(?:каталожн\w*\s+)?номер|обозначение|артикул)\s+"
                              r"(?:(?:у|для)\s+)?(.+)$", clause)
            if not field:
                break
            name = re.sub(r"(?:\s+(?:ид[её]т|идут|нужно|нужны|требуется))?(?:\s+на (?:один )?двигатель)?$",
                          "", field[2])
            if requests and re.match(r"(?:их|таких|этих|такой|этой)\b", name):
                name = requests[0][0]
            requests.append((name, "count" if field[1] == "сколько" else "code"))
    if {field for _, field in requests} == {"count", "code"}:
        if _catalog_terms(requests[0][0]) == _catalog_terms(requests[1][0]):
            return [(requests[0][0], "both")]
        return requests
    if (re.search(r"обозначени|каталожн\w*\s+номер|артикул", question)
            and re.search(r"сколько|количеств", question)):
        name = re.search(r"\b(?:у|для)\s+(.+?)(?=\s+и\s+|\s+по\s+таблице\b|[?;,.]|$)", question)
        if name:
            return [(name[1].strip(), "both")]
    return []


def _catalog_terms(text):
    """Нормализует названия узлов и окончания при сверке с заголовком или строкой."""
    text = re.sub(r"\bколенвал(?:а|у|ом|е)?\b", "коленчатого вала", text.casefold())
    text = re.sub(r"\bзаглушк", "пробк", text)
    text = re.sub(r"чок\b", "чк", text)
    text = re.sub(r"ний\b", "ния", text)
    return {_stem(w) for w in re.findall(r"[\w-]+", text)}


def _catalog_row(search, subject, field, title_terms):
    """Читает обозначение и количество одной детали в одной строке таблицы."""
    terms = _catalog_terms(subject)
    hits = search.find(subject)
    matches = {}
    for hit in hits:
        if hit.get("kind") != "table" or not title_terms.issubset(_catalog_terms(hit.get("section", ""))):
            continue
        for table in hit.get("tables", []):
            rows = table.get("rows", [])
            if not rows:
                continue
            header = [_clean(cell or "").casefold() for cell in rows[0]]
            if not {"наименование", "обозначение"}.issubset(header):
                continue
            qty = next((i for i, col in enumerate(header) if re.fullmatch(r"количество[,\s]+шт\.?", col)), None)
            if qty is None:
                continue
            for row in rows[1:]:
                if len(row) != len(header):
                    continue
                name = _clean(row[header.index("наименование")] or "")
                words = _catalog_terms(name)
                if not terms or not terms.issubset(words):
                    continue
                code = _clean(row[header.index("обозначение")] or "")
                count = _clean(row[qty] or "")
                matches[(name, code, count, hit["document_id"], hit["page"], hit.get("condition"))] = hit
    if len(matches) != 1:
        return False
    (name, code, count, *_), row_hit = next(iter(matches.items()))
    if field in ("code", "both") and not code:
        return False
    if field in ("count", "both") and not re.fullmatch(r"\d+", count):
        return False

    excerpt = f"{name} {code} {count}"
    if field != "code":
        row_hit = _catalog_heading(search, hits, row_hit, excerpt)
    if field == "both":
        answer = f"{name}. Обозначение — {code}. Количество — {count} шт."
    elif field == "count":
        answer = f"{name} — {count} шт."
    else:
        answer = f"{name}. Обозначение — {code}."
    search.add(row_hit, excerpt, text=answer)
    return True


def _catalog_heading(search, hits, row_hit, excerpt):
    """Привязывает заголовок предыдущего листа только к точно совпавшей строке."""
    row_page = row_hit["page"]
    row_pattern = re.compile(r"(?<!\S)" + re.escape(excerpt) + r"(?!\S)")
    for joined in hits:
        if (joined.get("kind") != "table" or joined["document_id"] != row_hit["document_id"]
                or joined.get("source_sha256") != row_hit.get("source_sha256")):
            continue
        parts = joined.get("page_parts", [])
        for previous, part in zip(parts, parts[1:]):
            if part["page"] != row_page or not row_pattern.search(_clean(part["text"])):
                continue
            heading = _clean(previous["text"])
            if not all(w in heading.casefold() for w in ("наименование", "обозначение", "количество", "шт")):
                continue
            title = re.search(r"\bтаблица\s+\d+[а-я]?\b", heading, re.I)
            label = (title[0].capitalize() + ". " if title else "") + "Количество указано в штуках."
            search.add(dict(joined, page=previous["page"]), heading, text=label)
            return dict(joined, page=row_page)
    return row_hit


_PART_NUMBER = re.compile(r"(?<![\w.-])\d+(?:[.-]\d+)+(?![\w-]|\.\w)")
_HYPHENS = str.maketrans(dict.fromkeys("‐‑‒–—−", "-"))


def _drain_filter_number(question):
    """Выделяет полный номер фильтра из вопроса о сливе воды."""
    operation = re.search(r"\b(?:слить|сливать)\s+(?:собранную\s+)?воду\b"
                          r"|(?:^|\b(?:порядок|процедура|выполнить|провести)\s+)слив[а]?\s+воды\b", question)
    if not operation or "фильтр" not in question:
        return ""
    numbers = _PART_NUMBER.findall(question.translate(_HYPHENS))
    return numbers[0] if len(numbers) == 1 else ""


def _drain_filter(search, question, number):
    """Сохраняет условия и шаги процедуры для указанного фильтра."""
    for hit in search.find(f"Слив воды из фильтра {number}"):
        title = hit.get("section", "").casefold()
        if hit.get("kind") != "procedure" or not re.search(r"\bслив\s+воды\b", title) or "фильтр" not in title:
            continue
        if any(word in question and word not in title for word in ("предварительн", "тонк", "груб", "топлив", "маслян")):
            continue
        parts = hit.get("page_parts") or [hit]
        body = "\n".join([hit.get("condition", ""), title, *(part["text"] for part in parts)])
        if number in _PART_NUMBER.findall(body.translate(_HYPHENS)):
            search.add_procedure(hit)
            return True
    return False


def _component_words(text):
    text = re.sub(r"\bмаслонасос", "масляный насос", text.casefold())
    return [_stem(word) for word in re.findall(r"[\w-]+", text)]


def _disassembly(search, question):
    """Извлекает нумерованную разборку названного узла."""
    request = re.search(r"\b(?:разобрать|разбирать|разборк[аиуе])\s+"
                        r"([\w-]+(?:[ \t]+[\w-]+){0,4})[?!. ]*$", question.strip())
    if not request:
        return False
    component = request[1]
    wanted = _component_words(component)
    for hit in search.find(f"Разборка {component}"):
        kind = hit.get("kind")
        if kind not in {"procedure", "text"}:
            continue
        title = hit.get("section", "").split(".", 1)[0]
        owner = _component_words(title.removeprefix("Разборка "))
        # В разделе об узле заголовок разборки может быть сокращён.
        for heading in re.finditer(r"^[ \t]*Разборка[ \t]+([^\n.:]+?)[ \t]*[:.]?[ \t]*$", hit["text"], re.I | re.M):
            name = _component_words(heading[1])
            body = hit["text"][heading.end():].strip()
            boundary = re.search(r"^[ \t]*(?:После разборки\b|Сборка\b|Дефектовка\b)", body, re.M | re.I)
            if boundary:
                body = body[:boundary.start()].rstrip()
            elif not (kind == "procedure" and title == hit.get("section")
                      and title.casefold().startswith("разборка ") and owner == wanted
                      and len(hit.get("page_parts", [hit])) == 1):
                continue
            named_in_heading = (owner == wanted and name and all(word in wanted for word in name)
                                or name == wanted and len(name) > 1)
            # Если индекс сохранил только общий раздел, название насоса подтверждает сам шаг.
            named_in_step = (kind == "text" and title.casefold() == "разборка двигателя"
                             and name == ["насос"] and wanted == _component_words("масляный насос")
                             and re.search(r"\b(?:крышки|корпуса) масляного насоса\b", _clean(body), re.I))
            if not (named_in_heading or named_in_step):
                continue
            parts = re.split(r"^[ \t]*(\d+)[.)][ \t]+", body, flags=re.M)
            numbers = [int(number) for number in parts[1::2]]
            steps = [_clean(step) for step in parts[2::2]]
            if parts[0].strip() or not steps or not all(steps) or numbers != list(range(1, len(steps) + 1)):
                continue
            text = "\n".join(f"{i}. {step}" for i, step in enumerate(steps, 1))
            search.add(hit, body, text=text)
            return True
    return False


_QUESTION_WORD = r"(?:чем|как(?:ая|ие|ой|ую|ое|ов[аы]?)?|куда|где|какими?|сколько|что)\b"
_ACTIONS = (
    (r"\b(?:подготов|очист|протер)", r"\b(?:подготов|очист|протер|протир|обезжир|промы|просуш|проду)"),
    (r"\b(?:постав|установ|помест|встав|улож)", r"\b(?:постав|устан[ао]в|помес|встав|улож|уклад|размес)"),
)


def _clause_terms(clause):
    """Оставляет слова, по которым можно проверить предмет и условия вопроса."""
    stop = {"чем", "где", "куда", "это", "что", "при", "под", "для", "над", "как", "или", "его",
            "она", "они", "мне", "этот", "этой", "надо", "нужно", "следует", "сколько", "должен"}
    words = re.findall(r"[а-яёa-z0-9]+", clause.casefold())
    return {_stem(w) for w in words if (len(w) >= 3 or w.isdigit()) and w not in stop
            and not re.fullmatch(_QUESTION_WORD, w) and not w.endswith(("ть", "ться"))}


def _clause_score(clause, terms, sentence):
    shared = len(terms & _clause_terms(sentence))
    if shared < 2 or 3 * shared < 2 * len(terms):
        return 0
    # Признаки и место установки должны относиться к одному варианту («с ...», «а без ...»).
    parts = re.split(r"[,;]\s*(?:а|но)\s+", sentence.casefold())
    features = re.search(r"\b(без|со?)\s+(.+)", clause)
    if features:
        required = _clause_terms(features[2])
        marker = r"со?" if features[1] != "без" else "без"
        parts = [p for p in parts if any(required <= _clause_terms(c)
                 for c in re.findall(rf"\b{marker}\s+([^,;.!?]+)", p))]
    for part in parts:
        if any(re.search(requested, clause) and not re.search(stated, part) for requested, stated in _ACTIONS):
            continue
        if clause.startswith("куда ") and not re.search(r"\b(?:в|во|на|к|ко|под|над|между|за|внутрь|вверх|вниз|впер[её]д)\b", part):
            continue
        if clause.startswith("чем ") and not re.search(r"\b(?:с помощью|посредством|[а-яё]{3,}(?:ом|ем|кой|ью|ами|ями))\b", part):
            continue
        return shared
    return 0


def _marking_root(word):
    """Сводит падежные формы цвета и названия направляющей к одной основе."""
    word = word.casefold().replace("ё", "е")
    return _stem(re.sub(r"(?:ыми|ими|ый|ий|ой|ей|ым|им|ых|их)$", "", word))


def _marked_row(hits, component, colour, guide):
    """Находит однозначную строку с цифрой детали и цветом сопряжённой поверхности."""
    matches = {}
    for hit in hits:
        if hit.get("kind") != "table":
            continue
        for table in hit.get("tables", ()):
            rows = table.get("rows", ())
            if len(rows) < 2:
                continue
            header = [_clean(cell or "") for cell in rows[0]]
            columns = [cell.casefold() for cell in header]
            digit = next((i for i, cell in enumerate(columns)
                          if component.search(cell) and re.search(r"цифр|номер", cell)
                          and re.search(r"маркировк|групп", cell)), None)
            paint = next((i for i, cell in enumerate(columns) if re.search(r"цвет|краск", cell)
                          and (not guide or guide in {_marking_root(w) for w in re.findall(r"[а-яё]+", cell)})), None)
            if digit is None or paint is None or digit == paint:
                continue
            for cells in rows[1:]:
                if len(cells) != len(header):
                    continue
                row = [_clean(cell or "") for cell in cells]
                if not row[digit].isdigit() or _marking_root(row[paint]) != colour:
                    continue
                text = " | ".join(header) + "\n" + " | ".join(row)
                key = hit["document_id"], hit["page"], hit.get("condition"), text
                matches.setdefault(key, (hit, text))
    return next(iter(matches.values())) if len(matches) == 1 else None


def _marked_fit(search, marking, check):
    """Отвечает по таблице маркировки и связанному указанию о ручной проверке."""
    part = re.search(r"\b(?:на|у)\s+([а-яё]{4,})\b", marking)
    part = part or re.search(r"\b(?:посадк|подбор)\w*\s+([а-яё]{4,})\b", check)
    colour = re.search(r"\b([а-яё]{4,})\s+(?:метк|маркировк|краск)\w*\b", marking)
    manual = r"\b(?:пальц\w*|от\s+руки|вручную|рукой)\b"
    if not (part and colour and re.search(r"\b(?:цифр|номер)", marking)
            and re.search(r"\b(?:посадк|подбор)", check) and re.search(manual, check)):
        return None

    component = re.compile(rf"\b{re.escape(_stem(part[1]))}\w*\b", re.I)
    guide = re.search(r"\bдля\s+([а-яё]{4,})\s+с\b", marking)
    row = _marked_row(search.find(marking), component, _marking_root(colour[1]),
                      _marking_root(guide[1]) if guide else "")
    if row is None:
        return "В найденных таблицах не подтверждена однозначная цифра маркировки."

    row_hit, text = row
    search.add(row_hit, text, text=text)
    row_source = row_hit["document_id"], row_hit.get("source_sha256")
    row_pages = {row_hit["page"], *row_hit.get("related_pages", ())}
    for hit in search.find(f"{check} {part[1]}"):
        if hit.get("kind", "text") not in ("text", "procedure"):
            continue
        if (hit["document_id"], hit.get("source_sha256")) != row_source:
            continue
        if hit["page"] not in row_pages and row_hit["page"] not in hit.get("related_pages", ()):
            continue
        if hit.get("condition") and row_hit.get("condition") and hit["condition"] != row_hit["condition"]:
            continue
        for sentence in re.split(r"(?<=[.!?])\s+(?=[А-ЯЁ])", _clean(hit["text"])):
            if (component.search(sentence) and re.search(manual, sentence, re.I)
                    and re.search(r"\b(?:опуска|перемещ|проход|вход|двига|скольз)", sentence, re.I)
                    and re.search(r"\b(?:медленн|свободн|плавн|без\s+(?:заедан|усили|закусыван))", sentence, re.I)):
                search.add(hit, sentence)
                return ""
    return f"В найденном тексте не подтверждена часть вопроса «{check}»."


_RUN_MOTOR = (r"(?:(?:за)?пустить|(?:дать\s+)?поработать)\s+(?:двигател[ьюя]|мотор[уа]?)"
              r"|(?:двигател[ьюя]|мотор[уа]?)\s+(?:(?:должен|дать)\s+)?(?:по)?работать"
              r"|(?:держать|оставить)\s+(?:двигатель|мотор)\s+(?:включ[её]нным|работающим)")
_RUN_SPAN = re.compile(rf"\b(?:{_RUN_MOTOR})\s+(?:(?:в течение|на)\s+)?(?:не менее\s+|не более\s+)?"
                       r"\d+(?:[.,]\d+)?(?:\s*(?:[-–—…]|до)\s*\d+(?:[.,]\d+)?)?\s*"
                       r"(?:секунд\w*|минут\w*|час(?:а|ов)?)\b", re.I)


def _drained_volume(clause, substance, sentence):
    """Возвращает None для чужой операции, False для слива без указанного объёма."""
    action = re.search(r"\b(?:слить|сливать|сливают|выпустить)\s+(.+?)(?:\s+и\s+|[;!]|$)", sentence, re.I)
    if not action or re.search(r"\bне\s*$", sentence[:action.start()], re.I) or not substance.search(action[1]):
        return None
    quantity = r"\d+(?:[.,]\d+)?\s*(?:мл|л|миллилитр\w*|литр\w*)\b"
    portion = r"(?:по\s+)?"
    if re.search(r"\bкажд", clause) and not re.search(r"\bиз каждого\b", sentence, re.I):
        portion = r"по\s+"
    return bool(re.match(rf"{portion}{quantity}\s+{substance.pattern}", action[1], re.I))


def _duration_terms(text):
    """Сохраняет цель и условия работы; распознаёт равнозначные обозначения удаления воздуха."""
    text = re.sub(r"\b(?:выгнать|удалить|удалени[яе])\s+(?:воздух[а]?|воздушн\w+\s+проб\w*)\b",
                  "деаэрация", text.casefold())
    text = re.sub(rf"\b(?:{_RUN_MOTOR})\b", " ", text)
    text = re.sub(r"\b(?:как долго|сколько (?:времени|минут|секунд|часов)|на какое время|"
                  r"для|чтобы|дать|затем|потом|после этого)\b", " ", text)
    return {_stem(w) for w in re.findall(r"[а-яё]+", text)}


def _duration_after(clause, following):
    """Проверяет длительность действия и запрошенные условия в той же фразе."""
    wanted = _duration_terms(clause)
    matches = []
    for sentence in following:
        motor = _RUN_SPAN.search(sentence)
        if motor and not re.search(r"\bне\s*$", sentence[:motor.start()], re.I):
            if wanted <= _duration_terms(sentence):
                matches.append(sentence)
    return (1, matches[0]) if len(matches) == 1 else (0, "")


def _procedure_steps(text):
    """Для каждого пункта возвращает фразы и непосредственно следующий нумерованный пункт."""
    marker = r"^[ \t]*(\d{1,3})[.)][ \t]+(?=[А-ЯЁ])"
    for paragraph in re.split(r"\n[ \t]*\n", text):
        steps = []
        for block in re.split(r"(?m)(?=^[ \t]*\d{1,3}[.)][ \t]+[А-ЯЁ])", paragraph):
            if not block.strip():
                continue
            number = re.match(marker, block)
            body = _clean(block[number.end():] if number else block)
            sentences = [s for s in re.split(r"(?<=[.!?])\s+(?=[А-ЯЁ])", body)
                         if 0 < len(s) <= 850 and not re.match(r"Рис\.", s)]
            steps.append((int(number[1]) if number else None, sentences))
        for (number, sentences), (next_number, following) in zip(steps, [*steps[1:], (None, [])]):
            consecutive = number is not None and next_number == number + 1
            yield sentences, following if consecutive else []


def _washing_temperature(clause, sentence, context):
    """Ищет предел температуры именно той жидкости, которой промывают указанный узел."""
    if "температур" not in clause or not re.search(r"промы|моем|моём|мыть", clause):
        return None
    media = (r"вод(?:а|е|у|ы|ой)", r"масл(?:о|е|а|ом)", r"раствор(?:е|а|ом)?", r"керосин(?:е|а|ом)?")
    requested = [pattern for pattern in media if re.search(rf"\b{pattern}\b", clause)]
    if len(requested) != 1:
        return None
    if re.search(r"минимальн|не ниже|не менее", clause):
        bound = r"не (?:ниже|менее)\s+"
    elif re.search(r"максимальн|не выше|не более", clause):
        bound = r"не (?:выше|более)\s+"
    else:
        bound = r"(?:не (?:ниже|менее|выше|более)\s+)?"
    # Привязываем число к жидкости непосредственно перед ним, даже в перечислении.
    reading = rf"\b{requested[0]}\s+(?:с температурой|при температуре)\s+{bound}[+−-]?\d+(?:[.,]\d+)?\s*°\s*[сc]\b"
    if not re.search(reading, sentence, re.I) or not re.search(r"промы|мыть|моют", sentence, re.I):
        return 0
    component = re.search(r"\b(?:детал(?:и|ей)|узлы|для промывки)\s+(?:разобранн\w+\s+)?([а-яё]{4,})\b", clause)
    if component:
        target = _stem(component[1])
        if target not in _clause_terms(context):
            return 0
        # Явно названный объект промывки важнее темы соседних фраз.
        qualified = re.search(r"\bпромывк[ау]\s+(.+?)\s+(?:должна|производ|выполн|осуществ)", sentence, re.I)
        if (qualified and not re.fullmatch(r"деталей|должна", qualified[1], re.I)
                and target not in _clause_terms(qualified[1])):
            return 0
    return 3


def _operating_terms(text):
    """Сводит формулировки условий эксплуатации к словам, общим для вопроса и руководства."""
    text = re.sub(r"\b(?:мотор(?:а|ом|е|у)?|двигател(?:ь|я|ем|е|ю))\b", "двигатель", text.casefold())
    text = re.sub(r"\bне\s+работа(?:ет|ющ\w*)\b", "остановлен", text)
    text = re.sub(r"\bработа(?:ет|ющ\w*)\b", "работает", text)
    text = re.sub(r"\b(?:стоит|стоящ\w*)\b", "стоит", text)
    return _clause_terms(text) - {"пока", "если", "когда"}


def _conditional_instruction(search, question):
    """Связывает условие из вопроса с распоряжением «в таком случае» в том же пункте."""
    prompt = re.search(r"\b(?:что (?:делать|сделать) с|как (?:пользоваться|управлять))\s+", question)
    if not prompt:
        return
    targets, *after = re.split(r"\s*,?\s*\b(?:если|когда|пока)\b\s*", question[prompt.end():], maxsplit=1)
    condition = question[:prompt.start()].strip(" .?!,") or (after[0] if after else "")
    needed = _operating_terms(condition)
    controls = _clause_terms(targets)
    if len(needed) < 2 or not controls or re.search(r"\bне\b", condition):
        return
    for hit in search.primary:
        if hit.get("kind", "text") not in ("text", "procedure"):
            continue
        for sentences, _ in _procedure_steps(hit["text"]):
            for before, action in zip(sentences, sentences[1:]):
                if (re.search(r"\b(?:в (?:таких случаях|этом случае)|при этом)\b", action, re.I)
                        and controls <= _clause_terms(action) and needed <= _operating_terms(before)):
                    search.add(hit, before + " " + action)
                    return


def _compound_answer(search, question):
    """Ищет подтверждение каждой части вопроса в пределах одного пункта руководства."""
    clauses = re.split(rf",?\s+и\s+(?={_QUESTION_WORD})", question.strip(" ?!."))
    if len(clauses) != 2 or not re.search(rf"\b{_QUESTION_WORD}", clauses[0]):
        return ""
    marked = _marked_fit(search, *clauses)
    if marked is not None:
        return marked
    timed = bool(re.search(r"\b(?:как долго|сколько (?:времени|минут|секунд|часов)|на какое время)\b", clauses[1])
                 and re.search(_RUN_MOTOR, clauses[1]))
    liquid = re.search(r"\bсколько\s+(топлив|масл|вод|жидкост)[а-яё]*\b", clauses[0]) if timed else None
    substance = re.compile(rf"\b{liquid[1]}(?:о|а|у|ы|ой|и|ь|ью)?\b", re.I) if liquid else None
    terms = [_clause_terms(substance.sub(liquid[1], c) if i == 0 and substance else c)
             for i, c in enumerate(clauses)]
    hits = {}
    for clause in clauses:
        for hit in search.find(clause):
            key = (hit["document_id"], hit["page"], hit.get("condition"), hit["text"])
            hits.setdefault(key, hit)
    best_key = (0, 0)
    for hit in hits.values():
        if hit.get("kind", "text") not in ("text", "procedure"):
            continue
        for sentences, next_step in _procedure_steps(hit["text"]):
            step = " ".join(sentences)
            matches = []
            for clause, anchors in zip(clauses, terms):
                scored = []
                for sentence in sentences:
                    subject = sentence
                    if liquid and clause == clauses[0]:
                        # «Слить топливо» подтверждает операцию, даже когда количество пропущено.
                        subject = substance.sub(liquid[1], subject)
                    temperature = _washing_temperature(clause, sentence, step)
                    score = _clause_score(clause, anchors, subject) if temperature is None else temperature
                    scored.append((score, sentence))
                matches.append(max(scored, key=lambda row: row[0], default=(0, "")))
            if timed:
                matches[1] = (0, "")
                if matches[0][0]:
                    following = sentences[sentences.index(matches[0][1]) + 1:]
                    volume = _drained_volume(clauses[0], substance, matches[0][1]) if liquid else True
                    if volume is not None:
                        matches[1] = _duration_after(clauses[1], following + next_step)
                    if not volume:
                        matches[0] = (0, "")
            key = (sum(score > 0 for score, _ in matches), sum(score for score, _ in matches))
            if key > best_key:
                best_key, best_hit, best_matches = key, hit, matches
    if not best_key[0]:
        return ""
    missing = []
    for clause, (score, sentence) in zip(clauses, best_matches):
        if score:
            search.add(best_hit, sentence)
        else:
            missing.append(f"В найденном тексте не подтверждена часть вопроса «{clause}».")
    return " ".join(missing)


_INSTRUCTION = re.compile(
    r"\b(?:долж|следует|рекоменду|установ|встав|ориентир|допуст|предпочт|правил|необходим|"
    r"заворач|провери|промы|смаз|не\s+более|не\s+допуска)", re.I)
_MASS = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:г|кг)\b(?!\s*[·∙⋅×*\uf0d7.]?\s*см\b)", re.I)
_TORQUE = re.compile(r"\b\d+(?:[.,]\d+)?\s*н\s*(?:[·∙⋅×*.]\s*)?м\b", re.I)
_PRINTED_ROW = re.compile(r"(?m)^[ \t]*\d+[ \t]{2,}\S[^\n]*[ \t]{2,}\S[^\n]*$")
_DIRECTION = re.compile(r"\b(?:вперед|вперёд|вверх|вниз|назад|внутрь|наружу|"
                        r"в\s+(?:одну|разные|противоположные)\s+сторон)", re.I)
_CYLINDER_ORDINALS = ("перв", "втор", "трет", "четверт", "пят", "шест", "седьм", "восьм", "девят")
_COLOUR = re.compile(r"\b(син|зелен|голуб|красн|желт|бел|черн)[а-я]+")


def _instruction_context(search, question):
    """Выбирает связанный отрывок, сохраняющий предмет вопроса и указание руководства."""
    question = question.casefold().replace("ё", "е")
    asks_torque = bool(re.search(r"\b(?:с\s+каким|какой|каков)\s+момент|\bмомент\s+затяжк", question))
    anchors = _clause_terms(question)
    if asks_torque:
        anchors.discard("момент")
    if len(anchors) < 5:
        return
    min_shared = 3 if asks_torque else 5
    clauses = re.split(rf",?\s+и\s+(?={_QUESTION_WORD})", question)
    subjects = [_clause_terms(clause) for clause in clauses]
    colours = set(_COLOUR.findall(question))
    cylinder_numbers = set()
    if "цилиндр" in question:
        cylinder_numbers = {str(index) for index, prefix in enumerate(_CYLINDER_ORDINALS, 1)
                            if re.search(rf"\b{prefix}(?:ом|ем|ого|его|ый|ий|ой|ую|ей|ье|ьем)\b", question)}
    asks_mass = bool(re.search(r"\b(?:груз\w*|масс\w*)\b", question))
    asks_direction = "ориентир" in question
    asks_permission = question.startswith("можно ли ")
    best = (0, 0, 0)
    chosen = None
    for hit in search.primary:
        if hit.get("kind", "text") not in ("text", "procedure"):
            continue
        body = hit["text"]
        if re.match(r"\s*таблица\s+\d+", body, re.I):
            rows = list(_PRINTED_ROW.finditer(body))
            if rows:
                body = body[rows[-1].end():]
        steps = re.split(r"\n[ \t]*\n|\n(?=\s*\d{1,3}[.)]\s+[А-ЯЁ])", body)
        for step in steps:
            sentences = re.split(r"(?<=[.!?])\s+(?=[А-ЯЁ])", _clean(step))
            for start, sentence in enumerate(sentences):
                if re.match(r"Рис\.\s*\d", sentence):
                    continue
                for end in range(start + 1, min(start + 3, len(sentences)) + 1):
                    excerpt = " ".join(sentences[start:end])
                    if len(excerpt) > 620 or not _INSTRUCTION.search(excerpt):
                        continue
                    normalized = excerpt.casefold().replace("ё", "е")
                    words = _clause_terms(normalized)
                    shared = len(anchors & words)
                    if shared < min_shared or shared * 5 < 3 * len(anchors):
                        continue
                    # Число из соседнего предложения может относиться к другой детали.
                    if asks_torque and (end != start + 1 or len(_TORQUE.findall(excerpt)) != 1):
                        continue
                    if len(subjects) > 1 and any(len(subject & words) < 2 for subject in subjects):
                        continue
                    if not colours <= set(_COLOUR.findall(normalized)):
                        continue
                    if asks_mass and not _MASS.search(excerpt):
                        continue
                    if asks_direction and not _DIRECTION.search(excerpt):
                        continue
                    if asks_permission and not re.search(r"следует|долж|допуска|разреш|запрещ", normalized):
                        continue
                    if cylinder_numbers:
                        groups = re.findall(r"\b(\d+(?:[,\s]+\d+)*)\s+цилиндр", normalized)
                        if not cylinder_numbers <= set(re.findall(r"\d+", " ".join(groups))):
                            continue
                    score = (shared, start - end, -len(excerpt))
                    if score > best:
                        best = score
                        chosen = hit, excerpt
    if chosen:
        search.add(*chosen)


def _recording_interval(search, question):
    """Ищет период записи по модели прибора и объекту измерения в одном пункте."""
    question = question.translate(_HYPHENS)
    device_code = re.compile(r"\b[a-zа-яё]+-\d+[a-zа-яё\d]*(?![\w-])", re.I)
    devices = set(device_code.findall(question))
    record = r"\b(?:запис|сохран|регистрир)\w*"
    timing = r"\b(?:как часто|между записями|интервал\w*|дискретност\w*|периодичност\w*|период\b|через сколько)"
    if len(devices) != 1 or not re.search(timing, question) or not re.search(record, question):
        return None
    device = devices.pop()
    label = re.compile(rf"(?<![\w-]){re.escape(device)}(?![\w-])", re.I)
    # После удаления формулировки запроса остаются признаки записываемого объекта.
    def subject_words(text):
        text = re.sub(r"\bэбу\b", "блок управления", text.casefold())
        text = re.sub(r"\bданн(?:ые|ых|ым|ыми)\b", "параметры", text)
        return _clause_terms(text)

    subject = re.sub(record + "|" + timing, " ", label.sub(" ", question))
    subject = re.sub(r"\b(?:свою|памят\w*|тестер\w*|сканер\w*|прибор\w*|каким|какой|какова|частот\w*|секунд\w*)\b", " ", subject)
    required = subject_words(subject)
    if not required:
        return "Уточните, какие данные записывает прибор."
    interval = r"\b(?:с (?:дискретностью|периодичностью|интервалом)|каждые|через каждые)\s+\d+(?:[.,]\d+)?\s*(?:мс|с|секунд\w*|минут\w*)\b"
    matches = {}
    for hit in search.primary:
        if not label.search(hit.get("section", "").translate(_HYPHENS)):
            continue
        for sentence in re.split(r";|(?<=[.!?])\s+(?=[А-ЯЁ])", _clean(hit["text"])):
            if any(not label.fullmatch(code) for code in device_code.findall(sentence.translate(_HYPHENS))):
                continue
            for clause in re.split(r",\s*(?:а|но)\s+|\s+и\s+(?=[а-яё]+(?:ть|ет|ют)\b)", sentence, flags=re.I):
                action = re.search(record, clause, re.I)
                magnitude = re.search(interval, clause, re.I)
                if (action and magnitude and action.end() <= magnitude.start()
                        and not re.search(r"\b(?:не|невозмож\w*|недоступ\w*)\b", clause, re.I)
                        and required <= subject_words(clause)):
                    matches[(hit["document_id"], hit["page"], hit.get("condition"), sentence)] = hit
    if len(matches) != 1:
        return "В руководстве не найден однозначный интервал записи для этого прибора и указанных данных."
    (_, _, _, sentence), hit = next(iter(matches.items()))
    for part in hit.get("page_parts", ()):
        # Условие относится к этому же размеченному фрагменту; у него может быть своя страница.
        for prerequisite in re.split(r"(?<=[.!?])\s+", _clean(part["text"])):
            if label.search(prerequisite.translate(_HYPHENS)) and re.search(r"\b(?:необходимо|требуется)\s+установить\b", prerequisite, re.I):
                search.add(dict(hit, **part), prerequisite)
    search.add(hit, sentence)
    return ""


def _generic(search, question, engine):
    """Показывает совпадающее предложение с отличительными словами вопроса."""
    terms = re.findall(r"[а-яёa-z0-9]+(?:[.,-][а-яёa-z0-9]+)*", question.casefold())
    skip = ("как", "где", "како", "каки", "кака", "каку", "какое", "какая", "что", "чего",
            "надо", "нужн", "можно", "двигател", "моей", "модел", "машин", "этой",
            "провери", "проверя", "уточн", "скольк", "долж", "делать", "всех", "искать")
    terms = list(dict.fromkeys(t for t in terms if len(t) >= 3 and t not in engine.casefold()
                               and not t.startswith(skip)))
    codes = [t for t in terms if re.fullmatch(r"\d{2,}\.\d{2,}", t)]
    if codes:
        if re.search(r"сопротивлен|\bом(?:а|ов|ах)?\b", question, re.I):
            pressure = _pressure(question)
            if pressure is None:
                return "Укажите контрольное давление и единицу измерения для проверки датчика."
            value, unit = pressure
            candidates = search.find(f"Датчик {codes[0]}. Давление {value} {unit}. Сопротивление датчика, Ом.")
            if not _table_row(search, candidates, codes[0], pressure):
                if not _table_row(search, search.find(codes[0]), codes[0], pressure):
                    return "В найденных таблицах нет подтверждённой строки для этого датчика при указанном давлении и единице измерения."
            return ""
        candidates = search.find(" ".join(codes))
        for hit in candidates:
            for sentence in re.split(r"(?<=[.!?])\s+(?=[А-ЯЁ])", _clean(hit["text"])):
                if all(c in sentence for c in codes) and len(sentence) <= 650:
                    search.add(hit, sentence)
                    return
    if len(terms) < 2:
        return
    stems = list(dict.fromkeys(_stem(t) for t in terms))
    hits = search.find(" ".join(terms[:7]))
    for hit in hits:
        for sentence in re.split(r"(?<=[.!?])\s+(?=[А-ЯЁ])", _clean(hit["text"])):
            if len(sentence) <= 850 and all(re.search(rf"\b{re.escape(stem)}", sentence, re.I) for stem in stems):
                if hit.get("kind") == "procedure":
                    search.add_procedure(hit)
                else:
                    search.add(hit, sentence, required=False)
                return


def _knock_causes(search):
    """Показывает примеры причин из таблицы, не перенося в ответ колонку ремонта."""
    for hit in search.find("Стуки в двигателе. Вероятная причина"):
        if "стуки в двигателе" not in hit.get("section", "").casefold():
            continue
        if hit["page"] != min(hit.get("source_pages") or [hit["page"]]):
            continue
        for table in hit.get("tables", []):
            rows = table.get("rows", [])
            if not rows:
                continue
            column = next((i for i, cell in enumerate(rows[0])
                           if _clean(cell or "").casefold() in {"вероятная причина", "возможная причина"}), None)
            if column is None:
                continue
            examples = [re.sub(r"^[а-я]\)\s*", "", _clean(row[column])).rstrip(" ;.")
                        for row in rows[1:] if len(row) > column and row[column]][:5]
            if examples:
                excerpt = ". ".join(examples) + "."
                text = ". ".join(example[:1].upper() + example[1:] for example in examples) + "."
                search.add(hit, excerpt, text=text, kind="cause")
                return


def _knock_dialog(search, q):
    """Сопоставляет слова механика с условиями проверки из руководства."""
    _knock_causes(search)
    clauses = re.split(r"[,;.!?]|\bно\b", q)
    affirmative = " ".join(part for part in clauses if not re.search(r"\b(?:не|нет|без)\b", part))
    location = re.search(r"\bкрышк\w*\s+(?:привода\s+)?цепи\b|передн\w*\s+крышк\w*\s+(?:головк\w*|гбц)", affirmative)
    deceleration = re.search(r"\bрезк\w*\s+(?:сброс\w*|снижен\w*)\s+(?:оборот\w*|частот\w*\s+вращен\w*)", affirmative)
    tentative = "?" in q or re.search(r"\b(?:может быть|возможно|кажется|если|или)\b", q)
    if not (location and deceleration) or tentative:
        if re.search(r"\b(?:не могу определить|затрудняюсь|не знаю)\b", q):
            return ("По записи источник стука установить не удалось. Если стук повторится, "
                    "отметьте, откуда он слышен, обороты и нагрузку в этот момент."), True
        return "Источник стука не установлен. Откуда он слышен, при каких оборотах и нагрузке?", True
    found = search.passage(
        "Гидронатяжитель. Стук у крышки цепи при резком сбросе частоты вращения",
        r"Гидронатяжитель подлежит проверке и ремонту при обнаружении стука в зоне передней крышки "
        r"головки цилиндров и крышки цепи\.\s*Стук отч[её]тливо слышен при резком сбросе частоты вращения"
        r".{0,150}?стетофонендоскопа.{0,180}?к пробке крышки верхнего или нижнего гидронатяжителя"
        r".{0,500}?износ зв[её]здочек, повышенная вытяжка цепи, разрушение успокоителя цепи\.",
        explain=lambda _: ("При таком стуке можно проверить гидронатяжитель. Руководство предлагает "
                           "прослушать стук стетофонендоскопом у пробки крышки верхнего или нижнего "
                           "гидронатяжителя. Возможны и другие причины — износ звёздочек, повышенная "
                           "вытяжка цепи или разрушение успокоителя цепи."),
        work="Прослушать стук у пробки крышки верхнего или нижнего гидронатяжителя стетофонендоскопом.")
    clarification = "По словам механика, стук слышен у крышки цепи при резком сбросе оборотов. Причина остаётся неизвестной."
    if not found:
        clarification += " В найденных разделах руководства нет проверки для этих условий."
    return clarification, False


def _oil_thermovalve(search):
    """Находит условия начала подачи масла в радиатор или сноску о комплектации."""
    variant = search.variant
    common = f"{search.engine}|all"
    allowed = {common}
    if variant:
        allowed.add(f"{search.engine}|{variant}")
    if variant != "без термоклапана":
        for hit in search.find("При достижении температуры масла плунжер открывает путь к масляному радиатору"):
            if not allowed.intersection(hit.get("applicability", ())):
                continue
            regime = re.search(r"При давлении масла выше [^.!?]{1,220}?к плунжеру\s*\d*\.\s*"
                               r"При достижении температуры масла [^.!?]{1,500}?перемещать плунжер, "
                               r"открывая путь [^.!?]{1,160}?к масляному радиатору\.", _clean(hit["text"]), re.I)
            if regime:
                search.add(hit, regime[0])
                return "", "", False

    for hit in search.find("Установка термоклапана. Устанавливается на часть двигателей в зависимости от комплектации"):
        if common not in hit.get("applicability", ()) or "термоклапан" not in hit.get("section", "").casefold():
            continue
        footnote = re.search(r"Устанавливается на часть двигателей в зависимости от комплектации", _clean(hit["text"]), re.I)
        if footnote:
            search.add(hit, footnote[0], text=f"Термоклапан {footnote[0].lower()}.")
            if variant == "без термоклапана":
                return "По карточке машины термоклапан отсутствует.", "", False
            if variant != "с термоклапаном":
                return "", "Уточните, установлен ли на этой машине термоклапан.", True
            break
    return "В найденных фрагментах для этой машины нет условий начала открытия термоклапана.", "", False


def _thermostat(search, question):
    """Ищет отдельно температуру открытия и правило эксплуатации без термостата."""
    missing = []
    fully = "полн" in question
    if fully:
        found = search.passage(
            "температура полного открытия клапана термостата",
            r"(?:Температура полного открытия (?:клапана )?термостата|Клапан термостата полностью открывается)"
            r"[^.!?]{0,130}?\d[^.!?]{0,45}?[°º]\s*[СC][^.!?]{0,100}[.]")
        if not found:
            missing.append("В найденных разделах нет температуры полного открытия термостата.")
    if (not fully or re.search(r"начал|начин", question)) and re.search(r"откры|начин|температур|градус|срабатыв", question):
        found = search.passage(
            "клапан термостата открывается температура охлаждающей жидкости радиатор",
            r"Клапан термостата (?:начинает открываться|открывается) при температуре "
            r"[^.!?]{0,100}?\d[^.!?]{0,45}?[°º]\s*[СC](?:,\s*[^.!?]{1,130})?\.")
        if not found:
            missing.append("В найденных разделах нет температуры начала открытия термостата.")
    if re.search(r"\bбез\s+(?:него|термостата)\b", question):
        found = search.passage(
            "эксплуатация двигателя без термостата",
            r"(?:Не допускается эксплуатация двигателя без термостата|Эксплуатация двигателя без термостата запрещена)"
            r"(?:,\s*т\.\s*к\.\s*[^.!?]{1,220})?\.")
        if not found:
            missing.append("В найденных разделах нет указаний об эксплуатации без термостата.")
    return " ".join(missing)


def _cam_sprockets(search, question):
    """Сохраняет условие каждого фрагмента и не угадывает конструкцию привода."""
    drives = {"зубчат": "зубчатыми", "втулоч": "втулочными"}
    named = [stem for stem in drives if stem in question]
    uncertain = re.search(r"неизвест|не знаю|не уверен|неясно|\bне\s+(?:зубчат|втулоч)|\bли\b.{0,12}\bцеп", question)
    requested = named if len(named) == 1 and not uncertain else drives
    missing = []
    for stem in requested:
        chain = drives[stem]
        for hit in search.find(f"Звёздочки распределительных валов. Установочные метки. Привод с {chain} цепями"):
            section = hit.get("section", "").casefold().replace("ё", "е")
            if ("звездоч" not in section or "распределительн" not in section
                    or stem not in hit.get("condition", "").casefold()):
                continue
            excerpt = _clean(hit["text"])
            if "взаимозамен" in excerpt.casefold() and "метк" in excerpt.casefold():
                search.add(hit, excerpt)
                break
        else:
            missing.append(f"В найденных разделах нет данных о взаимозаменяемости звёздочек для привода с {chain} цепями.")
    return " ".join(missing)


def _procedure_part(hits, procedure, part, allowed):
    """Выбирает размеченную выдержку из применимого фрагмента нужной редакции."""
    for hit in hits:
        if (hit["document_id"] != procedure["document_id"] or hit["page"] != part["page"]
                or hit.get("source_sha256") != procedure["source_sha256"]
                or not allowed.intersection(hit.get("applicability", ()))
                or hit.get("condition", "") not in ("", procedure.get("condition", ""))):
            continue
        try:
            excerpt = extract_span(hit["text"], part)
        except ValueError:
            continue  # Другой фрагмент той же физической страницы.
        return dict(hit, condition=procedure.get("condition", "")), excerpt
    return None


def _reviewed_procedure(search, question, procedures):
    """Собирает ответ из найденных частей одной размеченной процедуры в порядке PDF."""
    allowed = {f"{search.engine}|all"}
    if search.variant:
        allowed.add(f"{search.engine}|{search.variant}")
    best = None
    for procedure in procedures:
        if not allowed.intersection(procedure["applicability"]):
            continue
        if not all(any(term in question for term in group) for group in procedure["answer_terms"]):
            continue
        if any(term in question for term in procedure.get("answer_exclude", ())):
            continue
        found, missing = [], []
        for part in procedure["parts"]:
            source = _procedure_part(search.primary, procedure, part, allowed)
            if source is None:
                hits = search.find(f"{part['start']} {part['end']}")
                source = _procedure_part(hits, procedure, part, allowed)
            if source is None:
                missing.append(part["missing"])
            else:
                found.append(source)
        if not missing:
            best = found, missing
            break
        if best is None or len(found) > len(best[0]):
            best = found, missing
    if best is None:
        return None
    found, missing = best
    for hit, excerpt in found:
        search.add(hit, excerpt)
    return " ".join(missing)


_CAMSHAFT = r"\b(?:распредвал\w*|распределительн\w*\s+вал\w*)\b"


def _camshaft_clearance(search):
    """Извлекает место установки щупа и допуск из того же пункта руководства."""
    number = r"\d+(?:[.,]\d+)?"
    tolerance = re.compile(
        rf"(?:допустимый\s+)?зазор\s*(?:должен\s+(?:быть|составлять)|составляет|[—–:-])?\s*"
        rf"(?:от\s+)?{number}\s*(?:[…–—-]|\.{{2,3}}|до)\s*{number}\s*мм\b", re.I)
    target = re.compile(rf"\bосев\w*\s+(?:зазор|люфт)\w*\s+{_CAMSHAFT}", re.I)
    partial = None
    for hit in search.find("Измерить щупом осевой зазор распределительного вала"):
        # В PDF шаги разделены точкой с запятой. Точка в «рис. 82» относится к номеру рисунка.
        steps = re.split(r"(?<=;)\s*|(?<=[.!?])\s+(?=[А-ЯЁ]|\d+[.)]\s)"
                         r"|(?<=,)\s+(?=(?i:(?:допустимый\s+)?зазор)\b)", _clean(hit["text"]))
        for i, step in enumerate(steps):
            if not target.search(step) or not re.search(r"\bщуп\w*\b.*?\bмежду\s+.+?\s+и\s+\S", step, re.I):
                continue
            method = step.lstrip(" -–•")
            method = method[:1].upper() + method[1:]
            if i + 1 < len(steps) and tolerance.match(steps[i + 1]):
                search.add(hit, f"{method} {steps[i + 1]}")
                return ""
            if partial is None:
                partial = hit, method
    if partial:
        search.add(*partial)
        return "Для этого измерения в найденном тексте не указан допустимый зазор в миллиметрах."
    return "В найденных фрагментах нет указания, куда вставить щуп для проверки осевого зазора распредвала."


def retrieve(backend, engine, question, diagnosis=None, topic="", *, variant=None, procedures=()):
    """Разбирает вопрос на поисковые фразы. Модель берётся из карточки машины."""
    q = re.sub(rf"(?<!\w){re.escape(engine)}(?!\w)", "", question, flags=re.I).casefold()
    fault = " ".join(str((diagnosis or {}).get(k) or "") for k in ("anomaly_title", "description")).casefold()
    missing = clarification = ""
    oil = ("масл" in q or "смазк" in q)
    broad = re.search(r"что (?:провер|уточн|делать)|чем опас|в ч[её]м причин|какие причин", q)
    prev_oil = topic in {"oil", "oil-low", "oil-valve", "oil-measurement"}
    # Учитываем диагноз для общего вопроса; явно названный узел задаёт новую тему.
    generic_words = ("что", "как", "где", "это", "этом", "чем", "чего", "при", "для", "мне", "машин",
                     "провер", "уточн", "делать", "водител", "двигател", "причин", "каки", "како",
                     "дальш", "опас", "риск", "сейча", "ещё", "еще", "сначал", "после", "диагност", "этой",
                     "отклонен", "выявлен", "подтвержден")
    words = re.findall(r"[а-яё]{3,}", q)
    contextual = broad and all(t.startswith(generic_words) for t in words)
    fault_name = (diagnosis.get("anomaly_title") or "").casefold() if diagnosis and diagnosis["status"] == "anomaly" else ""
    focus = q.replace(fault_name, " ").strip(" .!?:") if fault_name else q
    use_ml_symptoms = bool(fault_name and (contextual or not focus or generic_diagnostic_question(focus)))
    asks_about_fault = bool(fault_name and (use_ml_symptoms or fault_name in q))
    primary = question
    if use_ml_symptoms:
        primary = "\n".join([question, diagnosis.get("anomaly_title") or "", diagnosis.get("description") or ""])
    code = (diagnosis or {}).get("anomaly_type") if fault_name and diagnosis.get("signal_quality") != "bad" else None
    search = _Search(backend, engine, variant, primary, fault_code=code)
    inspection_missing = False
    needs_clarification = False
    codes = _diagram_codes(question)
    catalog_parts = _catalog_parts(q)
    filter_number = _drain_filter_number(q)
    if catalog_parts:
        topic = "parts-table"
        intro = re.split(r"\b(?:сколько|како[ей]|каков[оа]?)\b", q, maxsplit=1)[0]
        table = re.search(r"\b(?:по|в) таблице\s*(.*)$", intro)
        title_terms = _catalog_terms(table[1]) if table else set()
        unmatched = [name for name, field in catalog_parts if not _catalog_row(search, name, field, title_terms)]
        if unmatched:
            missing = "В найденных таблицах нет однозначной строки для запроса «" + "», «".join(unmatched) + "»."
    elif filter_number:
        topic = "filter-drain"
        if not _drain_filter(search, q, filter_number):
            if not variant or variant == "all":
                needs_clarification = True
                clarification = "Уточните исполнение двигателя по табличке, чтобы подобрать процедуру для указанного фильтра."
            else:
                missing = "Для этого исполнения двигателя и номера фильтра процедура слива воды не найдена."
    elif (legend := select_figure_positions(search, q)) is not None:
        topic = "diagram"
        missing = legend
    elif re.search(r"схем|обознач|расшифр", q) and codes:
        topic = "diagram"
        _diagram(search, codes)
    elif "по вспышкам" in q or "диагностической кнопк" in q:
        topic = "flash-code"
        found = search.passage("диагностической кнопки",
                       r"[^.]{0,100}наж[а-яё ]{1,40}диагностическ.{1,800}?вспыш[а-яё ]{1,60}\.")
        if not found:
            inspection_missing = True
            missing = "В проверенных разделах руководства процедура считывания кода по вспышкам лампы не найдена."
            for hit in search.find(FUEL_SYSTEM):
                if hit["engine_models"] != [engine]:
                    continue
                # Берём проверенные подписи и значения одного столбца таблицы данной модели.
                fields = re.search(r"Топливный насос высокого давления\s*[—–:]\s*[а-яё-]+плунжерный\.\s*"
                                   r"Регулятор частоты вращения\s*[—–:]\s*[а-яё-]+(?:,\s*[а-яё-]+)*\.",
                                   _clean(hit["text"]), re.I)
                if fields:
                    search.add(hit, fields.group())
                    break
    elif (decision := select_defect_card(search, q)) is not None:
        topic = "defect-card"
        missing = decision
    elif (limit := select_component_limit(search, q)) is not None:
        topic = "component-limit"
        missing = limit
    elif re.search(r"\d{2,}\.\d{2,}", q):
        topic = "sensor"
        missing = _generic(search, question, engine) or ""
    elif ("осев" in q and re.search(r"зазор|люфт", q) and re.search(_CAMSHAFT, q)
          and re.search(r"щуп|измер|замер|провер|между", q)):
        topic = "camshaft-clearance"
        missing = _camshaft_clearance(search)
    elif "термоклапан" in q and re.search(r"температур|градус", q) and "полн" not in q:
        topic = "oil-thermovalve"
        missing, clarification, needs_clarification = _oil_thermovalve(search)
    elif select_oil_maintenance(search, q):
        topic = "oil-maintenance"
    elif "клапан" in q and ("зазор" in q or "щуп" in q) and "редукцион" not in q:
        topic = "valve-clearance"
        search.passage("теплового зазора", r"Величина теплового зазора у впускного и выпускного"
                       r".{1,500}?на холодном двигателе")
        if not search.passage("торцом клапана", r"Вставив щуп в зазор.{1,950}?с усилием\."):
            search.passage("торцом клапана", r"Вставив щуп в зазор между[^.]{10,600}?в пределах\s+"
                           r"\d+[,\.]\d+\s*[-–—…]\s*\d+[,\.]\d+\s*мм\.")
    elif "клапан" in q and ("редукцион" in q or "насос" in q or prev_oil and not re.search(r"впуск|выпуск|теплов|зазор", q)):
        topic = "oil-valve"
        _valve(search, q)
    elif (torque := installation_torque(search, q)) is not None:
        topic = "installation-torque"
        missing = torque
    elif select_workshop_instruction(search, q):
        topic = "workshop-instruction"
    elif _disassembly(search, q):
        topic = "disassembly"
    elif _asks_pressure_measurement(q, prev_oil):
        topic = "oil-measurement"
        if not _pressure_measurement(search):
            _generic(search, question, engine)
    elif ("давлен" in q or prev_oil) and (oil or prev_oil) and re.search(r"норматив|номинальн|прогрет|прогре[вт]", q) or (
            "манометр" in q and re.search(r"прогрет|нагрет|номинальн", q) and re.search(r"норм|показан", q)):
        topic = "oil"
        found = search.passage("прогретого номинальной давление",
                               r"Давление масла в системе смазки двигателя, прогретого.{1,300}?"
                               r"холостого хода,? не менее\s+[\d,.]+\s*\([\d,.]+\)")
        if not found:
            missing = "В проверенных разделах норматив давления масла для указанного режима не найден."
            search.passage("сигнализатор аварийного давления",
                           r"Сигнализатор аварийного давления масла,.{1,350}?кгс/см2\)\.",
                           explain=lambda excerpt: f"{excerpt} Это порог срабатывания лампы.")
            _level(search, "general")
    elif re.search(r"ламп|сигнализатор|индикатор", q) and (oil or prev_oil or "давлен" in q) and not broad:
        topic = "oil"
        _lamp(search)
    elif (oil and mentions_pressure_drop(q)
          or contextual and (topic == "oil-low" or "масл" in fault and mentions_pressure_drop(fault))):
        topic = "oil-low"
        causes_only = bool(re.search(r"причин|почему|из-за чего", q) and
                           not re.search(r"провери|уточн|риск|опас|что делать", q))
        _low_pressure(search, causes_only=causes_only)
        if causes_only and not search.evidence:
            missing = "В найденных разделах руководства причины пониженного давления масла не указаны."
        if search.evidence and not causes_only:
            clarification = ("Уточните у водителя показание прибора, единицы измерения, обороты, "
                             "состояние аварийной лампы и был ли перегрев.")
    elif ("термостат" in q and re.search(r"откры|пуска|срабатыв|без (?:него|термостата)", q)
          or topic == "thermostat" and re.fullmatch(r"(?:а\s+)?(?:можно (?:ли )?(?:ездить )?)?без него[?!. ]*", q.strip())):
        topic = "thermostat"
        missing = _thermostat(search, q)
    elif (re.search(r"зв[её]здоч", q) and re.search(r"распредвал|распределительн|впускн|выпускн", q)
          and re.search(r"местами|метк|отлич|взаимозамен|перестав", q)):
        topic = "cam-sprockets"
        missing = _cam_sprockets(search, q)
    elif _asks_oil_grade(q):
        topic = "oil-grade"
        _oil_grades(search)
        if not search.evidence:
            _generic(search, question, engine)
    elif oil and "уров" in q:
        topic = "oil"
        _level(search)
    elif (re.search(r"\bстук[аи]?\s+(?:в\s+)?двигател[ея]\b", q)
          or re.fullmatch(r"\s*(?:что|где) (?:может )?стуч(?:ать|ит)(?: в двигателе)?[?!. ]*", q)
          or (contextual and re.fullmatch(r"стук[и]?\s+(?:в\s+)?двигател[яе]", fault_name))
          or (topic == "engine-knock" and re.search(
              r"не (?:могу|знаю|уверен)|затрудняюсь|крышк|спереди|сзади|при (?:резком|сбросе|нагрузке)|на слух", q)
              and not re.match(r"(?:как|чем|сколько|где|како[йев])\b", q))):
        topic = "engine-knock"
        clarification, needs_clarification = _knock_dialog(search, q)
    elif (reviewed := _reviewed_procedure(search, q, procedures)) is not None:
        topic = "reviewed-procedure"
        missing = reviewed
    elif (interval := _recording_interval(search, q)) is not None:
        topic = "recording-interval"
        missing = interval
    elif (gap := _diagnostic_sensor_gap(search, q)) is not None:
        topic = "diagnostic-table"
        missing = gap
    elif (norm := select_normative_rows(search, q)) is not None:
        topic = "normative-table"
        missing = norm
    else:
        topic = ""
        if search.fault_hits:
            terms = (diagnosis.get("description") or fault_name) if use_ml_symptoms else focus
            select_fault_evidence(search, terms, symptoms=use_ml_symptoms)
        if not search.evidence:
            _generic(search, question, engine)
        if not search.evidence:
            missing = _compound_answer(search, q)
        if not (search.evidence or missing or asks_about_fault):
            _conditional_instruction(search, q.strip())
            if not search.evidence:
                _instruction_context(search, q.strip())
    if not search.evidence and topic in {"oil", "oil-low", "oil-valve"} and not missing:
        _generic(search, question, engine)
    if not search.evidence and topic in {"oil", "oil-low", "oil-valve"} and not missing:
        if _level(search, "general"):
            missing = "Прямой инструкции для этого вопроса не найдено. В руководстве есть общая проверка системы смазки."
    if not search.evidence and "топливн" in q and "аппаратур" in q:
        if search.passage("проверка топливной аппаратуры",
                          r"Проверка топливной аппаратуры[.:]\s*(?:проверьте|осмотрите|убедитесь)[^.]{5,450}\.",
                          kind="general"):
            missing = "Инструкция для запрошенного способа проверки не найдена. Есть общая проверка топливной аппаратуры."
    if fault_name and not asks_about_fault and not search.evidence:
        # Механик может назвать только часть неисправности, например «износ постелей».
        subject = {_stem(word) for word in words
                   if not word.startswith(generic_words + ("чтоб", "подтверд", "гипотез", "неисправност"))}
        fault_terms = {_stem(word) for word in re.findall(r"[а-яё]{3,}", fault_name)}
        asks_about_fault = bool(subject and subject <= fault_terms)
    return dict(topic=topic, queries=search.queries, evidence=search.evidence, missing=missing,
                clarification=clarification, inspection_missing=inspection_missing, asks_about_fault=asks_about_fault,
                needs_clarification=needs_clarification)
