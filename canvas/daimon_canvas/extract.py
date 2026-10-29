"""Turns any file into readable text, whatever its extension: PDFs, Office and OpenDocument files, legacy .doc,
RTF, HTML, zips and plain text. Pure stdlib apart from pymupdf."""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .client import strip_html

MAX_TEXT = 2_000_000  # chars; a page-by-page read never needs more than this
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".tif", ".tiff"}
MUPDF_EXTS = {".pdf", ".epub", ".xps", ".oxps", ".fb2", ".mobi", ".cbz"}

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
OD_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
OD_TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"


class Unreadable(Exception):
    """The file has no text that can be pulled out; the message says why."""


def extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        raise Unreadable("It's an image, so there's no text to read.")
    data = path.read_bytes()
    if ext in MUPDF_EXTS or data[:5] == b"%PDF-":
        text = _mupdf(path)
    elif data[:4] == b"PK\x03\x04":
        text = _zip_doc(path, ext)
    elif data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        text = _legacy_office(data)
    elif data[:5] == b"{\\rtf":
        text = _rtf(data.decode("latin-1"))
    else:
        text = _plain(data, ext)
    return text[:MAX_TEXT]


def _plain(data: bytes, ext: str) -> str:
    if b"\x00" in data[:8192] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise Unreadable("It's a binary file with no readable text.")
    for enc in ("utf-8-sig", "utf-16") if data.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("utf-8", errors="replace")
    if ext in (".html", ".htm", ".xhtml") or (ext not in (".md", ".txt") and text.lstrip()[:15].lower().startswith(("<!doctype html", "<html"))):
        return strip_html(text)
    return text


def _mupdf(path: Path) -> str:
    import pymupdf

    try:
        doc = pymupdf.open(path)
    except Exception as e:
        raise Unreadable(f"It couldn't be opened ({e}).")
    pages = [page.get_text().strip() for page in doc]
    if not any(pages):
        raise Unreadable(f"It has {len(pages)} pages but no text layer (probably a scan).")
    label = "Page" if doc.is_pdf else "Section"
    return "\n\n".join(f"## {label} {i + 1}\n{t}" for i, t in enumerate(pages) if t)


def _zip_doc(path: Path, ext: str) -> str:
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise Unreadable(f"It's a damaged archive ({e}).")
    with z:
        names = set(z.namelist())
        if "word/document.xml" in names:
            return _docx(z)
        if "ppt/presentation.xml" in names:
            return _pptx(z)
        if "xl/workbook.xml" in names:
            return _xlsx(z)
        if "content.xml" in names and "mimetype" in names:
            return _odf(z)
        if ext == ".epub" or "META-INF/container.xml" in names:
            return _mupdf(path)
        listing = [f"{i.filename} ({i.file_size} bytes)" for i in z.infolist() if not i.is_dir()]
        return "Zip archive containing:\n" + "\n".join(listing[:200])


def _docx(z: zipfile.ZipFile) -> str:
    body = ET.fromstring(z.read("word/document.xml")).find(f"{W}body")
    out = []
    for el in body if body is not None else []:
        if el.tag == f"{W}p":
            out.append(_docx_para(el))
        elif el.tag == f"{W}tbl":
            for row in el.iter(f"{W}tr"):
                cells = [" ".join(_docx_para(p) for p in tc.iter(f"{W}p")).strip() for tc in row.iter(f"{W}tc")]
                out.append(" | ".join(cells))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _docx_para(p: ET.Element) -> str:
    parts = []
    for el in p.iter():
        if el.tag == f"{W}t":
            parts.append(el.text or "")
        elif el.tag == f"{W}tab":
            parts.append("\t")
        elif el.tag in (f"{W}br", f"{W}cr"):
            parts.append("\n")
    return "".join(parts)


