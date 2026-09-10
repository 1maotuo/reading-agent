"""Uniform parsers and source-anchor resolution for F-03-01."""

from __future__ import annotations

import hashlib
import posixpath
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree as ET

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .errors import DocumentParseError
from .models import (
    Block,
    Chapter,
    DocumentFormat,
    ParsedDocument,
    Section,
    SourceAnchor,
    SourceLocator,
)


PIPELINE_VERSION = "f03-01-v1"
MAX_SOURCE_BYTES = 50 * 1024 * 1024
MAX_EPUB_MEMBERS = 2_000
MAX_EPUB_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
ANCHOR_CONTEXT_CHARS = 64

_FORMAT_BY_SUFFIX: dict[str, DocumentFormat] = {
    ".pdf": "pdf",
    ".epub": "epub",
    ".txt": "txt",
    ".md": "markdown",
    ".markdown": "markdown",
}
_LIST_PATTERN = re.compile(r"^\s*(?:[-*+] |\d+[.)]\s+)(.+?)\s*$")
_CHINESE_CHAPTER_PATTERN = re.compile(
    r"^第[一二三四五六七八九十百零〇0-9]+[章篇部卷]\s*.*$"
)
_CHINESE_SECTION_PATTERN = re.compile(
    r"^第[一二三四五六七八九十百零〇0-9]+节\s*.*$"
)
_ENGLISH_CHAPTER_PATTERN = re.compile(r"^(?:chapter|part)\s+[\w.-]+(?:\s+.*)?$", re.I)
_ENGLISH_SECTION_PATTERN = re.compile(r"^section\s+[\w.-]+(?:\s+.*)?$", re.I)


@dataclass(frozen=True)
class _RawBlock:
    kind: str
    text: str
    locator: SourceLocator
    heading_level: int | None = None


@dataclass
class _PendingSection:
    title: str
    level: int
    locator: SourceLocator | None
    blocks: list[_RawBlock]


@dataclass
class _PendingChapter:
    title: str
    locator: SourceLocator | None
    sections: list[_PendingSection]


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", value).strip()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _stable_id(prefix: str, *parts: object) -> str:
    canonical = "\x1f".join(str(part) for part in parts)
    return f"{prefix}_{_sha256_text(canonical)[:24]}"


def _safe_read_bytes(path: Path) -> bytes:
    if not path.exists() or not path.is_file():
        raise DocumentParseError("FILE_NOT_FOUND", "文件不存在或不是普通文件")
    size = path.stat().st_size
    if size == 0:
        raise DocumentParseError("EMPTY_FILE", "文件为空")
    if size > MAX_SOURCE_BYTES:
        raise DocumentParseError(
            "FILE_TOO_LARGE",
            "文件超过本阶段允许的大小",
            max_bytes=MAX_SOURCE_BYTES,
            actual_bytes=size,
        )
    return path.read_bytes()


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DocumentParseError(
        "UNSUPPORTED_ENCODING",
        "文本编码无法识别；当前支持UTF-8、UTF-16和GB18030",
    )


def _heading_level(text: str) -> int | None:
    text = normalize_text(text)
    if _CHINESE_CHAPTER_PATTERN.match(text) or _ENGLISH_CHAPTER_PATTERN.match(text):
        return 1
    if _CHINESE_SECTION_PATTERN.match(text) or _ENGLISH_SECTION_PATTERN.match(text):
        return 2
    return None


def _plain_lines_to_blocks(
    lines: list[str],
    locator_factory: Callable[[int, int, int], SourceLocator],
) -> list[_RawBlock]:
    """Turn plain/layout lines into semantic blocks with conservative heuristics."""

    raw_blocks: list[_RawBlock] = []
    paragraph_lines: list[str] = []
    paragraph_start = 0

    def flush_paragraph(end_index: int) -> None:
        nonlocal paragraph_lines, paragraph_start
        text = normalize_text(" ".join(paragraph_lines))
        if text:
            raw_blocks.append(
                _RawBlock(
                    kind="paragraph",
                    text=text,
                    locator=locator_factory(
                        paragraph_start,
                        end_index,
                        len(raw_blocks),
                    ),
                )
            )
        paragraph_lines = []

    for index, original_line in enumerate(lines):
        line = normalize_text(original_line)
        if not line:
            if paragraph_lines:
                flush_paragraph(index - 1)
            continue

        level = _heading_level(line)
        list_match = _LIST_PATTERN.match(original_line)
        if level is not None:
            if paragraph_lines:
                flush_paragraph(index - 1)
            raw_blocks.append(
                _RawBlock(
                    kind="heading",
                    text=line,
                    locator=locator_factory(index, index, len(raw_blocks)),
                    heading_level=level,
                )
            )
            continue

        if list_match:
            if paragraph_lines:
                flush_paragraph(index - 1)
            raw_blocks.append(
                _RawBlock(
                    kind="list_item",
                    text=normalize_text(list_match.group(1)),
                    locator=locator_factory(index, index, len(raw_blocks)),
                )
            )
            continue

        if not paragraph_lines:
            paragraph_start = index
        paragraph_lines.append(line)
        if re.search(r"[。！？.!?][\"'”’）)]?$", line):
            flush_paragraph(index)

    if paragraph_lines:
        flush_paragraph(len(lines) - 1)
    return raw_blocks


