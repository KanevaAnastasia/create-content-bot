"""
Lazy-loading session registry backed by DB snapshots when available.

Типичные ключи словаря сессии:
- state, user_input, user_feedback, concepts, selected_concept, recipe_steps,
  scenes_plan, scenes_text, production.
"""

from typing import Any


class _SessionRegistry(dict[int, dict[str, Any]]):
    def get_loaded(self, key: int) -> dict[str, Any] | None:
        """Return an in-memory session without lazy-loading it from persistence."""
        try:
            chat_id = int(key)
        except (TypeError, ValueError):
            return None
        return super().get(chat_id)

    def _restore(self, chat_id: int) -> dict[str, Any] | None:
        if super().__contains__(chat_id):
            return super().get(chat_id)
        from .session_store import load_session_if_possible

        restored = load_session_if_possible(chat_id)
        if isinstance(restored, dict):
            super().__setitem__(chat_id, restored)
            return restored
        return None

    def get(self, key: int, default=None):  # type: ignore[override]
        try:
            chat_id = int(key)
        except (TypeError, ValueError):
            return default
        restored = self._restore(chat_id)
        if restored is not None:
            return restored
        return default

    def __getitem__(self, key: int) -> dict[str, Any]:
        restored = self._restore(int(key))
        if restored is not None:
            return restored
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        try:
            chat_id = int(key)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        restored = self._restore(chat_id)
        return restored is not None or super().__contains__(chat_id)

    def __setitem__(self, key: int, value: dict[str, Any]) -> None:
        chat_id = int(key)
        super().__setitem__(chat_id, value)
        from .session_store import save_session_if_possible

        save_session_if_possible(chat_id, value)

    def pop(self, key: int, default=None):  # type: ignore[override]
        try:
            chat_id = int(key)
        except (TypeError, ValueError):
            return default
        from .session_store import delete_session_if_possible

        delete_session_if_possible(chat_id)
        return super().pop(chat_id, default)

    def __delitem__(self, key: int) -> None:
        self.pop(key)


SESSIONS: dict[int, dict[str, Any]] = _SessionRegistry()


def get_loaded_session(chat_id: int | None) -> dict[str, Any] | None:
    """Return only the currently loaded session, without restoring a deleted one."""
    if chat_id is None:
        return None
    if isinstance(SESSIONS, _SessionRegistry):
        return SESSIONS.get_loaded(chat_id)
    try:
        return dict.get(SESSIONS, int(chat_id))
    except (TypeError, ValueError):
        return None


def save_loaded_session_if_possible(chat_id: int | None) -> None:
    """Persist the loaded session snapshot if it still exists in memory."""
    session = get_loaded_session(chat_id)
    if not isinstance(session, dict):
        return
    from .session_store import save_session_if_possible

    save_session_if_possible(chat_id, session)
