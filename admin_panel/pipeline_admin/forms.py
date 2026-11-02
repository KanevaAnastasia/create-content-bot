from __future__ import annotations

import json
import re
from typing import Any

from django import forms

from app.media_file_store import store_scene_candidate_media

from .models import ChatSession, GenerationRun, Scene, SceneImageCandidate, SceneVideoCandidate


def _pretty(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, indent=2, sort_keys=True)


def _scene_run_id(obj: Any) -> Any:
    try:
        scene = getattr(obj, "scene", None)
    except Exception:
        scene = None
    return getattr(scene, "run_id", None)


def _loads_json(value: str, *, fallback: Any) -> Any:
    text = (value or "").strip()
    if not text:
        return fallback
    return json.loads(text)


def _speech_by_lang_from_admin_text(value: Any, *, existing: dict[str, Any] | None = None) -> dict[str, str]:
    """Accept either a JSON language map or a plain Russian speech line."""
    existing_clean = {
        str(key).strip().lower(): " ".join(str(item or "").split())
        for key, item in (existing or {}).items()
        if str(key).strip() and str(item or "").strip()
    }
    if isinstance(value, dict):
        return {
            str(key).strip().lower(): " ".join(str(item or "").split())
            for key, item in value.items()
            if str(key).strip() and str(item or "").strip()
        }
    raw = str(value or "").strip()
    if not raw:
        return {}
    if raw.startswith(("{", "[")):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise forms.ValidationError(f"Invalid JSON: {exc}") from exc
        if isinstance(parsed, dict):
            return _speech_by_lang_from_admin_text(parsed)
        if isinstance(parsed, str) and parsed.strip():
            return {**existing_clean, "ru": " ".join(parsed.split())}
        raise forms.ValidationError("Expected JSON object or plain speech text.")
    return {**existing_clean, "ru": " ".join(raw.split())}


_TITLE_COLOR_RE = re.compile(r"#?([0-9a-fA-F]{6})")
PENDING_OVERLAY_TITLE_STYLE_KEY = "pending_overlay_title_style"
DEFAULT_TITLE_COLOR = "#ffffff"


def _normalize_title_color(value: Any) -> str:
    raw = str(value or "").strip()
    match = _TITLE_COLOR_RE.fullmatch(raw)
    if not match:
        return ""
    return f"#{match.group(1).lower()}"


def _title_color_for_input(*styles: dict[str, Any]) -> str:
    for style in styles:
        if not isinstance(style, dict):
            continue
        normalized = _normalize_title_color(style.get("title_color"))
        if normalized:
            return normalized
    return DEFAULT_TITLE_COLOR


def _merged_style_from_sources(key: str, *sources: dict[str, Any]) -> dict[str, Any]:
    style: dict[str, Any] = {}
    for source in sources:
        value = source.get(key) if isinstance(source, dict) else None
        if isinstance(value, dict):
            style.update(value)
    return style


