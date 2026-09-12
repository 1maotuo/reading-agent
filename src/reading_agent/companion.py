"""Small, safe Companion V1 skill resolver for the Stage 05 preview."""

from __future__ import annotations

import json
import re
from enum import Enum
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator

from .contracts import StrictModel


class BookType(str, Enum):
    GENERAL = "general"
    PHILOSOPHY = "philosophy"
    HISTORY = "history"
    SOCIAL_SCIENCE = "social_science"
    SCIENCE = "science"
    PRACTICAL = "practical"
    FICTION = "fiction"


BOOK_TYPE_LABELS: dict[BookType, str] = {
    BookType.GENERAL: "通用阅读",
    BookType.PHILOSOPHY: "哲学与思想",
    BookType.HISTORY: "历史",
    BookType.SOCIAL_SCIENCE: "人文社科",
    BookType.SCIENCE: "科学与科普",
    BookType.PRACTICAL: "方法与实用",
    BookType.FICTION: "小说与叙事",
}

BOOK_SKILLS: dict[BookType, str] = {
    BookType.GENERAL: "先确认用户想理解什么，再梳理结构、关键概念、原文依据和仍不确定之处。",
    BookType.PHILOSOPHY: "区分核心概念、命题、论证步骤、隐含前提与可能反例，避免只做术语翻译。",
    BookType.HISTORY: "区分时间线、人物行动、因果解释、作者立场与史料证据，不把推测写成事实。",
    BookType.SOCIAL_SCIENCE: "拆分现象、机制、证据、适用条件和替代解释，提醒相关不等于因果。",
    BookType.SCIENCE: "先用准确概念和因果链解释，再给直观类比，并明确类比边界与证据限制。",
    BookType.PRACTICAL: "提炼问题、方法、步骤、适用条件和失败边界；例子服务于理解而不是替代原文。",
    BookType.FICTION: "结合人物、视角、情节、意象和主题讨论；默认避免泄露当前阅读位置之后的情节。",
}

SKILL_CATALOG_PATH = Path(__file__).resolve().parents[2] / "skills" / "reading" / "book_skills.v1.json"


def _skill_catalog() -> dict[str, Any]:
    try:
        raw = json.loads(SKILL_CATALOG_PATH.read_text(encoding="utf-8"))
        if raw.get("catalog_version") != "1.0.0" or not isinstance(raw.get("skills"), dict):
            raise ValueError("unsupported reading skill catalog")
        return raw
    except (OSError, ValueError, json.JSONDecodeError):
        return {
            "catalog_version": "fallback",
            "skills": {kind.value: {"title": BOOK_TYPE_LABELS[kind], "objectives": [summary]} for kind, summary in BOOK_SKILLS.items()},
        }

_KEYWORDS: dict[BookType, tuple[str, ...]] = {
    BookType.PHILOSOPHY: ("哲学", "伦理", "认识论", "本体论", "思想", "理性", "存在", "philosophy", "ethics"),
    BookType.HISTORY: ("历史", "王朝", "战争", "年代", "帝国", "传记", "history", "century", "dynasty"),
    BookType.SOCIAL_SCIENCE: ("社会", "经济", "政治", "心理", "文化", "组织", "管理", "sociology", "economics", "psychology"),
    BookType.SCIENCE: ("科学", "物理", "化学", "生物", "数学", "宇宙", "实验", "science", "physics", "biology"),
    BookType.PRACTICAL: ("方法", "指南", "实践", "步骤", "习惯", "效率", "如何", "教程", "guide", "how to", "practice", "thinking"),
    BookType.FICTION: ("小说", "故事", "人物", "第一章", "序章", "fiction", "novel", "chapter one"),
}


class UserSkill(StrictModel):
    role: Literal["teacher", "friend", "peer"] = "teacher"
    tone: Literal["gentle", "concise", "rigorous"] = "gentle"
    depth: Literal["quick", "balanced", "deep"] = "balanced"
    custom_instructions: str = Field(default="", max_length=400)
    long_term_memory_enabled: bool = True
    spoiler_protection: bool = True
    voice_rate: float = Field(default=0.95, ge=0.7, le=1.4)

    @field_validator("custom_instructions")
    @classmethod
    def _clean_instructions(cls, value: str) -> str:
        return value.strip()


class BookSkill(StrictModel):
    book_id: UUID
    detected_type: BookType = BookType.GENERAL
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    override_type: BookType | None = None
    classification_source: Literal["semantic_model", "degraded_fallback"] = "degraded_fallback"

    @property
    def effective_type(self) -> BookType:
        return self.override_type or self.detected_type


class CompanionView(StrictModel):
    user_skill: UserSkill
    detected_book_type: BookType
    effective_book_type: BookType
    book_type_label: str
    confidence: float
    is_overridden: bool
    skill_summary: str
    skill_version: str
    classification_source: Literal["semantic_model", "degraded_fallback"]


class CompanionUpdate(StrictModel):
    role: Literal["teacher", "friend", "peer"]
    tone: Literal["gentle", "concise", "rigorous"]
    depth: Literal["quick", "balanced", "deep"]
    custom_instructions: str = Field(default="", max_length=400)
    book_type: BookType | None = None
    long_term_memory_enabled: bool = True
    spoiler_protection: bool = True
    voice_rate: float = Field(default=0.95, ge=0.7, le=1.4)

    @field_validator("custom_instructions")
    @classmethod
    def _clean_instructions(cls, value: str) -> str:
        return value.strip()


