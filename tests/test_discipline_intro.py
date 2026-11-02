import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from app import discipline_intro
from app.discipline_intro import (
    DISCIPLINE_INTRO_APPROVED_PROVIDER_SESSION_KEY,
    DISCIPLINE_INTRO_APPROVED_VIDEO_PATH_SESSION_KEY,
    DISCIPLINE_INTRO_DIALOGUE_SESSION_KEY,
    DISCIPLINE_INTRO_ENABLED_SESSION_KEY,
    DISCIPLINE_INTRO_VIDEO_PATH_SESSION_KEY,
    DISCIPLINE_INTRO_VIDEO_VERSIONS_SESSION_KEY,
    STATE_WAITING_DISCIPLINE_INTRO_CHOICE,
    STATE_WAITING_DISCIPLINE_INTRO_DIALOGUE_APPROVAL,
    discipline_intro_start_frame_prompt,
    discipline_intro_video_prompt,
    format_discipline_intro_dialogue,
    generate_discipline_intro_dialogue,
    parse_intro_dialogue_text,
)
from app.handlers import handlers_state_scene, handlers_state_start
from app.story_generation import STORY_SOURCE_SESSION_KEY, STORY_VISUAL_STYLE_SESSION_KEY, STORY_VOICEOVER_SESSION_KEY
from app.session_store import _chat_session_cache_payload


