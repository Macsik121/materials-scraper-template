from __future__ import annotations

import importlib
import inspect
import pkgutil
from pathlib import Path
from typing import Type

from adapters.base import BaseAdapter


def list_adapters(*, include_template: bool = False) -> list[str]:
    """Возвращает список доступных адаптеров в пакете adapters/."""
    package_dir = Path(__file__).resolve().parent
    names: list[str] = []
    for module_info in pkgutil.iter_modules([str(package_dir)]):
        name = module_info.name
        if name in ("base", "__init__"):
            continue
        if name.startswith("_") and not include_template:
            continue
        names.append(name)
    return sorted(names)


def load_adapter(name: str) -> BaseAdapter:
    """
    Загружает адаптер по имени (например, 'moodle' -> adapters/moodle.py).
    Ищет подкласс BaseAdapter внутри модуля и возвращает его экземпляр.
    """
    normalized = name.strip().removesuffix(".py")
    if not normalized or normalized in ("base", "__init__"):
        raise ValueError(f"Некорректное имя адаптера: {name!r}")

    module_name = f"adapters.{normalized}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        available = list_adapters(include_template=True)
        hint = ", ".join(available) if available else "нет реализованных адаптеров"
        raise ValueError(
            f"Адаптер '{normalized}' не найден ({module_name}). Доступные: {hint}."
        ) from exc

    candidates: list[Type[BaseAdapter]] = []
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, BaseAdapter) and obj is not BaseAdapter and obj.__module__ == module.__name__:
            candidates.append(obj)

    if not candidates:
        raise ValueError(f"В модуле {module_name} не найден подкласс BaseAdapter.")

    return candidates[0]()


__all__ = ["BaseAdapter", "list_adapters", "load_adapter"]