def _pptx(z: zipfile.ZipFile) -> str:
    slides = sorted(
        (n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
        key=lambda n: int(re.search(r"(\d+)\.xml", n).group(1)),
    )
    out = []
    for i, name in enumerate(slides, 1):
        paras = ["".join(t.text or "" for t in p.iter(f"{A}t")) for p in ET.fromstring(z.read(name)).iter(f"{A}p")]
        text = "\n".join(p for p in paras if p.strip())
        out.append(f"## Slide {i}\n{text}")
    return "\n\n".join(out)


def _xlsx(z: zipfile.ZipFile) -> str:
    names = set(z.namelist())
    shared = []
    if "xl/sharedStrings.xml" in names:
        shared = ["".join(t.text or "" for t in si.iter(f"{S}t")) for si in ET.fromstring(z.read("xl/sharedStrings.xml"))]
    rels = {}
    if "xl/_rels/workbook.xml.rels" in names:
        for rel in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")):
            target = rel.get("Target", "").lstrip("/")
            rels[rel.get("Id")] = target if target.startswith("xl/") else f"xl/{target}"
    out = []
    for sheet in ET.fromstring(z.read("xl/workbook.xml")).iter(f"{S}sheet"):
        target = rels.get(sheet.get(f"{R}id"), "")
        if target not in names:
            continue
        rows = []
        for row in ET.fromstring(z.read(target)).iter(f"{S}row"):
            cells = []
            for c in row.iter(f"{S}c"):
                v = c.find(f"{S}v")
                if c.get("t") == "s" and v is not None:
                    cells.append(shared[int(v.text)] if int(v.text) < len(shared) else "")
                elif c.get("t") == "inlineStr":
                    cells.append("".join(t.text or "" for t in c.iter(f"{S}t")))
                else:
                    cells.append(v.text if v is not None and v.text else "")
            if any(cells):
                rows.append(" | ".join(cells))
        out.append(f"## Sheet: {sheet.get('name')}\n" + "\n".join(rows))
    return "\n\n".join(out)


def _odf(z: zipfile.ZipFile) -> str:
    out: list[str] = []

    def walk(el: ET.Element) -> None:
        if el.tag == f"{OD_TABLE}table-row":
            out.append(" | ".join("".join(c.itertext()).strip() for c in el if c.tag == f"{OD_TABLE}table-cell"))
        elif el.tag in (f"{OD_TEXT}p", f"{OD_TEXT}h"):
            out.append("".join(el.itertext()))
        else:
            for child in el:
                walk(child)

    walk(ET.fromstring(z.read("content.xml")))
    return "\n".join(line for line in out if line.strip())


def _legacy_office(data: bytes) -> str:
    """Old binary .doc/.xls/.ppt. There's no parser for these in the stdlib, so this recovers the runs of readable
    text (Word keeps its text as either UTF-16 or cp1252). Formatting is lost but the words come through."""
    text16 = data.decode("utf-16-le", errors="ignore")
    runs16 = re.findall(r"[\t\r\n\x20-\x7e\u00a0-\u024f\u2010-\u2027\u2030-\u203a]{12,}", text16)
    runs8 = re.findall(rb"[\t\r\n\x20-\x7e\x91-\x97\xa0-\xff]{12,}", data)
    text8 = [r.decode("cp1252", errors="ignore") for r in runs8]
    best = runs16 if sum(map(len, runs16)) >= sum(map(len, text8)) else text8
    best = [r.strip() for r in best if sum(ch.isalpha() for ch in r) > len(r) * 0.5]
    if not best:
        raise Unreadable("It's an old-format Office file with no text that could be recovered.")
    text = "\n".join(best).replace("\r", "\n")
    return "(Old-format Office file: text recovered without formatting.)\n" + re.sub(r"\n{3,}", "\n\n", text)


def _rtf(text: str) -> str:
    text = re.sub(r"\\'([0-9a-f]{2})", lambda m: bytes.fromhex(m.group(1)).decode("cp1252", errors="ignore"), text)
    text = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) % 65536), text)
    text = re.sub(r"\\(par|line)\b ?", "\n", text)
    text = re.sub(r"\\tab\b ?", "\t", text)
    text = re.sub(r"\{\\\*[^{}]*\}", "", text)
    text = re.sub(r"\{\\(fonttbl|colortbl|stylesheet|info)[^{}]*(\{[^{}]*\}[^{}]*)*\}", "", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    text = text.replace("{", "").replace("}", "").replace("\\\\", "\\")
    return re.sub(r"\n{3,}", "\n\n", text).strip()
