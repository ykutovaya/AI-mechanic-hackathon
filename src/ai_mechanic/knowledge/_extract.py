"""Чтение физической страницы PDF, ячеек таблиц и выбранных растровых подписей."""

import re
import subprocess

import pymupdf


def _local_heading(page):
    """Ищет короткий выделенный заголовок над основным текстом страницы."""
    for block in page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)["blocks"]:
        for line in block.get("lines", []):
            spans = line["spans"]
            heading = " ".join(" ".join(span["text"].split()) for span in spans).strip()
            # Перенесённая строка внутри блока — продолжение предупреждения.
            if re.match(r"^внимание\b", heading, re.I):
                break
            if not (4 <= len(heading) <= 100) or not any(c.isalpha() for c in heading):
                continue
            left, top, right, _ = line["bbox"]
            if top > page.cropbox.height * .36 or abs((left + right) / 2 - page.cropbox.width / 2) > page.cropbox.width * .13:
                continue
            if re.match(r"(?i)^(рис(?:унок|\.)|табл(?:ица|\.)|продолжение|окончание)", heading):
                continue
            if heading.endswith((".", ",", ";", ":")):
                continue
            if all(sp["flags"] & 16 for sp in spans) or heading.isupper():
                return heading
    return ""


def _tables(page, paths):
    if not paths:
        return []
    found = page.find_tables(strategy="lines_strict", snap_tolerance=4, paths=paths)
    tables = []
    for table in found.tables:
        if table.col_count < 2 or table.row_count < 2:
            continue
        cells = table.extract()
        if not any(cell and cell.strip() for row in cells for cell in row):
            continue
        tables.append({"bbox": list(table.bbox), "rows": cells})
    return tables


def _needs_ocr(page, text, paths):
    """Находит крупные изображения и чертежи со скудным текстовым слоем."""
    bounds = page.rect * page.derotation_matrix
    area = bounds.get_area()
    sparse_text = len("".join(text.split())) <= 200

    for image in page.get_image_info():
        if min(image["width"], image["height"]) < 64:
            continue
        region = pymupdf.Rect(image["bbox"]) & bounds
        coverage = region.get_area() / area
        if coverage >= 0.15 and sparse_text:
            return True
        # На смешанной странице проверяем текст внутри рисунка или скана.
        if coverage >= 0.35 and len("".join(page.get_textbox(region).split())) <= 200:
            return True

    if not sparse_text or len(paths) < 20:
        return False
    drawing = pymupdf.Rect()
    for path in paths:
        drawing.include_rect(path["rect"])
    return (drawing & bounds).get_area() >= area * 0.2


def _recognize(page, language, rotation):
    longest_side = max(page.rect.width, page.rect.height)
    scale = min(300 / 72, 6000 / max(longest_side, 1))
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale).prerotate(rotation),
                          colorspace=pymupdf.csGRAY, alpha=False)
    try:
        result = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", language],
            input=pix.tobytes("png"), capture_output=True, timeout=180,
        )
    except FileNotFoundError as exc:
        raise ValueError("Для OCR установите Tesseract и добавьте его в PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"Истекло время OCR страницы {page.number + 1}") from exc
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"OCR страницы {page.number + 1}: {message}. Проверьте tesseract --list-langs")
    return {"status": "pending", "language": language, "rotate": rotation,
            "text": result.stdout.decode("utf-8").strip()}


def _normalize_page_rotation(page):
    """Учитывает поворот PDF, если он выравнивает основной текст страницы."""
    if not page.rotation:
        return
    matrix = page.rotation_matrix
    text_flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    direction_change = 0
    for block in page.get_text("dict", flags=text_flags)["blocks"]:
        for line in block.get("lines", []):
            x, y = line["dir"]
            count = sum(len(span["text"].strip()) for span in line["spans"])
            direction_change += (x * matrix.a + y * matrix.c - x) * count
    if direction_change > 0:
        page.remove_rotation()


def iter_pages(pdf_path, *, ocr_pages=(), ocr_language="rus", ocr_rotate=0, ocr_auto=False):
    """Возвращает все физические страницы с 1, включая страницы без текстового слоя."""
    with pymupdf.open(pdf_path) as doc:
        if not doc.is_pdf or doc.needs_pass:
            raise ValueError(f"Нужен доступный для чтения PDF: {pdf_path}")
        selected = set(ocr_pages)
        if any(type(n) is not int or not 1 <= n <= len(doc) for n in selected):
            raise ValueError(f"Недопустимый номер страницы OCR в {pdf_path}")
        outline = {p: title.strip() for _, title, p in doc.get_toc() if 1 <= p <= len(doc)}
        section = ""
        for page in doc:
            _normalize_page_rotation(page)
            number = page.number + 1
            text = page.get_text("text", sort=True).strip()
            heading = outline.get(number) or _local_heading(page)
            if heading:
                section = heading
            row = {"page": number, "text": text, "section": section}
            paths = page.get_drawings()
            tables = _tables(page, paths)
            if tables:
                row["tables"] = tables
            if number in selected or (ocr_auto and _needs_ocr(page, text, paths)):
                row["ocr"] = _recognize(page, ocr_language, ocr_rotate)
            yield row
