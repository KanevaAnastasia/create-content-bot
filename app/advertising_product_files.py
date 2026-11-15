"""Validated product photos and XLSX import (pictures anchored to product rows)."""
from __future__ import annotations

import io
import posixpath
import uuid
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image, ImageOps

PHOTO_ROOT = Path(__file__).resolve().parents[1] / "artifacts" / "advertising_queue"
MAX_FILE_BYTES = 30 * 1024 * 1024
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "p": "http://schemas.openxmlformats.org/package/2006/relationships",
      "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
      "a": "http://schemas.openxmlformats.org/drawingml/2006/main"}


def photo_path(value: str) -> Path:
    path = Path(value).resolve()
    if not path.is_relative_to(PHOTO_ROOT.resolve()) or not path.is_file():
        raise ValueError("Фотография не найдена в хранилище рекламной очереди.")
    return path


def validate_photo(data: bytes) -> bytes:
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("Изображение должно быть не больше 30 МБ.")
    with Image.open(io.BytesIO(data)) as source:
        if source.width * source.height > 40_000_000:
            raise ValueError("Слишком большое разрешение изображения (максимум 40 Мп).")
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((2400, 2400))
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=95)
        return out.getvalue()


def store_photo(data: bytes) -> str:
    normalized = validate_photo(data)
    PHOTO_ROOT.mkdir(parents=True, exist_ok=True)
    path = PHOTO_ROOT / f"{uuid.uuid4()}.jpg"
    path.write_bytes(normalized)
    return str(path)


def _relationships(zf, part):
    directory, name = posixpath.split(part)
    relfile = posixpath.join(directory, "_rels", name + ".rels")
    if relfile not in zf.namelist():
        return {}
    result = {}
    for rel in ET.fromstring(zf.read(relfile)):
        if rel.get("TargetMode") == "External":
            continue
        target = rel.get("Target", "")
        resolved = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(directory, target))
        if resolved.startswith("../"):
            raise ValueError("Некорректная ссылка внутри Excel.")
        result[rel.get("Id")] = resolved
    return result


def read_product_workbook(data: bytes) -> list[dict]:
    """No remote fetching or arbitrary local file paths from spreadsheet cells."""
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("Excel должен быть не больше 30 МБ.")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        if sum(i.file_size for i in zf.infolist()) > 120 * 1024 * 1024 or len(zf.infolist()) > 5000:
            raise ValueError("Слишком большой распакованный Excel.")
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        sheet = workbook.find("m:sheets/m:sheet", NS)
        if sheet is None:
            raise ValueError("Нет листа с товарами.")
        part = _relationships(zf, "xl/workbook.xml")[sheet.get("{" + NS["r"] + "}id")]
        sheet_xml = ET.fromstring(zf.read(part))
        strings = []
        if "xl/sharedStrings.xml" in zf.namelist():
            strings = ["".join(el.itertext()) for el in ET.fromstring(zf.read("xl/sharedStrings.xml"))]
        photos: dict[int, list[bytes]] = {}
        for drawing in sheet_xml.findall("m:drawing", NS):
            drawing_part = _relationships(zf, part)[drawing.get("{" + NS["r"] + "}id")]
            links = _relationships(zf, drawing_part)
            for anchor in ET.fromstring(zf.read(drawing_part)):
                row = anchor.findtext("xdr:from/xdr:row", namespaces=NS)
                if row is None:
                    continue
                for blip in anchor.findall(".//a:blip", NS):
                    image_part = links.get(blip.get("{" + NS["r"] + "}embed"))
                    if image_part:
                        photos.setdefault(int(row) + 1, []).append(validate_photo(zf.read(image_part)))
        rows = []
        headers = None
        for row in sheet_xml.findall("m:sheetData/m:row", NS):
            cells = {}
            for cell in row.findall("m:c", NS):
                column = "".join(c for c in cell.get("r", "") if c.isalpha())
                kind = cell.get("t")
                value = cell.findtext("m:v", default="", namespaces=NS)
                if kind == "s":
                    value = strings[int(value)]
                elif kind == "inlineStr":
                    value = "".join(el.text or "" for el in cell.findall(".//m:t", NS))
                cells[column] = value.strip()
            if headers is None:
                headers = {v.lower(): k for k, v in cells.items()}
                if "название" not in headers:
                    raise ValueError("Первая строка: Название, Картинки, Комментарий.")
                continue
            number = int(row.get("r"))
            title = cells.get(headers["название"], "")
            if not title and not photos.get(number) and not any(cells.values()):
                continue
            if not title or not photos.get(number):
                raise ValueError(f"Строка {number}: нужны название и вставленная фотография (Вставка → Рисунки, над ячейками).")
            if len(photos[number]) > 8:
                raise ValueError(f"Строка {number}: максимум 8 фотографий товара.")
            raw_format = cells.get(headers.get("тип", headers.get("формат", "")), "").lower()
            product_format = {"": "clip", "клип": "clip", "clip": "clip", "пост": "post", "post": "post"}.get(raw_format)
            if product_format is None:
                raise ValueError(f"Строка {number}: тип должен быть Клип или Пост.")
            rows.append({"format": product_format, "title": title, "comment": cells.get(headers.get("комментарий", ""), ""), "photos": photos[number]})
        if not rows:
            raise ValueError("В файле нет товаров с фотографиями.")
        if len(rows) > 200:
            raise ValueError("За один импорт можно добавить не больше 200 товаров.")
        return rows
