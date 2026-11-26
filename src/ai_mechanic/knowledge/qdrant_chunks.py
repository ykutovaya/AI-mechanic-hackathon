"""Фрагменты руководств с физическими страницами и областью применимости."""

import json
import re
from uuid import NAMESPACE_URL, uuid5


def clean(text):
    """Убирает переносы слов из PDF."""
    return " ".join(re.sub(r"(?<=\w)-[ \t]*\n\s*(?=\w)", "", text).split())


def _paragraphs(text):
    """Читает абзацы и нумерованные шаги, сохраняя их порядок."""
    text = re.sub(r"\n\s*\d+\s*\Z", "", text).strip()
    blocks = re.split(r"\n[ \t]*\n+|\n(?=[ \t]{3,}(?:[А-ЯЁ]|\d+[.)] |[–•] ))", text)
    return [b.strip() for b in blocks if b.strip()]


def _groups(blocks, size=900):
    group, total = [], 0
    for block in blocks:
        if total > 250 and total + len(block) > size:
            yield "\n".join(group)
            group, total = [], 0
        group.append(block)
        total += len(block)
    if group:
        yield "\n".join(group)


def _pairs(models, pairs):
    if pairs is None:
        return [f"{model}|all" for model in models]
    if not isinstance(pairs,list) or any(
        not isinstance(p,str) or len(p.split("|")) != 2 or not p.split("|")[1]
        or p.split("|")[0] not in models for p in pairs
    ):
        raise ValueError("Пара применимости должна содержать модель документа и исполнение")
    return sorted(set(pairs))


def extract_span(text, spec):
    """Возвращает текст между маркерами разметки, сохраняя формулировку источника."""
    if "start" not in spec:
        return text.strip()
    def find(marker, offset):
        match = re.search(r"\s+".join(re.escape(w) for w in marker.split()), text[offset:], re.I)
        if match is None:
            raise ValueError(f"Выгрузка не содержит маркер {marker!r}")
        return offset + match.start(), offset + match.end()
    start = find(spec["start"], 0)[0]
    end = find(spec["end"], start)[1] if spec.get("end") else len(text)
    return text[start:end].strip()


def _chunk(page, section, models, pairs, parts, kind, condition="", related=()):
    if not models or not set(models).issubset(page["engine_models"]):
        raise ValueError("Модели фрагмента должны входить в реестр документа")
    meta = {k:page[k] for k in ("document_id","title","edition","path","source_sha256")}
    text = "\n".join(part["text"] for part in parts)
    pages = sorted({p["page"] for p in parts})
    effective = _pairs(models,pairs)
    chunk = dict(meta, section=section or "Текст руководства", kind=kind, condition=condition,
                 engine_models=models, applicability=effective, page_parts=parts, source_pages=pages,
                 related_pages=sorted(set(related)-set(pages)), text=text)
    chunk["id"] = str(uuid5(NAMESPACE_URL,json.dumps(chunk, sort_keys=True, ensure_ascii=False)))
    chunk["embedding_text"] = "\n".join(filter(None,[section, condition, clean(text)]))
    return chunk


def _table_parts(table, footnotes):
    rows = table.get("rows", [])
    if len(rows) < 2:
        return
    header = [clean(c or "") for c in rows[0]]
    heading = []
    group = []
    previous = ""
    def part(group):
        data = [header, *([heading] if heading else []), *group]
        text = "\n".join(" | ".join(str(cell or "") for cell in row) for row in data)
        if footnotes:
            text += "\n" + "\n".join(footnotes)
        return {"text":text,"tables":[{"rows":data}]}
    for row in rows[1:]:
        cells = [clean(c) if c is not None else None for c in row]
        if len(cells)>1 and cells[0] and not any(cells[1:]):
            if group:
                yield part(group)
            group, heading, previous = [], cells, ""
            continue
        if cells and cells[0] is None and previous:
            cells[0] = previous
        if not any(cells):
            continue
        if len(group) >= 6 or (not heading and len(group) >= 2 and sum(len(str(r)) for r in group)>900):
            yield part(group)
            group = []
        previous = cells[0] or ""
        group.append(cells)
    if group:
        yield part(group)


def _model_restriction(page, allowed):
    body = clean(page["text"]).casefold().replace("–","-").replace("—","-")
    if re.search(r"(?:на части|на отдельных|в зависимости от комплектации|по спецзаказу|при наличии|на двигателях с |на двигателях без |для двигател(?:ей|я) с |(?:могут|может) (?:устанавлива|комплектова)|исполнени[ияюй]|на модификац|комплектаци[ияйюях]|(?:с|без) адаптер(?:ом|а)|для ранних|ранних лет выпуска|втулочны[ехми]+ цеп|зубчаты[ехми]+ цеп)",body):
        return True
    model_pattern = r"(?:ям[з3]|змз)\s*[-‐]?\s*\d{3,}(?:[а-яa-z\d.]|-\d+)*"
    codes = {re.sub(r"[\s-]", "", m.group(0)).rstrip(".") for m in re.finditer(model_pattern, body)}
    values = {re.sub(r"[\s-]", "", model.casefold()) for model in allowed}
    if codes and (len(allowed) > 1 or not codes.issubset(values)):
        return True
    # Разные модели внутри одного листа требуют проверки границ и колонок.
    return (
        len(codes) > 1
        or sum(model.casefold() in body for model in page["engine_models"]) > 1
        or any(
            model.casefold() in body for model in page["engine_models"] if model not in allowed
        )
    )