def test_generate_discipline_intro_dialogue_is_short_problem_solution(monkeypatch):
    captured: list[str] = []

    def fake_gemini(prompt, **_kwargs):
        captured.append(prompt)
        return json.dumps(
            {
                "vasily": "Я опять откладываю важный проект.",
                "anastasia": "Выбери первый шаг и закончи его сейчас.",
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(discipline_intro, "gemini_generate_sync", fake_gemini)

    result = generate_discipline_intro_dialogue("История о том, как закончить диссертацию")

    assert result["vasily"].startswith("Я опять")
    assert result["anastasia"].startswith("Выбери первый шаг")
    assert "ровно две реплики" in captured[0]
    assert "довести дело до конца" in captured[0]


def test_discipline_intro_video_prompt_locks_speakers_and_camera_halves():
    prompt = discipline_intro_video_prompt(
        {
            "vasily": "Я никогда не начну этот проект, он огромный!",
            "anastasia": "Тебе не нужно делать всё сразу. Сейчас надо только начать",
        }
    )

    assert "0.0-4.0 seconds: Vasily is the only speaker" in prompt
    assert "4.0-8.0 seconds: Anastasia is the only speaker" in prompt
    assert "camera makes a gentle smooth push-in toward his face" in prompt
    assert "camera smoothly redirects" in prompt
    assert "Knopka" in prompt
    assert "Generate no audio" in prompt


def test_grandma_intro_dialogue_is_question_then_story_bridge(monkeypatch):
    captured: list[str] = []

    def fake_gemini(prompt, **_kwargs):
        captured.append(prompt)
        return json.dumps(
            {
                "grandson": "Бабушка, салат назвали в честь императора?",
                "grandma": "Нет, внучек. Его история началась совсем в другом месте.",
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(discipline_intro, "gemini_generate_sync", fake_gemini)

    result = generate_discipline_intro_dialogue(
        "Представь: Америка, 1920-е. Сухой закон.",
        project_slug="grandma_kitchen",
    )

    assert set(result) == {"grandson", "grandma"}
    assert "Внук задаёт бабушке" in captured[0]
    assert "естественно и без повтора продолжаться" in captured[0]
    assert "Василий" not in captured[0]
    assert "Анастасия" not in captured[0]


def test_grandma_intro_prompts_use_only_grandson_and_grandmother():
    dialogue = {
        "grandson": "Бабушка, салат назвали в честь императора?",
        "grandma": "Нет, внучек. Его история началась совсем в другом месте.",
    }

    formatted = format_discipline_intro_dialogue(dialogue, project_slug="grandma_kitchen")
    video_prompt = discipline_intro_video_prompt(dialogue, project_slug="grandma_kitchen")
    image_prompt = discipline_intro_start_frame_prompt(dialogue, project_slug="grandma_kitchen")

    assert "Внук:" in formatted
    assert "Бабушка:" in formatted
    assert "0.0-4.0 seconds: Grandson is the only speaker" in video_prompt
    assert "4.0-8.0 seconds: Grandmother is the only speaker" in video_prompt
    assert "natural Russian-speaking boy around eight to ten years old" in video_prompt
    assert "natural elderly Russian-speaking woman" in video_prompt
    assert "Generate no audio" not in video_prompt
    assert "grandmother, grandson" in image_prompt
    for wrong_character in ("Vasily", "Anastasia", "Knopka", "Василий", "Анастасия"):
        assert wrong_character not in video_prompt
        assert wrong_character not in image_prompt


def test_explicit_grandma_dialogue_is_parsed_verbatim():
    text = (
        "Исправь: Внук: Ба, а «Цезарь» правда назвали в честь того самого Цезаря?\n"
        "Бабушка: Того самого оставь в покое. Тут без выпивки не обошлось."
    )

    assert parse_intro_dialogue_text(text, project_slug="grandma_kitchen") == {
        "grandson": "Ба, а «Цезарь» правда назвали в честь того самого Цезаря?",
        "grandma": "Того самого оставь в покое. Тут без выпивки не обошлось.",
    }


def test_intro_fix_button_waits_for_feedback_instead_of_generating():
    replies: list[str] = []
    session = {"state": STATE_WAITING_DISCIPLINE_INTRO_DIALOGUE_APPROVAL}

    class Message:
        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

    update = SimpleNamespace(message=Message(), effective_chat=SimpleNamespace(id=None))
    handled, normalized = asyncio.run(
        handlers_state_scene._handle_guided_feedback_buttons(
            update,
            session=session,
            state=STATE_WAITING_DISCIPLINE_INTRO_DIALOGUE_APPROVAL,
            text="Исправь",
        )
    )

    assert handled is True
    assert normalized == "Исправь"
    assert any("Опишите правку" in text for text in replies)


def test_grandma_intro_video_uses_native_project_voices(monkeypatch, tmp_path):
    image_path = tmp_path / "intro.png"
    image_path.write_bytes(b"image")
    output_path = tmp_path / "native.mp4"
    captured: dict[str, object] = {}
    session = {
        "selected_project_key": "grandma_kitchen",
        DISCIPLINE_INTRO_DIALOGUE_SESSION_KEY: {
            "grandson": "Ба, а Цезарь правда тот самый?",
            "grandma": "Внучок, Юлий Цезарь тут совсем ни при чём.",
        },
        "discipline_intro_image_path": str(image_path),
    }

    class Message:
        async def reply_text(self, _text, reply_markup=None):
            _ = reply_markup

        async def reply_video(self, video, caption=None):
            _ = video
            captured["caption"] = caption

        async def reply_document(self, **_kwargs):
            raise AssertionError("video delivery should succeed")

    def fake_syntax(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(success=True, video_bytes=b"mp4", provider_message="ok", attempt_id="attempt")

    def fake_store(_chat_id, video_bytes, *, provider):
        assert video_bytes == b"mp4"
        assert provider == "veo"
        output_path.write_bytes(video_bytes)
        return str(output_path)

    monkeypatch.setattr(handlers_state_scene, "try_generate_via_syntax", fake_syntax)
    monkeypatch.setattr(handlers_state_scene, "store_discipline_intro_video", fake_store)
    monkeypatch.setattr(
        handlers_state_scene,
        "generate_discipline_intro_audio",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("ElevenLabs must not be used")),
    )
    monkeypatch.setattr(
        handlers_state_scene,
        "compose_discipline_intro",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("native audio must not be replaced")),
    )
    monkeypatch.setattr(handlers_state_scene, "SESSIONS", {})

    result = asyncio.run(
        handlers_state_scene._generate_discipline_intro_video_candidate(
            SimpleNamespace(message=Message()),
            chat_id=47,
            session=session,
            provider="veo",
        )
    )

    assert result is True
    assert captured["audio_enabled"] is True
    assert "Voice lock GRANDSON" in str(captured["prompt"])
    assert "проектные голоса" in str(captured["caption"])
    assert session["state"] == "waiting_discipline_intro_video_approval"


def test_discipline_style_choice_starts_story_before_intro_choice(monkeypatch):
    called: list[str] = []
    session = {
        "selected_project_key": "discipline",
        STORY_SOURCE_SESSION_KEY: "Большой проект пугает Василия.",
    }

    async def fail_intro(*_args, **_kwargs):
        raise AssertionError("intro must be chosen only after script approval")

    async def fake_story(_update, _context, *, chat_id, session):
        called.append(f"story:{chat_id}")
        session["state"] = "waiting_story_script_approval"
        return True

    class Message:
        async def reply_text(self, _text, reply_markup=None):
            _ = reply_markup

    monkeypatch.setattr(handlers_state_start, "_start_discipline_intro_dialogue", fail_intro)
    monkeypatch.setattr(handlers_state_start, "_start_initial_story_voiceover", fake_story)

    result = asyncio.run(
        handlers_state_start._handle_waiting_story_style_choice_state(
            SimpleNamespace(message=Message()),
            None,
            chat_id=42,
            session=session,
            text="Основной",
        )
    )

    assert result is True
    assert called == ["story:42"]
    assert session[STORY_VISUAL_STYLE_SESSION_KEY] == "primary"
    assert session["state"] == "waiting_story_script_approval"


def test_discipline_script_approval_asks_intro_choice_before_audio(monkeypatch):
    session = {
        "selected_project_key": "discipline",
        STORY_VOICEOVER_SESSION_KEY: "Утверждённый сценарий.",
    }
    replies: list[str] = []

    class Message:
        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

    async def fail_audio(*_args, **_kwargs):
        raise AssertionError("audio must wait for intro choice")

    monkeypatch.setattr(handlers_state_scene, "_generate_and_send_story_audio", fail_audio)
    result = asyncio.run(
        handlers_state_scene._handle_waiting_story_script_approval_state(
            SimpleNamespace(message=Message()),
            None,
            chat_id=43,
            session=session,
            text="ОК",
        )
    )

    assert result is True
    assert session["state"] == STATE_WAITING_DISCIPLINE_INTRO_CHOICE
    assert any("Добавить короткое" in text for text in replies)


def test_grandma_script_approval_also_asks_intro_choice_before_audio(monkeypatch):
    session = {
        "selected_project_key": "grandma_kitchen",
        STORY_VOICEOVER_SESSION_KEY: "Утверждённая история бабушки.",
    }
    replies: list[str] = []

    class Message:
        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

    async def fail_audio(*_args, **_kwargs):
        raise AssertionError("audio must wait for grandma intro choice")

    monkeypatch.setattr(handlers_state_scene, "_generate_and_send_story_audio", fail_audio)
    result = asyncio.run(
        handlers_state_scene._handle_waiting_story_script_approval_state(
            SimpleNamespace(message=Message()),
            None,
            chat_id=46,
            session=session,
            text="ОК",
        )
    )

    assert result is True
    assert session["state"] == STATE_WAITING_DISCIPLINE_INTRO_CHOICE
    assert any("Добавить короткое" in text for text in replies)


def test_discipline_intro_choice_can_continue_without_intro(monkeypatch):
    session = {
        "selected_project_key": "discipline",
        "state": STATE_WAITING_DISCIPLINE_INTRO_CHOICE,
        STORY_VOICEOVER_SESSION_KEY: "Утверждённый сценарий.",
        DISCIPLINE_INTRO_APPROVED_VIDEO_PATH_SESSION_KEY: "/tmp/old-intro.mp4",
    }
    started: list[int] = []

    class Message:
        async def reply_text(self, _text, reply_markup=None):
            _ = reply_markup

    async def fake_audio(_update, _context, *, chat_id, session):
        started.append(chat_id)
        return True

    monkeypatch.setattr(handlers_state_scene, "_generate_and_send_story_audio", fake_audio)
    result = asyncio.run(
        handlers_state_scene._handle_waiting_discipline_intro_choice_state(
            SimpleNamespace(message=Message()),
            None,
            chat_id=44,
            session=session,
            text="Без интро",
        )
    )

    assert result is True
    assert started == [44]
    assert session[DISCIPLINE_INTRO_ENABLED_SESSION_KEY] is False
    assert DISCIPLINE_INTRO_APPROVED_VIDEO_PATH_SESSION_KEY not in session


def test_skip_intro_during_script_approval_preserves_verbatim_script(monkeypatch):
    approved_text = "Я прочитала за вас десятки книг. Это мой дословный текст."
    session = {
        "selected_project_key": "discipline",
        "state": "waiting_story_script_approval",
        STORY_VOICEOVER_SESSION_KEY: approved_text,
    }
    started_with: list[str] = []

    class Message:
        async def reply_text(self, _text, reply_markup=None):
            _ = reply_markup

    async def fake_audio(_update, _context, *, chat_id, session):
        _ = chat_id
        started_with.append(session[STORY_VOICEOVER_SESSION_KEY])
        return True

    async def forbid_refinement(*_args, **_kwargs):
        raise AssertionError("«Без интро» must never be sent to Gemini as script feedback")

    monkeypatch.setattr(handlers_state_scene, "_generate_and_send_story_audio", fake_audio)
    monkeypatch.setattr(handlers_state_scene, "_refine_story_voiceover", forbid_refinement)

    result = asyncio.run(
        handlers_state_scene._handle_waiting_story_script_approval_state(
            SimpleNamespace(message=Message()),
            None,
            chat_id=47,
            session=session,
            text="Без интро",
        )
    )

    assert result is True
    assert session[STORY_VOICEOVER_SESSION_KEY] == approved_text
    assert session[DISCIPLINE_INTRO_ENABLED_SESSION_KEY] is False
    assert started_with == [approved_text]


def test_legacy_current_intro_can_skip_back_to_story_script(monkeypatch):
    session = {
        "selected_project_key": "discipline",
        "state": STATE_WAITING_DISCIPLINE_INTRO_DIALOGUE_APPROVAL,
        STORY_SOURCE_SESSION_KEY: "Сценарий старой текущей генерации.",
    }
    started: list[int] = []

    class Message:
        async def reply_text(self, _text, reply_markup=None):
            _ = reply_markup

    async def fake_story(_update, _context, *, chat_id, session):
        started.append(chat_id)
        session["state"] = "waiting_story_script_approval"
        return True

    monkeypatch.setattr(handlers_state_scene, "_start_initial_story_voiceover", fake_story)
    result = asyncio.run(
        handlers_state_scene._handle_waiting_discipline_intro_dialogue_approval_state(
            SimpleNamespace(message=Message()),
            None,
            chat_id=45,
            session=session,
            text="Без интро",
        )
    )

    assert result is True
    assert started == [45]
    assert session[DISCIPLINE_INTRO_ENABLED_SESSION_KEY] is False


def test_intro_approval_continues_ordinary_story_only_for_discipline(monkeypatch, tmp_path):
    video_path = tmp_path / "approved-intro.mp4"
    video_path.write_bytes(b"video")
    session = {
        "selected_project_key": "discipline",
        DISCIPLINE_INTRO_DIALOGUE_SESSION_KEY: {"vasily": "Проблема", "anastasia": "Решение"},
        DISCIPLINE_INTRO_VIDEO_PATH_SESSION_KEY: str(video_path),
        DISCIPLINE_INTRO_VIDEO_VERSIONS_SESSION_KEY: [{"path": str(video_path), "provider": "veo"}],
    }
    replies: list[str] = []
    started: list[int] = []

    class Message:
        async def reply_text(self, text, reply_markup=None):
            _ = reply_markup
            replies.append(text)

    async def fake_start(_update, _context, *, chat_id, session):
        started.append(chat_id)
        session["state"] = "waiting_story_script_approval"
        return True

    monkeypatch.setattr(handlers_state_scene, "_start_initial_story_voiceover", fake_start)
    result = asyncio.run(
        handlers_state_scene._handle_waiting_discipline_intro_video_approval_state(
            SimpleNamespace(message=Message()),
            None,
            chat_id=84,
            session=session,
            text="Апрув",
        )
    )

    assert result is True
    assert started == [84]
    assert session[DISCIPLINE_INTRO_APPROVED_VIDEO_PATH_SESSION_KEY] == str(video_path)
    assert session[DISCIPLINE_INTRO_APPROVED_PROVIDER_SESSION_KEY] == "veo"
    assert any("Интро утверждено" in text for text in replies)


def test_intro_video_keyboard_has_skip_action():
    markup = handlers_state_scene._discipline_intro_video_approval_markup()
    labels = [button["text"] for row in markup.to_dict()["keyboard"] for button in row]

    assert labels[:4] == ["Апрув", "Без интро", "ПЕРЕГЕНЕРИРОВАТЬ VEO", "ПЕРЕГЕНЕРИРОВАТЬ KLING"]


def test_intro_dialogue_keyboard_stays_compact_and_keeps_draft_action():
    markup = handlers_state_start._discipline_intro_dialogue_approval_markup()
    labels = [button["text"] for row in markup.to_dict()["keyboard"] for button in row]

    assert labels[:5] == ["ОК", "Без интро", "ПЕРЕГЕНЕРИРОВАТЬ", "Добавь", "Исправь"]
    assert "Сохранить черновик" in labels
    assert "Версии текста" not in labels
    assert "СВОЙ ТЕКСТ" not in labels


def test_intro_paths_and_approval_survive_session_cache_for_drafts_and_final_worker(tmp_path):
    intro = tmp_path / "intro.mp4"
    intro.write_bytes(b"approved")
    cached = _chat_session_cache_payload(
        {
            "state": "waiting_discipline_intro_video_approval",
            "discipline_intro_enabled": True,
            "discipline_intro_dialogue": {"vasily": "Проблема", "anastasia": "Решение"},
            "discipline_intro_audio_path": str(tmp_path / "dialogue.mp3"),
            "discipline_intro_video_path": str(intro),
            "discipline_intro_approved_video_path": str(intro),
            "discipline_intro_approved_provider": "veo",
        }
    )

    assert cached["discipline_intro_dialogue"]["vasily"] == "Проблема"
    assert cached["discipline_intro_enabled"] is True
    assert cached["discipline_intro_video_path"] == str(intro)
    assert cached["discipline_intro_approved_video_path"] == str(intro)
    assert cached["discipline_intro_approved_provider"] == "veo"