def _safe_scene_index(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _russian_overlay_title(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    direct = value.get("ru")
    if not isinstance(direct, str):
        direct = next(
            (
                item
                for key, item in value.items()
                if str(key or "").strip().lower().replace("_", "-").startswith("ru")
            ),
            "",
        )
    return " ".join(str(direct or "").split())


def _sync_generation_title_from_overlay(obj: ChatSession, overlay_title_by_lang: dict[str, Any]) -> None:
    title = _russian_overlay_title(overlay_title_by_lang)
    if not title or not obj.generation_run_id:
        return
    GenerationRun.objects.filter(id=obj.generation_run_id).update(human_title=title)


def _sync_generation_prompts_and_cursor_from_session(obj: ChatSession, session: dict[str, Any]) -> None:
    if not obj.generation_run_id or not isinstance(session, dict):
        return
    has_cursor = "image_scene_index" in session or "scene_media_substep" in session or "scene_media_language_index" in session
    updates: dict[str, Any] = {}
    if has_cursor:
        updates["is_active"] = bool(str(session.get("state") or "").strip() and session.get("state") != "ready_for_production")
        try:
            updates["current_scene_index"] = int(session.get("image_scene_index"))
        except (TypeError, ValueError):
            updates["current_scene_index"] = None
        substep = str(session.get("scene_media_substep") or "").strip()
        updates["current_media_substep"] = substep or None
        try:
            updates["current_media_language_index"] = int(session.get("scene_media_language_index"))
        except (TypeError, ValueError):
            updates["current_media_language_index"] = None
    if updates:
        GenerationRun.objects.filter(id=obj.generation_run_id).update(**updates)

    production = session.get("production")
    scenes = production.get("json_plan") if isinstance(production, dict) else None
    if not isinstance(scenes, list) or not scenes:
        return
    try:
        rows = {int(row.scene_order): row for row in Scene.objects.filter(run_id=obj.generation_run_id)}
    except Exception:
        return
    for idx, payload in enumerate(scenes, start=1):
        if not isinstance(payload, dict):
            continue
        row = rows.get(idx)
        if row is None:
            continue
        for key in ("start_frame_prompt", "start_frame_prompt_effective", "end_frame_prompt"):
            setattr(row, key, str(payload.get(key) or "") or None)
        for key in ("video_prompt_by_lang", "speech_by_lang", "syntax_prompt_by_lang", "prompt_meta_json"):
            value = payload.get(key)
            setattr(row, key, value if isinstance(value, dict) else {})
        row.text = str(payload.get("text") or row.text or "")
        try:
            row.duration_sec = float(payload.get("duration_sec") or row.duration_sec or 0.0)
        except (TypeError, ValueError):
            pass
        row.save(
            update_fields=[
                "text",
                "duration_sec",
                "start_frame_prompt",
                "start_frame_prompt_effective",
                "end_frame_prompt",
                "video_prompt_by_lang",
                "speech_by_lang",
                "syntax_prompt_by_lang",
                "prompt_meta_json",
            ]
        )


_MEDIA_SUBSTEP_CHOICES = (
    ("start_prompt", "start_prompt - approve/generate start frame"),
    ("start_image_approval", "start_image_approval - approve generated start frame"),
    ("end_prompt", "end_prompt - approve/generate end frame"),
    ("end_image_approval", "end_image_approval - approve generated end frame"),
    ("video_prompt", "video_prompt - approve/generate video"),
)

_MEDIA_PENDING_KEYS = (
    "scene_prompt_input_pending",
    "scene_video_feedback_pending",
    "scene_video_speed_pending",
    "scene_phrase_input_pending",
    "scene_phrase_translation_pending",
    "scene_speech_shorten_pending",
    "scene_media_ai_human_review",
)


class ChatSessionAdminForm(forms.ModelForm):
    selected_concept = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    viral_concept_brief = forms.CharField(
        required=False,
        label="Viral concept brief (JSON)",
        widget=forms.Textarea(attrs={"rows": 6}),
    )
    recipe_full_text = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 8}))
    scenes_plan_json = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 10}))
    production_plan_json = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 14}))
    overlay_title_by_lang_json = forms.CharField(
        required=False,
        label="Final title overlay by language (JSON)",
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text='Example: {"ru": "Борщ", "en": "Borscht"}. Used for the titled final master.',
    )
    overlay_subtitle_by_lang_json = forms.CharField(
        required=False,
        label="Optional title subtitle by language (JSON)",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text='Optional third title line, for example {"ru": "КБЖУ на 100 гр: 126/10/4/24"}.',
    )
    overlay_title_style_json = forms.CharField(
        required=False,
        label="Final title overlay style (JSON)",
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text='Optional style, for example {"title_color": "#c72638", "top_margin_ratio": 0.05}.',
    )
    overlay_title_color = forms.CharField(
        required=False,
        label="Final title color",
        widget=forms.TextInput(attrs={"type": "color"}),
        help_text="Saved as pending; the final video uses it only after Build/rebuild is pressed.",
    )

    scene_index = forms.IntegerField(required=False, min_value=0, help_text="0-based scene index to edit below.")
    start_frame_prompt = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 4}))
    end_frame_requested = forms.BooleanField(required=False)
    end_frame_prompt = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 4}))
    video_prompt_by_lang_json = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 8}))
    speech_by_lang_json = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 6}))
    syntax_retry_context_json = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 6}))
    video_approvals_by_lang_json = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 4}))
    apply_media_step_override = forms.BooleanField(
        required=False,
        label="Apply media step override",
        help_text="Check this before saving to move the live session to the selected scene/substep.",
    )
    media_scene_number = forms.IntegerField(
        required=False,
        min_value=1,
        label="Media scene number",
        help_text="1-based scene number in production.json_plan.",
    )
    media_substep = forms.ChoiceField(
        required=False,
        choices=(("", "Keep current"),) + _MEDIA_SUBSTEP_CHOICES,
        label="Media substep",
    )
    media_language_index = forms.IntegerField(
        required=False,
        min_value=0,
        label="Media language index",
        help_text="0 for the first language, 1 for the second, etc.",
    )

    class Meta:
        model = ChatSession
        fields = ("chat_id", "state", "session_json", "generation_run_id")
        widgets = {
            "session_json": forms.Textarea(attrs={"rows": 16}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        session = dict(self.instance.session_json or {})
        production = session.get("production") if isinstance(session.get("production"), dict) else {}
        scenes = production.get("json_plan") if isinstance(production, dict) else []
        scene_idx = _safe_scene_index(session.get("image_scene_index"))
        scene = scenes[scene_idx] if isinstance(scenes, list) and 0 <= scene_idx < len(scenes) else {}

        self.fields["selected_concept"].initial = session.get("selected_concept") or ""
        self.fields["viral_concept_brief"].initial = _pretty(session.get("viral_concept_brief") or {})
        self.fields["recipe_full_text"].initial = session.get("recipe_full_text") or ""
        self.fields["scenes_plan_json"].initial = _pretty(session.get("scenes_plan") or [])
        self.fields["production_plan_json"].initial = _pretty(production.get("json_plan") or [])
        self.fields["overlay_title_by_lang_json"].initial = _pretty(
            production.get("overlay_title_by_lang") or session.get("overlay_title_by_lang") or {}
        )
        self.fields["overlay_subtitle_by_lang_json"].initial = _pretty(
            production.get("overlay_subtitle_by_lang") or session.get("overlay_subtitle_by_lang") or {}
        )
        self.fields["overlay_title_style_json"].initial = _pretty(
            production.get("overlay_title_style") or session.get("overlay_title_style") or {}
        )
        active_title_style = _merged_style_from_sources("overlay_title_style", session, production)
        pending_title_style = _merged_style_from_sources(PENDING_OVERLAY_TITLE_STYLE_KEY, session, production)
        self.fields["overlay_title_color"].initial = _title_color_for_input(pending_title_style, active_title_style)
        self.fields["scene_index"].initial = scene_idx
        self.fields["media_scene_number"].initial = scene_idx + 1
        self.fields["media_substep"].initial = session.get("scene_media_substep") or "start_prompt"
        self.fields["media_language_index"].initial = _safe_scene_index(session.get("scene_media_language_index"))
        if isinstance(scene, dict):
            self.fields["start_frame_prompt"].initial = scene.get("start_frame_prompt") or scene.get("start_frame_prompt_ru") or ""
            self.fields["end_frame_requested"].initial = bool(scene.get("end_frame_requested"))
            self.fields["end_frame_prompt"].initial = scene.get("end_frame_prompt") or scene.get("end_frame_prompt_ru") or ""
            self.fields["video_prompt_by_lang_json"].initial = _pretty(scene.get("video_prompt_by_lang") or {})
            self.fields["speech_by_lang_json"].initial = _pretty(scene.get("speech_by_lang") or {})
            self.fields["syntax_retry_context_json"].initial = _pretty(scene.get("syntax_retry_context") or {})
            self.fields["video_approvals_by_lang_json"].initial = _pretty(scene.get("video_approvals_by_lang") or {})

    def clean(self):
        cleaned = super().clean()
        for field_name, fallback in {
            "viral_concept_brief": {},
            "scenes_plan_json": [],
            "production_plan_json": [],
            "overlay_title_by_lang_json": {},
            "overlay_subtitle_by_lang_json": {},
            "overlay_title_style_json": {},
            "video_prompt_by_lang_json": {},
            "speech_by_lang_json": {},
            "syntax_retry_context_json": {},
            "video_approvals_by_lang_json": {},
        }.items():
            try:
                cleaned[field_name] = _loads_json(cleaned.get(field_name) or "", fallback=fallback)
            except json.JSONDecodeError as exc:
                self.add_error(field_name, f"Invalid JSON: {exc}")
        expected_types = {
            "viral_concept_brief": dict,
            "scenes_plan_json": list,
            "production_plan_json": list,
            "overlay_title_by_lang_json": dict,
            "overlay_subtitle_by_lang_json": dict,
            "overlay_title_style_json": dict,
            "video_prompt_by_lang_json": dict,
            "speech_by_lang_json": dict,
            "syntax_retry_context_json": dict,
            "video_approvals_by_lang_json": dict,
        }
        for field_name, expected_type in expected_types.items():
            value = cleaned.get(field_name)
            if value is not None and not isinstance(value, expected_type):
                self.add_error(field_name, f"Expected JSON {expected_type.__name__}.")
        title_color = str(cleaned.get("overlay_title_color") or "").strip()
        if title_color:
            normalized_title_color = _normalize_title_color(title_color)
            if not normalized_title_color:
                self.add_error("overlay_title_color", "Use a 6-digit hex color, for example #c72638.")
            else:
                cleaned["overlay_title_color"] = normalized_title_color
        if cleaned.get("apply_media_step_override"):
            production_plan = cleaned.get("production_plan_json")
            scene_number = cleaned.get("media_scene_number")
            substep = cleaned.get("media_substep")
            if not isinstance(production_plan, list) or not production_plan:
                self.add_error("production_plan_json", "Production plan is required to move the media step.")
            if scene_number is None:
                self.add_error("media_scene_number", "Choose a scene number.")
            elif isinstance(production_plan, list) and not (1 <= int(scene_number) <= len(production_plan)):
                self.add_error("media_scene_number", f"Scene number must be between 1 and {len(production_plan)}.")
            if not substep:
                self.add_error("media_substep", "Choose a media substep.")
        return cleaned

    def save(self, commit=True):
        obj: ChatSession = super().save(commit=False)
        session = dict(obj.session_json or {})
        session["state"] = self.cleaned_data.get("state") or obj.state or ""
        session["selected_concept"] = self.cleaned_data.get("selected_concept") or None
        session["viral_concept_brief"] = self.cleaned_data.get("viral_concept_brief") or None
        session["recipe_full_text"] = self.cleaned_data.get("recipe_full_text") or None
        session["scenes_plan"] = self.cleaned_data.get("scenes_plan_json") or []

        production = session.get("production")
        if not isinstance(production, dict):
            production = {}
        production["json_plan"] = self.cleaned_data.get("production_plan_json") or []
        overlay_title_by_lang = self.cleaned_data.get("overlay_title_by_lang_json") or {}
        overlay_subtitle_by_lang = self.cleaned_data.get("overlay_subtitle_by_lang_json") or {}
        overlay_title_style = self.cleaned_data.get("overlay_title_style_json") or {}
        pending_overlay_title_style = _merged_style_from_sources(PENDING_OVERLAY_TITLE_STYLE_KEY, session, production)
        if "overlay_title_color" in self.data and "overlay_title_color" in self.changed_data:
            title_color = self.cleaned_data.get("overlay_title_color") or ""
            if title_color:
                pending_overlay_title_style["title_color"] = title_color
            else:
                pending_overlay_title_style.pop("title_color", None)
        if overlay_title_by_lang:
            production["overlay_title_by_lang"] = overlay_title_by_lang
            session["overlay_title_by_lang"] = overlay_title_by_lang
        else:
            production.pop("overlay_title_by_lang", None)
            session.pop("overlay_title_by_lang", None)
        if overlay_subtitle_by_lang:
            production["overlay_subtitle_by_lang"] = overlay_subtitle_by_lang
            session["overlay_subtitle_by_lang"] = overlay_subtitle_by_lang
        else:
            production.pop("overlay_subtitle_by_lang", None)
            session.pop("overlay_subtitle_by_lang", None)
        if overlay_title_style:
            production["overlay_title_style"] = overlay_title_style
            session["overlay_title_style"] = overlay_title_style
        else:
            production.pop("overlay_title_style", None)
            session.pop("overlay_title_style", None)
        if pending_overlay_title_style:
            production[PENDING_OVERLAY_TITLE_STYLE_KEY] = pending_overlay_title_style
            session[PENDING_OVERLAY_TITLE_STYLE_KEY] = pending_overlay_title_style
        else:
            production.pop(PENDING_OVERLAY_TITLE_STYLE_KEY, None)
            session.pop(PENDING_OVERLAY_TITLE_STYLE_KEY, None)
        session["production"] = production

        scenes = production.get("json_plan")
        scene_idx = self.cleaned_data.get("scene_index")
        if isinstance(scenes, list) and scene_idx is not None and 0 <= int(scene_idx) < len(scenes):
            scene = scenes[int(scene_idx)]
            if isinstance(scene, dict):
                start_prompt = self.cleaned_data.get("start_frame_prompt") or ""
                end_prompt = self.cleaned_data.get("end_frame_prompt") or ""
                scene["start_frame_prompt"] = start_prompt
                scene["start_frame_prompt_ru"] = start_prompt
                scene["end_frame_requested"] = bool(self.cleaned_data.get("end_frame_requested"))
                scene["end_frame_prompt"] = end_prompt
                scene["end_frame_prompt_ru"] = end_prompt
                scene["video_prompt_by_lang"] = self.cleaned_data.get("video_prompt_by_lang_json") or {}
                scene["speech_by_lang"] = self.cleaned_data.get("speech_by_lang_json") or {}
                scene["syntax_retry_context"] = self.cleaned_data.get("syntax_retry_context_json") or {}
                scene["video_approvals_by_lang"] = self.cleaned_data.get("video_approvals_by_lang_json") or {}

        if self.cleaned_data.get("apply_media_step_override"):
            media_scene_number = int(self.cleaned_data.get("media_scene_number") or 1)
            session["state"] = "waiting_scene_media"
            session["image_scene_index"] = media_scene_number - 1
            session["scene_media_substep"] = self.cleaned_data.get("media_substep") or "start_prompt"
            session["scene_media_language_index"] = int(self.cleaned_data.get("media_language_index") or 0)
            for key in _MEDIA_PENDING_KEYS:
                session.pop(key, None)

        obj.session_json = session
        obj.state = session.get("state") or obj.state
        if commit:
            obj.save()
            _sync_generation_title_from_overlay(obj, overlay_title_by_lang)
            _sync_generation_prompts_and_cursor_from_session(obj, session)
        return obj


class SceneAdminForm(forms.ModelForm):
    production_scene_json = forms.CharField(
        required=False,
        label="Production scene JSON",
        widget=forms.Textarea(attrs={"rows": 16}),
        help_text="Full scene payload from the linked ChatSession production.json_plan. Saving syncs it back.",
    )

    class Meta:
        model = Scene
        fields = (
            "run",
            "scene_order",
            "duration_sec",
            "text",
            "start_frame_prompt",
            "start_frame_prompt_effective",
            "end_frame_prompt",
            "video_prompt_by_lang",
            "speech_by_lang",
            "syntax_prompt_by_lang",
            "prompt_meta_json",
        )
        widgets = {
            "start_frame_prompt": forms.Textarea(attrs={"rows": 4}),
            "start_frame_prompt_effective": forms.Textarea(attrs={"rows": 4}),
            "end_frame_prompt": forms.Textarea(attrs={"rows": 4}),
            "video_prompt_by_lang": forms.Textarea(attrs={"rows": 8}),
            "speech_by_lang": forms.Textarea(attrs={"rows": 5}),
            "syntax_prompt_by_lang": forms.Textarea(attrs={"rows": 5}),
            "prompt_meta_json": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, production_scene: dict[str, Any] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["speech_by_lang"] = forms.CharField(
            required=False,
            label=self.fields["speech_by_lang"].label,
            help_text="Можно вставить обычную русскую речь или JSON вида {\"ru\": \"...\", \"en\": \"...\"}.",
            widget=forms.Textarea(attrs={"rows": 5}),
        )
        payload = production_scene if isinstance(production_scene, dict) else {}
        if not payload and self.instance and self.instance.pk:
            payload = {
                "scene": self.instance.scene_order,
                "text": self.instance.text,
                "duration_sec": self.instance.duration_sec,
            }
        if self.instance and self.instance.pk:
            for key in ("start_frame_prompt", "start_frame_prompt_effective", "end_frame_prompt"):
                if not payload.get(key):
                    value = getattr(self.instance, key, None)
                    if value:
                        payload[key] = value
            for key in ("video_prompt_by_lang", "speech_by_lang", "syntax_prompt_by_lang", "prompt_meta_json"):
                if not isinstance(payload.get(key), dict) or not payload.get(key):
                    value = getattr(self.instance, key, None)
                    if isinstance(value, dict) and value:
                        payload[key] = value
        self.fields["production_scene_json"].initial = _pretty(payload)
        speech_initial = payload.get("speech_by_lang") or getattr(self.instance, "speech_by_lang", None)
        self._initial_speech_by_lang = speech_initial if isinstance(speech_initial, dict) else {}
        if isinstance(speech_initial, dict) and speech_initial:
            self.fields["speech_by_lang"].initial = _pretty(speech_initial)

    def clean_speech_by_lang(self):
        return _speech_by_lang_from_admin_text(
            self.cleaned_data.get("speech_by_lang"),
            existing=self._initial_speech_by_lang,
        )

    def clean_production_scene_json(self):
        try:
            value = _loads_json(self.cleaned_data.get("production_scene_json") or "", fallback={})
        except json.JSONDecodeError as exc:
            raise forms.ValidationError(f"Invalid JSON: {exc}") from exc
        if value is not None and not isinstance(value, dict):
            raise forms.ValidationError("Expected JSON object.")
        return value or {}

    def clean(self):
        cleaned = super().clean()
        payload = dict(cleaned.get("production_scene_json") or {})
        payload["scene"] = cleaned.get("scene_order") or self.instance.scene_order
        payload["text"] = cleaned.get("text") or ""
        payload["duration_sec"] = float(cleaned.get("duration_sec") or 0.0)
        for key in ("start_frame_prompt", "start_frame_prompt_effective", "end_frame_prompt"):
            value = cleaned.get(key)
            if value:
                payload[key] = value
            else:
                payload.pop(key, None)
        for key in ("video_prompt_by_lang", "speech_by_lang", "syntax_prompt_by_lang", "prompt_meta_json"):
            value = cleaned.get(key)
            if isinstance(value, dict) and value:
                payload[key] = value
            else:
                payload.pop(key, None)
        cleaned["production_scene_json"] = payload
        return cleaned


class SceneImageCandidateAdminForm(forms.ModelForm):
    replacement_image = forms.FileField(
        required=False,
        label="Replace image",
        help_text="Upload a new image file to replace the stored frame.",
        widget=forms.ClearableFileInput(attrs={"accept": "image/*"}),
    )

    class Meta:
        model = SceneImageCandidate
        fields = ("kind", "liked", "approved_for_video")

    def clean_replacement_image(self):
        uploaded = self.cleaned_data.get("replacement_image")
        if uploaded is None:
            return None
        content_type = str(getattr(uploaded, "content_type", "") or "")
        if content_type and not content_type.startswith("image/"):
            raise forms.ValidationError("Upload an image file.")
        return uploaded

    def save(self, commit=True):
        obj: SceneImageCandidate = super().save(commit=False)
        uploaded = self.cleaned_data.get("replacement_image")
        if uploaded is not None:
            payload = uploaded.read()
            if getattr(obj, "scene_id", None) and _scene_run_id(obj):
                obj.image_blob = None
                obj.media_uri = store_scene_candidate_media(
                    run_id=_scene_run_id(obj),
                    scene_id=obj.scene_id,
                    candidate_id=obj.candidate_id,
                    data=payload,
                    extension="png",
                )
                obj.mime_type = str(getattr(uploaded, "content_type", "") or "image/png")
                obj.byte_size = len(payload)
            else:
                obj.image_blob = payload
        if commit:
            obj.save()
        return obj


class SceneVideoCandidateAdminForm(forms.ModelForm):
    replacement_video = forms.FileField(
        required=False,
        label="Replace video",
        help_text="Upload a new video file to replace the stored scene video.",
        widget=forms.ClearableFileInput(attrs={"accept": "video/*"}),
    )

    class Meta:
        model = SceneVideoCandidate
        fields = ("liked", "approved_for_production", "speed_coef")

    def clean_replacement_video(self):
        uploaded = self.cleaned_data.get("replacement_video")
        if uploaded is None:
            return None
        content_type = str(getattr(uploaded, "content_type", "") or "")
        if content_type and not content_type.startswith("video/"):
            raise forms.ValidationError("Upload a video file.")
        return uploaded

    def save(self, commit=True):
        obj: SceneVideoCandidate = super().save(commit=False)
        uploaded = self.cleaned_data.get("replacement_video")
        if uploaded is not None:
            payload = uploaded.read()
            if getattr(obj, "scene_id", None) and _scene_run_id(obj):
                obj.video_blob = None
                obj.media_uri = store_scene_candidate_media(
                    run_id=_scene_run_id(obj),
                    scene_id=obj.scene_id,
                    candidate_id=obj.candidate_id,
                    data=payload,
                    extension="mp4",
                )
                obj.mime_type = str(getattr(uploaded, "content_type", "") or "video/mp4")
                obj.byte_size = len(payload)
            else:
                obj.video_blob = payload
        if commit:
            obj.save()
        return obj