def _extract_markdown(data: bytes) -> tuple[str | None, list[_RawBlock]]:
    text = _decode_text(data)
    lines = text.splitlines()
    raw_blocks: list[_RawBlock] = []
    paragraph_lines: list[str] = []
    paragraph_start = 0
    title: str | None = None

    def locator(start: int, end: int, _: int) -> SourceLocator:
        return SourceLocator(line_start=start + 1, line_end=end + 1)

    def flush_paragraph(end_index: int) -> None:
        nonlocal paragraph_lines, paragraph_start
        normalized = normalize_text(" ".join(paragraph_lines))
        if normalized:
            raw_blocks.append(
                _RawBlock(
                    kind="paragraph",
                    text=normalized,
                    locator=locator(paragraph_start, end_index, len(raw_blocks)),
                )
            )
        paragraph_lines = []

    for index, original_line in enumerate(lines):
        heading_match = re.match(r"^\s*(#{1,6})\s+(.+?)\s*#*\s*$", original_line)
        list_match = _LIST_PATTERN.match(original_line)
        if heading_match:
            if paragraph_lines:
                flush_paragraph(index - 1)
            level = len(heading_match.group(1))
            heading_text = normalize_text(heading_match.group(2))
            if title is None and level == 1:
                title = heading_text
            raw_blocks.append(
                _RawBlock(
                    kind="heading",
                    text=heading_text,
                    locator=locator(index, index, len(raw_blocks)),
                    heading_level=level,
                )
            )
        elif list_match:
            if paragraph_lines:
                flush_paragraph(index - 1)
            raw_blocks.append(
                _RawBlock(
                    kind="list_item",
                    text=normalize_text(list_match.group(1)),
                    locator=locator(index, index, len(raw_blocks)),
                )
            )
        elif not original_line.strip():
            if paragraph_lines:
                flush_paragraph(index - 1)
        else:
            if not paragraph_lines:
                paragraph_start = index
            paragraph_lines.append(original_line)

    if paragraph_lines:
        flush_paragraph(len(lines) - 1)
    return title, raw_blocks


def _extract_txt(data: bytes) -> tuple[str | None, list[_RawBlock]]:
    text = _decode_text(data)
    lines = text.splitlines()

    def locator(start: int, end: int, _: int) -> SourceLocator:
        return SourceLocator(line_start=start + 1, line_end=end + 1)

    blocks = _plain_lines_to_blocks(lines, locator)
    title = next((item.text for item in blocks if item.heading_level == 1), None)
    return title, blocks


