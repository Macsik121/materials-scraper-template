from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from core.fs_utils import atomic_write_json
from core.models import Course, ItemStateRecord, ItemStatus


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ManifestStore:
    """Атомарное хранение нормализованного дерева курса (manifest.json)."""

    def __init__(self, course_dir: Path) -> None:
        self.path = course_dir / "manifest.json"

    def exists(self) -> bool:
        return self.path.exists()

    def save(self, course: Course) -> Path:
        return atomic_write_json(self.path, course.to_dict())

    def load(self) -> Course:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return Course.from_dict(data)


class StateStore:
    """
    Атомарный локальный трекер статусов каждого item_id (state.json).
    Обеспечивает идемпотентный перезапуск (SPEC §1.2).
    """

    def __init__(self, course_dir: Path) -> None:
        self.course_dir = course_dir
        self.path = course_dir / "state.json"
        self._records: dict[str, ItemStateRecord] = {}
        if self.path.exists():
            self.load()

    def load(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        items = raw.get("items", {}) if isinstance(raw, dict) else {}
        self._records = {
            item_id: ItemStateRecord.from_dict(rec)
            for item_id, rec in items.items()
            if isinstance(rec, dict)
        }

    def save(self) -> Path:
        payload = {
            "version": 1,
            "updated_at": _utc_now_iso(),
            "items": {item_id: rec.to_dict() for item_id, rec in sorted(self._records.items())},
        }
        return atomic_write_json(self.path, payload)

    def get(self, item_id: str) -> ItemStateRecord | None:
        return self._records.get(item_id)

    def all_records(self) -> dict[str, ItemStateRecord]:
        return dict(self._records)

    def set_status(
        self,
        item_id: str,
        status: ItemStatus,
        *,
        local_path: Path | str = "",
        size_bytes: int = 0,
        error: str = "",
        auto_save: bool = True,
    ) -> ItemStateRecord:
        rel_path = ""
        if local_path:
            p = Path(local_path)
            try:
                rel_path = str(p.resolve().relative_to(self.course_dir.resolve()))
            except ValueError:
                rel_path = str(p)

        record = ItemStateRecord(
            item_id=item_id,
            status=status,
            local_path=rel_path,
            size_bytes=size_bytes,
            error=error,
            updated_at=_utc_now_iso(),
        )
        self._records[item_id] = record
        if auto_save:
            self.save()
        return record

    def should_skip(self, item_id: str, *, force_recheck: bool = False) -> bool:
        """
        Элемент пропускается, если статус DONE и артефакт существует на диске
        (файл > 0 B либо непустая директория отчёта).
        """
        if force_recheck:
            return False
        rec = self._records.get(item_id)
        if rec is None or rec.status != ItemStatus.DONE or not rec.local_path:
            return False

        target = Path(rec.local_path)
        if not target.is_absolute():
            target = self.course_dir / target

        if target.is_file():
            return target.stat().st_size > 0
        if target.is_dir():
            return any(p.is_file() and p.stat().st_size > 0 for p in target.rglob("*"))
        return False

    def summary(self) -> dict[str, int]:
        counts = Counter(rec.status.value for rec in self._records.values())
        return {status.value: counts.get(status.value, 0) for status in ItemStatus}
