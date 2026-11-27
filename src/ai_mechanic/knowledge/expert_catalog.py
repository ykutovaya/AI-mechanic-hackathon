"""Указатель физических страниц по коду диагностики и применимости двигателя."""

import re
from uuid import NAMESPACE_URL, uuid5


def catalog_id(code, pair):
    return str(uuid5(NAMESPACE_URL, f"ai-mechanic/expert-catalog/{code}/{pair}"))


def catalog_records(report, registry):
    """Группирует доступные строки. Страницы для сверки хранятся без текста выдержек."""
    groups = {}
    for item in report["rows"]:
        if item["result"] == "bound":
            level, pages = "bound", item["source_pages"]
        elif item.get("routes"):
            level, pages = "route", sorted({r["page"] for r in item["routes"]})
        elif item.get("reading_reference"):
            level, pages = "reading", item["reading_reference"]["source_pages"]
        else:
            continue
        doc = registry[item["document_id"]]
        pair = item["engine_model"] + "|" + (item.get("variant") or "all")
        code = item["code"]
        key = catalog_id(code, pair)
        group = groups.setdefault(key, dict(id=key, kind="expert_catalog", code=code,
                                            applicability=[pair], entries=[]))
        group["entries"].append(dict(row=item["row"], section=item["section"], level=level,
                                     source_pages=pages, document_id=doc["document_id"],
                                     title=doc["title"], edition=doc["edition"], path=doc["path"],
                                     source_sha256=doc["source_sha256"], engine_models=[item["engine_model"]],
                                     applicability=[pair]))
    return list(groups.values())


def requests_references(question):
    """Распознаёт запрос списка источников по полученной диагностике."""
    return bool(re.fullmatch(
        r"(?:покаж\w*|дайте?|выве\w*|откр\w*|какие|где посмотреть)?\s*(?:все\s+|список\s+)?"
        r"(?:источник\w*|страниц\w*|раздел\w*|ссылк\w*)(?:\s+(?:pdf|руководств\w*|на руководств\w*))?"
        r"(?:\s+(?:по|для)\s+(?:(?:этой|этому|текущей|данной)\s+)?(?:неисправности|отклонению|диагнозу|проверке|коду))?"
        r"[?.!\s]*", question.strip(), re.I))
