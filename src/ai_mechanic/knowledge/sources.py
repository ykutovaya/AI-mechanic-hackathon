"""Проверка файла и физической страницы для ссылки на руководство."""

from functools import lru_cache
from hashlib import file_digest
from pathlib import Path

import pymupdf


class SourceUnavailable(ValueError):
    """Источник в индексе нельзя подтвердить по реестру и оригиналу."""


@lru_cache(maxsize=32)
def _file_hash(path, modified_ns, changed_ns, size, inode):
    """Пересчитывает SHA-256, когда изменились файл или его атрибуты."""
    with path.open("rb") as source:
        return file_digest(source, "sha256").hexdigest()


def resolve_source(hit, registry, root):
    """Проверяет совпадение редакции, пути и номера физической страницы."""
    doc_id = hit["document_id"]
    rows = [row for row in registry if row["document_id"] == doc_id]
    name = f"{hit['title']} [{doc_id}]"
    if len(rows) != 1:
        raise SourceUnavailable(f"{name}: проверьте идентификатор в реестре.")
    row = rows[0]
    path = (Path(root) / row["path"]).resolve()
    if (any(hit[k] != row[k] for k in ("title", "edition"))
            or (Path(root) / hit["path"]).resolve() != path
            or not set(hit["engine_models"]).issubset(row["engine_models"])):
        raise SourceUnavailable(f"{name}: сведения в индексе и реестре различаются.")
    page = hit["page"]
    try:
        digest = hit.get("source_sha256")
        if not digest or digest != row.get("source_sha256"):
            raise ValueError("контрольная сумма PDF в индексе и реестре различается")
        stat = path.stat()
        if _file_hash(path, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino) != digest:
            raise ValueError("PDF изменился после индексации. Пересоберите индекс этой редакции")
        with pymupdf.open(path) as pdf:
            if not pdf.is_pdf or pdf.needs_pass or pdf.page_count != row.get("page_count", pdf.page_count):
                raise ValueError("не удалось подтвердить PDF из реестра")
            if type(page) is not int or not 1 <= page <= pdf.page_count:
                raise ValueError(f"физическая страница {page} отсутствует")
            pdf.load_page(page - 1)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SourceUnavailable(f"{name}: недоступен источник {path.name} ({exc}).") from exc
    return {"document_id": doc_id, "title": row["title"], "edition": row["edition"],
            "page": page, "url": f"{path.as_uri()}#page={page}"}


def verified_pdf_pages(registry, root, document_id, engine, pages, *, sort=True):
    """Читает физические страницы оригинала после проверки модели, редакции и SHA-256."""
    rows = [row for row in registry if row["document_id"] == document_id and engine in row["engine_models"]]
    if len(rows) != 1 or not pages:
        return {}
    row = rows[0]
    try:
        resolve_source(dict(row, page=pages[0]), registry, root)
        with pymupdf.open(Path(root) / row["path"]) as pdf:
            if any(type(page) is not int or not 1 <= page <= pdf.page_count for page in pages):
                return {}
            return {page: dict(row, engine_models=[engine], page=page, text=pdf[page - 1].get_text(sort=sort))
                    for page in pages}
    except (OSError, RuntimeError, ValueError):
        return {}