def _reviewed_sections(annotations, pages):
    scopes = {}
    for note in annotations:
        for scope in note.get("sections",[]):
            pending = False
            for index in range(scope["first"],scope["last"]+1):
                key = (note["document_id"],index)
                if key not in pages:
                    continue
                row = pages[key]
                if note["source_sha256"] != row["source_sha256"]:
                    raise ValueError("Границы раздела относятся к другой редакции PDF")
                if index in scope.get("exclude_pages",[]):
                    pending = True
                    continue
                subset = scope.get("engine_models",row["engine_models"])
                pairs = _pairs(subset,scope.get("applicability"))
                if not pairs or not set(subset).issubset(row["engine_models"]):
                    raise ValueError("Модели раздела должны входить в реестр")
                exact_variant = all(not pair.endswith("|all") for pair in pairs)
                text = row["text"].strip()
                start = text.splitlines()[0].strip() if text else ""
                # Пункт с условиями на прошлом листе может продолжаться на следующем.
                continuation = bool(re.match(r"^[а-яё\d–-]|^(?:После этого|При этом|Затем|Далее|Кроме того)",start))
                if pending and not continuation:
                    pending = False
                if not exact_variant and _model_restriction(row,subset):
                    pending = True
                if not pending and text:
                    scopes[key] = (scope,subset,pairs)
    return scopes


def _numbered_entries(pages, note):
    """Восстанавливает строки с составным номером и продолжения таблицы между листами."""
    rule = note["numbered_table"]
    doc = note["document_id"]
    start = note["page"]
    if (doc,start) not in pages:
        return
    subjects = {}
    for number in rule["contents_pages"]:
        page = pages[doc,number]
        for table in page.get("tables",[]):
            for row in table["rows"]:
                if len(row) == 2 and (row[0] or "").isdigit():
                    subjects[row[0]] = (clean(row[1]), number)
    header = rule["headers"]
    entries = []
    headings = {}
    for number in range(start,rule["last_page"]+1):
        page = pages[doc,number]
        if page["source_sha256"] != note["source_sha256"]:
            raise ValueError("Содержание таблицы относится к другой редакции")
        original = clean(page["text"]).casefold()
        for key,(subject,_) in subjects.items():
            if re.search(r"(?<![\d.])" + key + r"\s+" + re.escape(subject.casefold()),original):
                headings[key] = number
        table_rows = page.get("numbered_rows")
        if table_rows is None:
            table_rows = [r for t in page.get("tables",[]) for r in t["rows"]]
        for cells in table_rows:
            if len(cells) != 2:
                continue
            left, right = (clean(c or "") for c in cells)
            if not (left or right) or [left,right] == header:
                continue
            match = re.match(r"^(\d+)\.(\d+)\s",left)
            if match and match[1] in subjects:
                group, toc = subjects[match[1]]
                entry = {"number":match[0].strip(),"subject":group,"anchor":headings.get(match[1],toc),
                         "parts":{}, "page":page}
                entries.append(entry)
            elif not entries:
                continue
            entry = entries[-1]
            entry["parts"].setdefault(number,[]).append([left,right])
    for entry in entries:
        parts = []
        anchor = entry["anchor"]
        subject = entry["subject"]
        for number, rows in entry["parts"].items():
            table = [header,*rows]
            body = "\n".join(" | ".join(r) for r in table)
            if number == anchor:
                body = subject + "\n" + body
            parts.append(dict(page=number,text=body,tables=[{"rows":table}]))
        if anchor not in entry["parts"]:
            parts.insert(0,dict(page=anchor,text=subject))
        text = "\n".join(p["text"] for p in parts)
        if re.search(r"при наличии\)|на (?:отдельных|части) двигател|в зависимости от комплектации",text,re.I):
            continue
        page = entry["page"]
        result = _chunk(page,rule["section"]+". "+subject,page["engine_models"],None,parts,"table")
        result["entry_number"] = entry["number"]
        yield result


