"""
Нормализация структуры ``ASSETS`` из ``config.py``.

``organize_assets.py`` может генерировать разные формы; здесь приводим к единому виду:
- персонажи: ``ASSETS["characters"][name]["base"]`` → путь к файлу;
- локации: ``ASSETS["locations"][name]["base"]`` и опционально ``top_view``.

Модуль импортируется пайплайном и подставляется в промпты LLM.
"""

from typing import Any

from config import ASSETS as CONFIG_ASSETS


def normalize_assets(assets: dict[str, Any]) -> dict[str, Any]:
    """
    Приводит сырой словарь из ``config.py`` к ожидаемой схеме.

    Поддерживает:
    - персонаж уже как ``{"base": "path"}`` или как строка-путь;
    - локация как строка-путь.

    Возвращает новый словарь, исходный не мутирует.
    """
    out: dict[str, Any] = {"characters": {}, "locations": {}}
    characters = assets.get("characters") or {}
    locations = assets.get("locations") or {}

    for key, value in characters.items():
        if isinstance(value, dict) and isinstance(value.get("base"), str):
            out["characters"][key] = {"base": value["base"]}
        elif isinstance(value, str):
            out["characters"][key] = {"base": value}

    for key, value in locations.items():
        if isinstance(value, str):
            out["locations"][key] = {"base": value}
            continue
        if isinstance(value, dict) and isinstance(value.get("base"), str):
            item: dict[str, str] = {"base": value["base"]}
            top_view = value.get("top_view")
            if isinstance(top_view, str) and top_view.strip():
                item["top_view"] = top_view
            out["locations"][key] = item
    return out


# Единый объект ассетов для всего приложения (импортируется после нормализации).
ASSETS: dict[str, Any] = normalize_assets(CONFIG_ASSETS)