def _extract_pdf(path: Path) -> tuple[str | None, list[_RawBlock]]:
    try:
        reader = PdfReader(str(path), strict=False)
    except (PdfReadError, OSError, ValueError) as exc:
        raise DocumentParseError("CORRUPT_PDF", "PDF损坏或结构无法读取") from exc

    if reader.is_encrypted:
        raise DocumentParseError("ENCRYPTED_PDF", "PDF已加密，当前阶段不接收加密文件")

    raw_blocks: list[_RawBlock] = []
    for page_index, page in enumerate(reader.pages):
        try:
            extracted = page.extract_text() or ""
        except Exception as exc:  # pypdf exposes several backend-specific errors.
            raise DocumentParseError(
                "PDF_TEXT_EXTRACTION_FAILED",
                "PDF文本提取失败",
                page=page_index + 1,
            ) from exc

        lines = extracted.splitlines()

        def locator(start: int, end: int, element_index: int) -> SourceLocator:
            return SourceLocator(
                page=page_index + 1,
                line_start=start + 1,
                line_end=end + 1,
                element_index=element_index,
            )

        raw_blocks.extend(_plain_lines_to_blocks(lines, locator))

    if not any(item.kind != "heading" and item.text for item in raw_blocks):
        raise DocumentParseError(
            "NO_EXTRACTABLE_TEXT",
            "PDF中没有可提取文本；它可能是扫描件",
        )

    metadata = reader.metadata
    title = normalize_text(str(metadata.title)) if metadata and metadata.title else None
    return title, raw_blocks


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _safe_epub_member(base: str, relative: str) -> str:
    parsed = urlparse(relative)
    if parsed.scheme or parsed.netloc:
        raise DocumentParseError("UNSAFE_EPUB_PATH", "EPUB引用了外部资源")
    member = posixpath.normpath(posixpath.join(base, unquote(parsed.path)))
    if member.startswith("../") or member == ".." or member.startswith("/"):
        raise DocumentParseError("UNSAFE_EPUB_PATH", "EPUB内部路径越界")
    return member


def _epub_semantic_elements(root: ET.Element) -> Iterable[tuple[str, ET.Element]]:
    body = next((element for element in root.iter() if _local_name(element.tag) == "body"), root)

    def walk(element: ET.Element, inside_list_item: bool = False) -> Iterable[tuple[str, ET.Element]]:
        tag = _local_name(element.tag)
        is_list_item = tag == "li"
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6", "li"}:
            yield tag, element
            if tag == "li":
                return
        elif tag in {"p", "blockquote"} and not inside_list_item:
            yield tag, element
            return
        for child in list(element):
            yield from walk(child, inside_list_item or is_list_item)

    yield from walk(body)


def _read_epub_package(
    archive: zipfile.ZipFile,
) -> tuple[str | None, list[tuple[str, bytes]]]:
    infos = archive.infolist()
    if len(infos) > MAX_EPUB_MEMBERS:
        raise DocumentParseError("EPUB_TOO_COMPLEX", "EPUB内部文件数量超过限制")
    if sum(info.file_size for info in infos) > MAX_EPUB_UNCOMPRESSED_BYTES:
        raise DocumentParseError("EPUB_TOO_LARGE", "EPUB解压后大小超过限制")

    try:
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        rootfile = next(
            element
            for element in container.iter()
            if _local_name(element.tag) == "rootfile"
        )
        package_path = rootfile.attrib["full-path"]
        package_root = ET.fromstring(archive.read(package_path))
    except (KeyError, StopIteration, ET.ParseError) as exc:
        raise DocumentParseError("CORRUPT_EPUB", "EPUB缺少有效的package结构") from exc

    package_dir = posixpath.dirname(package_path)
    title: str | None = None
    for element in package_root.iter():
        if _local_name(element.tag) == "title" and normalize_text(element.text or ""):
            title = normalize_text(element.text or "")
            break

    manifest: dict[str, tuple[str, str]] = {}
    for element in package_root.iter():
        if _local_name(element.tag) != "item":
            continue
        item_id = element.attrib.get("id")
        href = element.attrib.get("href")
        media_type = element.attrib.get("media-type", "")
        if item_id and href:
            manifest[item_id] = (_safe_epub_member(package_dir, href), media_type)

    ordered_documents: list[tuple[str, bytes]] = []
    for element in package_root.iter():
        if _local_name(element.tag) != "itemref":
            continue
        idref = element.attrib.get("idref")
        if not idref or idref not in manifest:
            continue
        href, media_type = manifest[idref]
        if media_type not in {"application/xhtml+xml", "text/html"}:
            continue
        try:
            ordered_documents.append((href, archive.read(href)))
        except KeyError as exc:
            raise DocumentParseError(
                "CORRUPT_EPUB",
                "EPUB spine引用了不存在的文档",
                href=href,
            ) from exc
    return title, ordered_documents


