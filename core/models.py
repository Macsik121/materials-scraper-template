from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ItemType(str, Enum):
    """Нормализованный тип учебного материала (SPEC §4.2)."""

    DOCUMENT = "document"
    VIDEO = "video"
    VIDEO_STREAM = "video_stream"
    HTML_PAGE = "html_page"
    ASSIGNMENT = "assignment"
    TEST_READONLY = "test_readonly"
    EXTERNAL_LINK = "external_link"


class ItemStatus(str, Enum):
    """Статус обработки элемента в state.json (SPEC §1.2)."""

    PENDING = "PENDING"
    DONE = "DONE"
    FAILED = "FAILED"
    DRM_LOCKED = "DRM_LOCKED"
    EXTERNAL_LINK = "EXTERNAL_LINK"
    SKIPPED = "SKIPPED"


@dataclass
class Item:
    """Единица учебного контента внутри модуля."""

    item_id: str
    order: int
    title: str
    item_type: ItemType
    source_url: str = ""
    lesson_url: str = ""
    extension: str = ""
    expected_filename: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "order": self.order,
            "title": self.title,
            "item_type": self.item_type.value,
            "source_url": self.source_url,
            "lesson_url": self.lesson_url,
            "extension": self.extension,
            "expected_filename": self.expected_filename,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Item:
        return cls(
            item_id=str(data["item_id"]),
            order=int(data["order"]),
            title=str(data["title"]),
            item_type=ItemType(data["item_type"]),
            source_url=str(data.get("source_url", "")),
            lesson_url=str(data.get("lesson_url", "")),
            extension=str(data.get("extension", "")),
            expected_filename=str(data.get("expected_filename", "")),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class Module:
    """Раздел или модуль курса."""

    module_id: str
    order: int
    title: str
    items: list[Item] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "module_id": self.module_id,
            "order": self.order,
            "title": self.title,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Module:
        return cls(
            module_id=str(data["module_id"]),
            order=int(data["order"]),
            title=str(data["title"]),
            items=[Item.from_dict(raw) for raw in data.get("items", [])],
        )


@dataclass
class Course:
    """Корневая модель курса для manifest.json."""

    course_id: str
    title: str
    platform: str
    source_url: str
    modules: list[Module] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def total_items(self) -> int:
        return sum(len(m.items) for m in self.modules)

    def to_dict(self) -> dict[str, Any]:
        return {
            "course_id": self.course_id,
            "title": self.title,
            "platform": self.platform,
            "source_url": self.source_url,
            "modules": [m.to_dict() for m in self.modules],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Course:
        return cls(
            course_id=str(data["course_id"]),
            title=str(data["title"]),
            platform=str(data["platform"]),
            source_url=str(data.get("source_url", "")),
            modules=[Module.from_dict(raw) for raw in data.get("modules", [])],
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class ItemStateRecord:
    """Запись состояния одного элемента в state.json."""

    item_id: str
    status: ItemStatus = ItemStatus.PENDING
    local_path: str = ""
    size_bytes: int = 0
    error: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "status": self.status.value,
            "local_path": self.local_path,
            "size_bytes": self.size_bytes,
            "error": self.error,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ItemStateRecord:
        return cls(
            item_id=str(data["item_id"]),
            status=ItemStatus(data.get("status", ItemStatus.PENDING.value)),
            local_path=str(data.get("local_path", "")),
            size_bytes=int(data.get("size_bytes", 0)),
            error=str(data.get("error", "")),
            updated_at=str(data.get("updated_at", "")),
        )
