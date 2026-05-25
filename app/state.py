"""
In-memory хранилище сессий пользователей по ``chat_id``.

В продакшене при перезапуске процесса состояние теряется — при необходимости заменить на Redis/SQLite.

Типичные ключи словаря сессии:
- state, user_input, user_feedback, concepts, selected_concept, recipe_steps,
  scenes_plan, scenes_text, production.
"""

from typing import Any

SESSIONS: dict[int, dict[str, Any]] = {}