def _extract_epub(path: Path) -> tuple[str | None, list[_RawBlock]]:
    if not zipfile.is_zipfile(path):
        raise DocumentParseError("CORRUPT_EPUB", "文件不是有效的EPUB压缩包")

    raw_blocks: list[_RawBlock] = []
    try:
        with zipfile.ZipFile(path) as archive:
            title, documents = _read_epub_package(archive)
            for href, payload in documents:
                try:
                    root = ET.fromstring(payload)
                except ET.ParseError as exc:
                    raise DocumentParseError(
                        "CORRUPT_EPUB_XHTML",
                        "EPUB正文不是有效的XHTML",
                        href=href,
                    ) from exc
                for element_index, (tag, element) in enumerate(_epub_semantic_elements(root)):
                    text = normalize_text("".join(element.itertext()))
                    if not text:
                        continue
                    heading_level = int(tag[1]) if tag.startswith("h") else None
                    kind = "heading" if heading_level else "list_item" if tag == "li" else "paragraph"
                    raw_blocks.append(
                        _RawBlock(
                            kind=kind,
                            text=text,
                            locator=SourceLocator(
                                href=href,
                                element_index=element_index,
                            ),
                            heading_level=heading_level,
                        )
                    )
    except zipfile.BadZipFile as exc:
        raise DocumentParseError("CORRUPT_EPUB", "EPUB压缩结构损坏") from exc

    if not any(item.kind != "heading" and item.text for item in raw_blocks):
        raise DocumentParseError("NO_EXTRACTABLE_TEXT", "EPUB中没有可提取正文")
    return title, raw_blocks


def _build_document(
    *,
    source_name: str,
    source_format: DocumentFormat,
    source_sha256: str,
    extracted_title: str | None,
    raw_blocks: list[_RawBlock],
) -> ParsedDocument:
    default_chapter = _PendingChapter(
        title="正文",
        locator=None,
        sections=[_PendingSection(title="正文", level=1, locator=None, blocks=[])],
    )
    pending_chapters = [default_chapter]
    current_chapter = default_chapter
    current_section = default_chapter.sections[0]

    for raw in raw_blocks:
        if raw.heading_level == 1:
            current_chapter = _PendingChapter(
                title=raw.text,
                locator=raw.locator,
                sections=[
                    _PendingSection(
                        title=raw.text,
                        level=1,
                        locator=raw.locator,
                        blocks=[],
                    )
                ],
            )
            pending_chapters.append(current_chapter)
            current_section = current_chapter.sections[0]
        elif raw.heading_level is not None:
            current_section = _PendingSection(
                title=raw.text,
                level=raw.heading_level,
                locator=raw.locator,
                blocks=[],
            )
            current_chapter.sections.append(current_section)
        else:
            current_section.blocks.append(raw)

    pending_chapters = [
        chapter
        for chapter in pending_chapters
        if any(section.blocks for section in chapter.sections)
    ]
    if not pending_chapters:
        raise DocumentParseError("NO_EXTRACTABLE_TEXT", "文档中没有可用于阅读的正文")

    flat_pending_blocks = [
        raw
        for chapter in pending_chapters
        for section in chapter.sections
        for raw in section.blocks
    ]
    document_id = f"doc_{source_sha256[:24]}"
    block_ordinal = 0
    chapters: list[Chapter] = []

    for chapter_ordinal, pending_chapter in enumerate(pending_chapters):
        chapter_id = _stable_id(
            "chap",
            PIPELINE_VERSION,
            source_sha256,
            chapter_ordinal,
            pending_chapter.title,
        )
        sections: list[Section] = []
        for section_ordinal, pending_section in enumerate(pending_chapter.sections):
            if not pending_section.blocks:
                continue
            section_id = _stable_id(
                "sec",
                PIPELINE_VERSION,
                source_sha256,
                chapter_ordinal,
                section_ordinal,
                pending_section.title,
            )
            blocks: list[Block] = []
            for raw in pending_section.blocks:
                previous_text = (
                    flat_pending_blocks[block_ordinal - 1].text if block_ordinal > 0 else ""
                )
                next_text = (
                    flat_pending_blocks[block_ordinal + 1].text
                    if block_ordinal + 1 < len(flat_pending_blocks)
                    else ""
                )
                content_sha256 = _sha256_text(raw.text)
                block_id = _stable_id(
                    "blk",
                    PIPELINE_VERSION,
                    source_sha256,
                    block_ordinal,
                    chapter_id,
                    section_id,
                    content_sha256,
                )
                blocks.append(
                    Block(
                        block_id=block_id,
                        ordinal=block_ordinal,
                        kind=raw.kind,
                        text=raw.text,
                        content_sha256=content_sha256,
                        chapter_id=chapter_id,
                        section_id=section_id,
                        source_anchor=SourceAnchor(
                            locator=raw.locator,
                            exact_quote=raw.text,
                            prefix=previous_text[-ANCHOR_CONTEXT_CHARS:],
                            suffix=next_text[:ANCHOR_CONTEXT_CHARS],
                        ),
                    )
                )
                block_ordinal += 1
            sections.append(
                Section(
                    section_id=section_id,
                    ordinal=section_ordinal,
                    title=pending_section.title,
                    level=pending_section.level,
                    path=[pending_chapter.title, pending_section.title],
                    blocks=blocks,
                )
            )
        chapters.append(
            Chapter(
                chapter_id=chapter_id,
                ordinal=chapter_ordinal,
                title=pending_chapter.title,
                sections=sections,
            )
        )

    title = normalize_text(extracted_title or "") or chapters[0].title or Path(source_name).stem
    return ParsedDocument(
        pipeline_version=PIPELINE_VERSION,
        document_id=document_id,
        source_name=source_name,
        source_format=source_format,
        source_sha256=source_sha256,
        title=title,
        chapters=chapters,
    )


