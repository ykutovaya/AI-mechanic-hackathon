"""Находит проверку СРОГ ЗМЗ-5234.10 и строку о заслонке РОГ ЯМЗ-536."""

from pathlib import Path
import re

import pymupdf
from qdrant_client.http.exceptions import ApiException

from .qdrant_chunks import clean
from .sources import SourceUnavailable, resolve_source


_ZMZ = "ЗМЗ-5234.10"
_Z_DOC = "zmz-5234-10-rk-2025"
_ROD_WORK = ("Проверить перемещение штока клапана рециркуляции на прогретом двигателе "
             "при повышении частоты от минимальных оборотов холостого хода до 1700 мин⁻¹.")
_FLAP_WORK = "Проверить подвижность заслонки системы рециркуляции отработавших газов (РОГ)."
_ROD = re.compile(
    r"Для проверки работоспособности СРОГ необходимо увеличить на прогретом двигателе "
    r"частоту вращения коленчатого вала с минимальных оборотов холостого хода до 1700 мин[-−⁻][1¹] "
    r"и наблюдать визуально за перемещением штока 9,? клапана рециркуляции 8\.", re.I)
_VACUUM = re.compile(r"В случае отсутствия перемещения штока проверить наличие разрежения в шланге 6 "
                     r"от термовакуумного включателя к клапану рециркуляции 8\.", re.I)
_TEMPERATURE = re.compile(r"Система рециркуляции отработавших газов \(СРОГ\) срабатывает на двигателе, "
                          r"прогретом до температуры охлаждающей жидкости не ниже плюс 35…40 °С, "
                          r"на частичных нагрузках\.", re.I)


def _rod_excerpt(text):
    """Сохраняет условия и последовательность проверки из нативного текста."""
    text = clean(text)
    step = _ROD.search(text)
    if not step:
        return ""
    warm = _TEMPERATURE.search(text, 0, step.start())
    vacuum = _VACUUM.match(text[step.end():].lstrip())
    return " ".join(match.group() for match in (warm, step, vacuum) if match)


def select_egr_inspection(search, question, use_ml_symptoms):
    """Для F11 выбирает первоначальную проверку именно системы рециркуляции."""
    if search.engine not in {_ZMZ, "ЯМЗ-536"}:
        return None
    if not use_ml_symptoms and not (re.search(r"рециркуляц|\b(?:egr|срог|рог)\b", question)
                                    and re.search(r"провер|подвижност|заедан|перемещен|работоспособ", question)):
        return None
    if search.engine == _ZMZ:
        if search.manual_type and not search.manual_type.search("Руководство по ремонту"):
            return None
        for hit in search.find("Для проверки работоспособности СРОГ увеличить частоту до 1700 "
                               "и наблюдать за перемещением штока клапана рециркуляции"):
            if hit["document_id"] == _Z_DOC and (excerpt := _rod_excerpt(hit["text"])):
                search.add(hit, excerpt, work=_ROD_WORK)
                return ""
        return "Не удалось подтвердить по руководству порядок проверки штока клапана рециркуляции."

    for hit in search.find("Затруднена подвижность заслонки системы рециркуляции отработавших газов РОГ"):
        if hit["document_id"] != "yamz-536-re-2016":
            continue
        for table in hit.get("tables", ()):
            for row in table.get("rows", ()):
                if len(row) != 2:
                    continue
                cause, remedy = (clean(cell or "") for cell in row)
                if (re.fullmatch(r"3\.15 Затруднена подвижность заслонки системы рециркуляции "
                                 r"отработавших газов \(РОГ\)\.?", cause, re.I)
                        and remedy.casefold().startswith("устранить заедание")):
                    search.add(hit, cause + " | " + remedy, work=_FLAP_WORK,
                               text=f"Пункт 3.15. {_FLAP_WORK} При обнаружении заедания устраните его.")
                    return ""
    return "В найденных строках руководства нет проверки подвижности заслонки РОГ."


def add_catalog_egr_evidence(result, backend, engine, variant, registry, root):
    """Читает страницу по применимой ссылке из экспертного указателя, если фрагмента нет в индексе."""
    if engine != _ZMZ or not callable(getattr(backend, "reference_entries", None)):
        return
    allowed = {engine + "|all"}
    if variant:
        allowed.add(engine + "|" + variant)
    try:
        entries = backend.reference_entries(engine, "F11", variant=variant)
    except (ApiException, OSError, ValueError, RuntimeError):
        return
    for entry in entries:
        if (entry["document_id"] != _Z_DOC or engine not in entry["engine_models"]
                or not allowed.intersection(entry.get("applicability", ()))
                or "рециркуляц" not in entry.get("section", "").casefold()):
            continue
        for page in entry["source_pages"]:
            hit = dict(entry, page=page)
            try:
                resolve_source(hit, registry, root)
                with pymupdf.open(Path(root) / hit["path"]) as pdf:
                    excerpt = _rod_excerpt(pdf[page - 1].get_text(sort=True))
            except (SourceUnavailable, OSError, RuntimeError, ValueError):
                continue
            if excerpt:
                result["evidence"].append(dict(hit, text=excerpt, excerpt=excerpt, work=_ROD_WORK, kind="fact",
                                               required=True, lamp=False, fault_code="F11", expert_rows=[entry["row"]]))
                result["missing"] = ""
                return