def make_chunks(rows, notes=()):
    """Строит воспроизводимые записи. Ссылки на соседние листы сами не расширяют источник."""
    pages = {(r["document_id"], r["page"]):r for r in rows}
    annotations = {(n["document_id"],n["page"]):n for n in notes}
    scopes = _reviewed_sections(notes,pages)
    common_tables = {}
    for note in notes:
        rule = note.get("common_tables")
        if rule:
            for number in range(note["page"], rule["last_page"] + 1):
                common_tables[note["document_id"],number] = (note["source_sha256"],rule)
    result = {}
    def add(chunk):
        if chunk["applicability"]:
            result[chunk["id"]] = chunk
    for key, page in pages.items():
        doc, number = key
        note = annotations.get(key,page.get("page_note") or {})
        if note and note.get("source_sha256") != page["source_sha256"]:
            raise ValueError(f"{doc}, лист {number}: разметка относится к другой редакции")
        models = page["engine_models"]
        section = note.get("section") or page["section"]
        related = set(page.get("related_pages", [])) | set(note.get("related_pages", []))
        for spec in note.get("passages",[]):
            subset = spec.get("engine_models",models)
            parts = []
            for loc in spec["parts"]:
                orig = pages[doc,loc["page"]]
                if (orig["source_sha256"] != page["source_sha256"] or
                    not set(subset).issubset(orig["engine_models"])):
                    raise ValueError("Продолжение относится к другой редакции или модели")
                body = extract_span(orig["text"],loc)
                if loc.get("verified_text"):
                    body = loc["verified_text"]
                part = dict(page=orig["page"], text=body)
                if "table_rows" in loc:
                    part["tables"] = [{"rows": loc["table_rows"]}]
                parts.append(part)
            add(_chunk(page,spec.get("section",section),subset,spec.get("applicability"),parts,
                       spec.get("kind","procedure"),spec.get("condition",""),related))
        if "fragments" in note:
            fragments = [dict(rule,text=extract_span(page["text"],rule)) for rule in note["fragments"]]
        else:
            fragments = list(page.get("fragments",[page]))
        ocr = page.get("ocr",{})
        review = note.get("verified_ocr")
        if review:
            if not ocr or not review.get("text","").strip():
                raise ValueError("Для проверенного OCR нужен результат распознавания этой страницы")
            ocr = dict(ocr,**review,status="verified")
        if ocr.get("status")=="verified":
            fragments.append(dict(ocr,source="ocr"))
        for item in fragments:
            if item is page and note.get("only_passages"):
                continue
            models_part = item["engine_models"]
            condition = item.get("condition","")
            pairs = item.get("applicability",note.get("applicability"))
            is_ocr = item.get("source")=="ocr"
            if pairs is None and not note.get("reviewed_common"):
                # Сверенный текст сам по себе не подтверждает применимость к исполнению.
                if len(models_part) > 1 or _model_restriction(item, models_part):
                    pairs = []
            item_section = section
            if item is page and key in scopes and not note.get("reviewed_common") and pairs in (None,[]):
                scope, models_part, pairs = scopes[key]
                item_section = scope["title"]
                condition = scope.get("condition","")
            if pairs == [] and (item is not page or key not in common_tables):
                continue
            fragment_links = related | set(item.get("related_pages", []))
            blocks = _paragraphs(item["text"])
            if is_ocr:
                groups = [item["text"]]
            else:
                groups = _groups(blocks)
            for group in groups:
                add(_chunk(page,item_section,models_part,pairs,[dict(page=number,text=group)],
                           "ocr" if is_ocr else "text",condition,fragment_links))
            if item is not page:
                continue
            footnotes = [clean(b) for b in blocks if re.match(r"^(?:\*+|\d+\))\s+\S",b)]
            for table in page.get("tables",[]):
                table_scope = pairs
                table_section = item_section
                if not pairs and key in common_tables:
                    digest, rule = common_tables[key]
                    if page["source_sha256"] != digest:
                        raise ValueError("Разметка общей таблицы относится к другой редакции")
                    if not table.get("rows") or number in rule.get("exclude_pages", []):
                        continue
                    header = [clean(v or "").casefold() for v in table["rows"][0]]
                    if header not in [[v.casefold() for v in h] for h in rule["headers"]]:
                        continue
                    table_scope = _pairs(models_part,None)
                    table_section = rule["section"]
                    allowed_rows = [table["rows"][0]]
                    for cells in table["rows"][1:]:
                        line = clean(" ".join(c or "" for c in cells))
                        if any(m.casefold() in line.casefold() for m in models_part) or re.search(
                            r"комплектац|модификац|при наличии|на части двигател|для модел|[ЯЗ]МЗ\s*[-–]\s*\d",
                            line, re.I
                        ):
                            continue
                        allowed_rows.append(cells)
                    table = {"rows":allowed_rows}
                for part in _table_parts(table,footnotes):
                    add(_chunk(page,table_section,models_part,table_scope,[dict(page=number,**part)],
                               "table",condition,related))
    for note in notes:
        if "numbered_table" in note:
            for chunk in _numbered_entries(pages,note):
                add(chunk)
    return list(result.values())