def parse_document(path: str | Path) -> ParsedDocument:
    source_path = Path(path).resolve()
    suffix = source_path.suffix.lower()
    source_format = _FORMAT_BY_SUFFIX.get(suffix)
    if source_format is None:
        raise DocumentParseError(
            "UNSUPPORTED_FORMAT",
            "当前只支持PDF、EPUB、TXT和Markdown",
            suffix=suffix,
        )

    data = _safe_read_bytes(source_path)
    source_sha256 = _sha256_bytes(data)
    if source_format == "pdf":
        title, raw_blocks = _extract_pdf(source_path)
    elif source_format == "epub":
        title, raw_blocks = _extract_epub(source_path)
    elif source_format == "markdown":
        title, raw_blocks = _extract_markdown(data)
    else:
        title, raw_blocks = _extract_txt(data)

    return _build_document(
        source_name=source_path.name,
        source_format=source_format,
        source_sha256=source_sha256,
        extracted_title=title,
        raw_blocks=raw_blocks,
    )


def _resolve_epub_element(path: Path, href: str, element_index: int) -> str:
    with zipfile.ZipFile(path) as archive:
        payload = archive.read(href)
    root = ET.fromstring(payload)
    elements = list(_epub_semantic_elements(root))
    if element_index >= len(elements):
        raise DocumentParseError("ANCHOR_NOT_FOUND", "EPUB来源锚点已失效")
    return normalize_text("".join(elements[element_index][1].itertext()))


def resolve_anchor_text(path: str | Path, block: Block) -> str:
    """Read source text at a block anchor without trusting only stored block text."""

    source_path = Path(path).resolve()
    source_format = _FORMAT_BY_SUFFIX.get(source_path.suffix.lower())
    locator = block.source_anchor.locator
    if source_format in {"txt", "markdown"}:
        if locator.line_start is None or locator.line_end is None:
            raise DocumentParseError("INVALID_ANCHOR", "文本来源锚点缺少行号")
        lines = _decode_text(_safe_read_bytes(source_path)).splitlines()
        selected = lines[locator.line_start - 1 : locator.line_end]
        return normalize_text(" ".join(selected))

    if source_format == "pdf":
        if locator.page is None or locator.element_index is None:
            raise DocumentParseError("INVALID_ANCHOR", "PDF来源锚点缺少页码或元素序号")
        reader = PdfReader(str(source_path), strict=False)
        if locator.page > len(reader.pages):
            raise DocumentParseError("ANCHOR_NOT_FOUND", "PDF页码来源锚点已失效")
        lines = (reader.pages[locator.page - 1].extract_text() or "").splitlines()

        def factory(start: int, end: int, element_index: int) -> SourceLocator:
            return SourceLocator(
                page=locator.page,
                line_start=start + 1,
                line_end=end + 1,
                element_index=element_index,
            )

        elements = _plain_lines_to_blocks(lines, factory)
        if locator.element_index >= len(elements):
            raise DocumentParseError("ANCHOR_NOT_FOUND", "PDF元素来源锚点已失效")
        return elements[locator.element_index].text

    if source_format == "epub":
        if locator.href is None or locator.element_index is None:
            raise DocumentParseError("INVALID_ANCHOR", "EPUB来源锚点缺少文档路径或元素序号")
        return _resolve_epub_element(source_path, locator.href, locator.element_index)

    raise DocumentParseError("UNSUPPORTED_FORMAT", "无法解析该来源锚点")