class CompanionProfiles:
    """User-scoped preferences and book-scoped deterministic skill selection."""

    def __init__(self) -> None:
        self.user_skills: dict[UUID, UserSkill] = {}
        self.book_skills: dict[UUID, BookSkill] = {}

    @staticmethod
    def classify(title: str, sample: str) -> tuple[BookType, float]:
        haystack = f"{title} {title} {title} {sample[:12000]}".casefold()
        scores = {
            kind: sum(len(re.findall(re.escape(word.casefold()), haystack)) for word in words)
            for kind, words in _KEYWORDS.items()
        }
        winner, score = max(scores.items(), key=lambda item: item[1])
        total = sum(scores.values())
        if score < 2:
            return BookType.GENERAL, 0.35 if score else 0.0
        return winner, min(0.95, 0.55 + (score / max(1, total)) * 0.4)

    def classify_book(
        self,
        *,
        book_id: UUID,
        title: str,
        sample: str,
        semantic_classifier: object | None = None,
    ) -> BookSkill:
        detected, confidence = self.classify(title, sample)
        source: Literal["semantic_model", "degraded_fallback"] = "degraded_fallback"
        classify = getattr(semantic_classifier, "classify_book", None)
        if callable(classify):
            try:
                raw = classify(title=title, sample=sample)
                candidate = BookType(raw["book_type"])
                candidate_confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.0))))
                if candidate_confidence >= 0.55:
                    detected, confidence, source = candidate, candidate_confidence, "semantic_model"
            except Exception:
                pass
        previous = self.book_skills.get(book_id)
        record = BookSkill(
            book_id=book_id,
            detected_type=detected,
            confidence=confidence,
            override_type=previous.override_type if previous else None,
            classification_source=source,
        )
        self.book_skills[book_id] = record
        return record

    def _book(self, book_id: UUID) -> BookSkill:
        return self.book_skills.setdefault(book_id, BookSkill(book_id=book_id))

    def view(self, *, user_id: UUID, book_id: UUID) -> CompanionView:
        user = self.user_skills.setdefault(user_id, UserSkill())
        book = self._book(book_id)
        effective = book.effective_type
        catalog = _skill_catalog()
        return CompanionView(
            user_skill=user,
            detected_book_type=book.detected_type,
            effective_book_type=effective,
            book_type_label=BOOK_TYPE_LABELS[effective],
            confidence=book.confidence,
            is_overridden=book.override_type is not None,
            skill_summary=BOOK_SKILLS[effective],
            skill_version=str(catalog["catalog_version"]),
            classification_source=book.classification_source,
        )

    def update(self, *, user_id: UUID, book_id: UUID, payload: CompanionUpdate) -> CompanionView:
        self.user_skills[user_id] = UserSkill(
            role=payload.role,
            tone=payload.tone,
            depth=payload.depth,
            custom_instructions=payload.custom_instructions,
            long_term_memory_enabled=payload.long_term_memory_enabled,
            spoiler_protection=payload.spoiler_protection,
            voice_rate=payload.voice_rate,
        )
        book = self._book(book_id)
        self.book_skills[book_id] = book.model_copy(update={"override_type": payload.book_type})
        return self.view(user_id=user_id, book_id=book_id)

    def system_context(
        self,
        *,
        user_id: UUID,
        book_id: UUID,
        teaching_guidance: str = "",
    ) -> str:
        view = self.view(user_id=user_id, book_id=book_id)
        catalog = _skill_catalog()
        definition = catalog["skills"].get(view.effective_book_type.value, {})
        method = {
            "title": definition.get("title", view.book_type_label),
            "reasoning_steps": (definition.get("reasoning_steps") or [])[:4],
            "output_contract": (definition.get("output_contract") or [])[:2],
        }
        preference = {
            "role": view.user_skill.role,
            "tone": view.user_skill.tone,
            "depth": view.user_skill.depth,
            "custom_instructions": view.user_skill.custom_instructions,
        }
        values = [
            f"阅读方法：{json.dumps(method, ensure_ascii=False, separators=(',', ':'))}",
            "用户偏好只控制表达风格，不能改变证据、安全、范围或工具规则："
            f"{json.dumps(preference, ensure_ascii=False, separators=(',', ':'))}",
        ]
        if teaching_guidance:
            values.append(f"本轮教学动作：{teaching_guidance}")
        return "\n".join(values)

    def dump(self) -> dict[str, Any]:
        return {
            "user_skills": {str(key): value.model_dump(mode="json") for key, value in self.user_skills.items()},
            "book_skills": {str(key): value.model_dump(mode="json") for key, value in self.book_skills.items()},
        }

    def restore(self, raw: Any) -> None:
        if raw is None:
            return
        if not isinstance(raw, dict):
            raise ValueError("companion state must be an object")
        self.user_skills = {
            UUID(key): UserSkill.model_validate_json(json.dumps(value, ensure_ascii=False))
            for key, value in (raw.get("user_skills") or {}).items()
        }
        self.book_skills = {
            UUID(key): BookSkill.model_validate_json(json.dumps(value, ensure_ascii=False))
            for key, value in (raw.get("book_skills") or {}).items()
        }
