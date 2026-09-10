"""Format-independent document models used by the parsing experiment."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


DocumentFormat = Literal["pdf", "epub", "txt", "markdown"]
BlockKind = Literal["paragraph", "list_item"]


class SourceLocator(BaseModel):
    """Format-specific coordinates required to revisit the original source."""

    model_config = ConfigDict(extra="forbid")

    page: int | None = Field(default=None, ge=1)
    href: str | None = None
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    element_index: int | None = Field(default=None, ge=0)


class SourceAnchor(BaseModel):
    """A quote plus surrounding text makes an anchor recoverable after reflow."""

    model_config = ConfigDict(extra="forbid")

    locator: SourceLocator
    exact_quote: str
    prefix: str = ""
    suffix: str = ""


class Block(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_id: str
    ordinal: int = Field(ge=0)
    kind: BlockKind
    text: str = Field(min_length=1)
    content_sha256: str
    chapter_id: str
    section_id: str
    source_anchor: SourceAnchor


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_id: str
    ordinal: int = Field(ge=0)
    title: str
    level: int = Field(ge=1, le=6)
    path: list[str]
    blocks: list[Block]


class Chapter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapter_id: str
    ordinal: int = Field(ge=0)
    title: str
    sections: list[Section]


class ParsedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "0.1.0"
    pipeline_version: str
    document_id: str
    source_name: str
    source_format: DocumentFormat
    source_sha256: str
    title: str
    chapters: list[Chapter]

    def all_blocks(self) -> list[Block]:
        return [
            block
            for chapter in self.chapters
            for section in chapter.sections
            for block in section.blocks
        ]

    def find_block(self, block_id: str) -> Block | None:
        return next(
            (block for block in self.all_blocks() if block.block_id == block_id),
            None,
        )

