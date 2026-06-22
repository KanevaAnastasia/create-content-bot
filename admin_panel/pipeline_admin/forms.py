from __future__ import annotations

import json
from typing import Any

from django import forms

from .models import ChatSession, SceneImageCandidate, SceneVideoCandidate


def _pretty(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, indent=2, sort_keys=True)


def _loads_json(value: str, *, fallback: Any) -> Any:
    text = (value or "").strip()
    if not text:
        return fallback
    return json.loads(text)


def _safe_scene_index(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


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

    scene_index = forms.IntegerField(required=False, min_value=0, help_text="0-based scene index to edit below.")
    start_frame_prompt = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 4}))
    end_frame_requested = forms.BooleanField(required=False)
    end_frame_prompt = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 4}))
    video_prompt_by_lang_json = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 8}))
    speech_by_lang_json = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 6}))
    syntax_retry_context_json = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 6}))
    video_approvals_by_lang_json = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 4}))

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
        self.fields["scene_index"].initial = scene_idx
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
            "video_prompt_by_lang_json": dict,
            "speech_by_lang_json": dict,
            "syntax_retry_context_json": dict,
            "video_approvals_by_lang_json": dict,
        }
        for field_name, expected_type in expected_types.items():
            value = cleaned.get(field_name)
            if value is not None and not isinstance(value, expected_type):
                self.add_error(field_name, f"Expected JSON {expected_type.__name__}.")
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

        obj.session_json = session
        obj.state = session.get("state") or obj.state
        if commit:
            obj.save()
        return obj


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
            obj.image_blob = uploaded.read()
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
            obj.video_blob = uploaded.read()
        if commit:
            obj.save()
        return obj
