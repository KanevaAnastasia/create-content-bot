"""
Лимит длины реплики сцены для Veo TTS: символы в секунду, паузы не считаются.

Токен ``[пауза]`` не входит в бюджет длины речи (паузы задаются отдельно).
"""

from __future__ import annotations

PAUSE_TOKEN = "[пауза]"
SPEECH_CHARS_PER_SECOND = 14


def speech_chars_excluding_pauses(text: str) -> int:
    """
    Длина речи в символах без учёта токенов ``[пауза]`` (регистрозависимо, как в сценарии).

    Пробелы вокруг удалённых токенов схлопываются по краям фрагментов; множественные пробелы
    между словами сохраняются как один пробел только на стыках удаления токена — упрощённо:
    удаляем все вхождения ``[пауза]`` и считаем ``len`` остатка.
    """
    raw = (text or "").strip()
    if not raw:
        return 0
    compact = raw.replace(PAUSE_TOKEN, "")
    return len(compact.strip())


def max_speech_chars_for_duration(duration_sec: int) -> int:
    """Максимум символов речи (без пауз) для ``duration_sec``."""
    try:
        sec = int(duration_sec)
    except (TypeError, ValueError):
        sec = 6
    return max(1, sec * SPEECH_CHARS_PER_SECOND)
