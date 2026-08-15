"""Extract text from uploaded documents."""

from __future__ import annotations

from pathlib import Path

import docx
import pdfplumber

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_FILES = 50

SUPPORTED_EXTENSIONS = (".md", ".txt", ".docx", ".pdf")


class UnsupportedFileType(Exception):
    pass


class FileTooLarge(Exception):
    pass


def extract_text(path: Path) -> str:
    """Extract plain text from a supported file. Raises UnsupportedFileType
    for anything else and FileTooLarge if the file exceeds MAX_FILE_BYTES."""
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise FileTooLarge(f"{path.name} is {size} bytes, over the {MAX_FILE_BYTES} byte limit")

    suffix = path.suffix.lower()
    if suffix in (".md", ".txt"):
        return _extract_plain_text(path)
    if suffix == ".docx":
        return _extract_docx(path)
    if suffix == ".pdf":
        return _extract_pdf(path)
    raise UnsupportedFileType(
        f"{path.name}: unsupported file type. Accepted formats: "
        f"{', '.join(SUPPORTED_EXTENSIONS)}"
    )


def _extract_plain_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_docx(path: Path) -> str:
    document = docx.Document(str(path))
    parts: list[str] = []
    for block in _iter_docx_blocks(document):
        if isinstance(block, docx.text.paragraph.Paragraph):
            if block.text.strip():
                parts.append(block.text)
        else:
            parts.append(_docx_table_to_markdown(block))
    return "\n\n".join(parts)


def _iter_docx_blocks(document: docx.Document):
    """Yield paragraphs and tables in document order."""
    parent_elm = document.element.body
    for child in parent_elm.iterchildren():
        if child.tag.endswith("}p"):
            yield docx.text.paragraph.Paragraph(child, document)
        elif child.tag.endswith("}tbl"):
            yield docx.table.Table(child, document)


def _docx_table_to_markdown(table: "docx.table.Table") -> str:
    rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
    if not rows:
        return ""
    header, *body_rows = rows
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in body_rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _extract_pdf(path: Path) -> str:
    pages: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            pages.append(text)
    return "\n\n".join(pages)


def word_count(text: str) -> int:
    return len(text.split())
