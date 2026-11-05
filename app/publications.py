"""Publication bundle assembly and queue persistence for direct social delivery."""

from __future__ import annotations

import base64
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Project, ProjectPublishAccount, PublicationJob
from .publication_config import project_env, project_env_name


PUBLICATION_STATUS_DRAFT = "draft"
PUBLICATION_STATUS_APPROVED = "approved"
PUBLICATION_STATUS_SCHEDULED = "scheduled"
PUBLICATION_STATUS_PUBLISHING = "publishing"
PUBLICATION_STATUS_PUBLISHED = "published"
PUBLICATION_STATUS_FAILED = "failed"
PUBLICATION_STATUS_NEEDS_MANUAL_ACTION = "needs_manual_action"

PUBLICATION_ARTIFACTS_DIR_ENV_NAME = "PUBLICATION_ARTIFACTS_DIR"
DEFAULT_PUBLICATION_ARTIFACTS_DIR = "artifacts/publications"
GRANDMA_PROJECT_SLUG = "grandma_kitchen"
GRANDMA_INSTAGRAM_RU_ATTRIBUTION = "Сделано с помощью @syntx_ai"

_HASHTAG_RE = re.compile(r"#[\wа-яА-ЯёЁ]+", flags=re.UNICODE)
_ENV_ACCOUNT_SPECS = (
    ("youtube", "ru", "default", "YOUTUBE_CHANNEL_ID_RU"),
    ("youtube", "en", "default", "YOUTUBE_CHANNEL_ID_EN"),
    ("instagram", "ru", "default", "INSTAGRAM_BUSINESS_ACCOUNT_ID_RU"),
    ("instagram", "en", "default", "INSTAGRAM_BUSINESS_ACCOUNT_ID_EN"),
    ("tiktok", "ru", "default", "TIKTOK_OPEN_ID"),
    ("vk", "ru", "default", "VK_GROUP_ID_RU"),
)


@dataclass(frozen=True, slots=True)
class PublicationTarget:
    platform: str
    language: str
    title: str | None
    description: str
    video_label: str

    @property
    def key(self) -> str:
        return publication_target_key(self.platform, self.language)


PUBLICATION_VIDEO_VARIANT_LABELS = {
    "clean": "clean master",
    "upscaled_4k": "4K master",
    "subtitled_music": "subtitled/music master",
    "titled_subtitled_music": "title/subtitled/music master",
}
DEFAULT_PUBLICATION_VIDEO_VARIANT = "titled_subtitled_music"

PUBLICATION_TARGET_LABELS = {
    "youtube:ru": "YouTube Shorts RU",
    "instagram:ru": "Instagram RU",
    "vk:ru": "VK RU",
    "tiktok:ru": "TikTok RU",
    "instagram:en": "Instagram EN",
    "youtube:en": "YouTube Shorts EN",
}


def publication_target_key(platform: str, language: str) -> str:
    return f"{str(platform or '').strip().lower()}:{str(language or '').strip().lower()}"


def normalize_publication_target_keys(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        raw_items = re.split(r"[\s,;]+", value)
    else:
        raw_items = list(value) if isinstance(value, (list, tuple, set)) else []
    out: set[str] = set()
    for item in raw_items:
        text = str(item or "").strip().lower()
        if not text:
            continue
        if ":" not in text and "/" in text:
            text = text.replace("/", ":", 1)
        if ":" in text:
            platform, language = text.split(":", 1)
            out.add(publication_target_key(platform, language))
    return out


def publication_target_label(target_or_key: PublicationTarget | str) -> str:
    key = target_or_key.key if isinstance(target_or_key, PublicationTarget) else str(target_or_key or "")
    return PUBLICATION_TARGET_LABELS.get(key, key)


def normalize_publication_video_variant(value: str | None) -> str:
    variant = str(value or "").strip()
    if variant in PUBLICATION_VIDEO_VARIANT_LABELS:
        return variant
    return DEFAULT_PUBLICATION_VIDEO_VARIANT


def normalize_publication_scheduled_at(value: Any, *, default_timezone=timezone.utc) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        scheduled = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            scheduled = datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M"):
                try:
                    scheduled = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    scheduled = None
            if scheduled is None:
                raise ValueError("Publication time must be ISO-like, YYYY-MM-DD HH:MM, or DD.MM.YYYY HH:MM.")
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=default_timezone)
    return scheduled.astimezone(timezone.utc)


def publication_artifacts_dir() -> Path:
    raw = (os.getenv(PUBLICATION_ARTIFACTS_DIR_ENV_NAME) or "").strip()
    base = Path(raw or DEFAULT_PUBLICATION_ARTIFACTS_DIR)
    if not base.is_absolute():
        base = Path(__file__).resolve().parent.parent / base
    return base


def extract_hashtags(text: str) -> list[str]:
    seen: list[str] = []
    for tag in _HASHTAG_RE.findall(str(text or "")):
        if tag not in seen:
            seen.append(tag)
    return seen


def instagram_ru_attribution_text(text: str) -> str:
    """Place the Grandma project attribution immediately before hashtags."""
    clean = str(text or "").strip()
    attribution_pattern = re.compile(
        rf"(?im)^\s*{re.escape(GRANDMA_INSTAGRAM_RU_ATTRIBUTION)}\s*$"
    )
    clean = attribution_pattern.sub("", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    if not clean:
        return ""
    hashtag = _HASHTAG_RE.search(clean)
    if hashtag is None:
        return f"{clean}\n\n{GRANDMA_INSTAGRAM_RU_ATTRIBUTION}"
    body = clean[: hashtag.start()].rstrip()
    tags = clean[hashtag.start() :].lstrip()
    return f"{body}\n\n{GRANDMA_INSTAGRAM_RU_ATTRIBUTION}\n\n{tags}".strip()


def strip_instagram_ru_attribution(text: str) -> str:
    clean = re.sub(
        rf"(?im)^\s*{re.escape(GRANDMA_INSTAGRAM_RU_ATTRIBUTION)}\s*$",
        "",
        str(text or ""),
    )
    return re.sub(r"\n{3,}", "\n\n", clean).strip()


def apply_project_publication_copy_rules(
    project_slug: str | None,
    copy: dict[str, str],
) -> dict[str, str]:
    """Apply project-owned copy rules before previewing or persisting jobs."""
    out = {str(key): str(value or "") for key, value in copy.items()}
    instagram_ru = str(out.get("instagram_ru") or "")
    if not instagram_ru.strip():
        return out
    without_attribution = strip_instagram_ru_attribution(instagram_ru)
    out["instagram_ru"] = (
        instagram_ru_attribution_text(without_attribution)
        if str(project_slug or "").strip() == GRANDMA_PROJECT_SLUG
        else without_attribution
    )
    return out


def build_publication_targets(
    copy: dict[str, str],
    *,
    selected_target_keys: Any = None,
) -> list[PublicationTarget]:
    selected = normalize_publication_target_keys(selected_target_keys)
    targets = [
        PublicationTarget(
            platform="youtube",
            language="ru",
            title=str(copy.get("youtube_ru_title") or "").strip() or None,
            description=str(copy.get("youtube_ru_description") or "").strip(),
            video_label="RU video / final master",
        ),
        PublicationTarget(
            platform="instagram",
            language="ru",
            title=None,
            description=str(copy.get("instagram_ru") or "").strip(),
            video_label="RU video / final master",
        ),
        PublicationTarget(
            platform="vk",
            language="ru",
            title=None,
            description=str(copy.get("vk_ru") or "").strip(),
            video_label="RU video / final master",
        ),
        PublicationTarget(
            platform="tiktok",
            language="ru",
            title=None,
            description=str(copy.get("tiktok_ru") or "").strip(),
            video_label="RU video / final master",
        ),
        PublicationTarget(
            platform="instagram",
            language="en",
            title=None,
            description=str(copy.get("instagram_en") or "").strip(),
            video_label="EN video / final master",
        ),
        PublicationTarget(
            platform="youtube",
            language="en",
            title=str(copy.get("youtube_en_title") or "").strip() or None,
            description=str(copy.get("youtube_en_description") or "").strip(),
            video_label="EN video / final master",
        ),
    ]
    if not selected:
        return targets
    return [target for target in targets if target.key in selected]


def resolve_project_for_publications(session: Session, project_slug: str | None) -> Project:
    if project_slug:
        project = session.scalar(select(Project).where(Project.slug == project_slug))
        if project is not None:
            return project
        raise ValueError(f"Project not found for publication bundle: {project_slug}")
    project = session.scalar(select(Project).where(Project.is_default.is_(True)))
    if project is None:
        raise ValueError("No project available for publication bundle.")
    return project


def upsert_project_publish_accounts_from_env(session: Session, project: Project) -> list[ProjectPublishAccount]:
    existing = session.scalars(
        select(ProjectPublishAccount).where(ProjectPublishAccount.project_id == project.id)
    ).all()
    index = {(row.platform, row.language, row.account_key): row for row in existing}
    out = list(existing)
    seen_keys: set[tuple[str, str, str]] = set()
    for platform, language, account_key, env_name in _ENV_ACCOUNT_SPECS:
        key = (platform, language, account_key)
        seen_keys.add(key)
        external_account_id = project_env(project.slug, env_name)
        if not external_account_id:
            row = index.get(key)
            if row is not None:
                row.external_account_id = None
                row.is_active = False
                row.meta_json = {**(row.meta_json or {}), "source_env": project_env_name(project.slug, env_name)}
            continue
        row = index.get(key)
        if row is None:
            row = ProjectPublishAccount(
                project_id=project.id,
                platform=platform,
                language=language,
                account_key=account_key,
                external_account_id=external_account_id,
                meta_json={"source_env": project_env_name(project.slug, env_name)},
            )
            session.add(row)
            out.append(row)
            index[key] = row
        else:
            row.external_account_id = external_account_id
            row.is_active = True
            row.meta_json = {**(row.meta_json or {}), "source_env": project_env_name(project.slug, env_name)}
    for key, row in index.items():
        if key in seen_keys:
            continue
        if row.external_account_id:
            continue
        row.is_active = False
    session.flush()
    return out


def _render_payload_for_language(post_production: dict[str, Any], language: str) -> dict[str, Any] | None:
    renders = post_production.get("renders_by_language")
    if isinstance(renders, dict):
        payload = renders.get(language)
        return payload if isinstance(payload, dict) else None
    return None


def export_publication_video_assets(
    post_production: dict[str, Any],
    *,
    bundle_key: str,
    base_dir: Path | None = None,
    video_variant: str | None = None,
) -> dict[str, str]:
    target_dir = (base_dir or publication_artifacts_dir()) / bundle_key
    target_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    renders = post_production.get("renders_by_language")
    if not isinstance(renders, dict):
        return out
    preferred_variant = normalize_publication_video_variant(video_variant)
    for language, payload in renders.items():
        if not isinstance(payload, dict):
            continue
        source = payload.get(preferred_variant)
        if not isinstance(source, dict):
            source = (
                payload.get("titled_subtitled_music")
                or payload.get("subtitled_music")
                or payload.get("upscaled_4k")
                or payload.get("clean")
                or {}
            )
        filename = f"publish_{language}.mp4"
        path = target_dir / filename
        video_path = str((source or {}).get("video_path") or "").strip()
        if video_path and Path(video_path).exists():
            shutil.copyfile(video_path, path)
            out[str(language)] = str(path.resolve())
            continue
        video_b64 = str((source or {}).get("video_b64") or "").strip()
        if not video_b64:
            continue
        try:
            video_bytes = base64.b64decode(video_b64)
        except Exception:
            continue
        path.write_bytes(video_bytes)
        out[str(language)] = str(path.resolve())
    return out


def export_publication_cover_assets(
    cover_sources: dict[str, str | Path] | None,
    *,
    bundle_key: str,
    base_dir: Path | None = None,
) -> dict[str, str]:
    """Copy optional per-language covers into the immutable publication bundle."""
    target_dir = (base_dir or publication_artifacts_dir()) / bundle_key
    target_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    for language, raw_source in (cover_sources or {}).items():
        source = Path(raw_source)
        if not source.is_file():
            continue
        suffix = source.suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png"}:
            continue
        target = target_dir / f"publish_cover_{str(language).lower()}{suffix}"
        shutil.copyfile(source, target)
        out[str(language).lower()] = str(target.resolve())
    return out


def queue_publication_bundle(
    session: Session,
    *,
    project_slug: str | None,
    recipe: str,
    copy: dict[str, str],
    post_production: dict[str, Any],
    origin: str = "production_final",
    selected_target_keys: Any = None,
    video_variant: str | None = None,
    video_source_labels: dict[str, str] | None = None,
    cover_sources: dict[str, str | Path] | None = None,
    trial_copy: dict[str, str] | None = None,
    trial_target_keys: Any = None,
    scheduled_at: Any = None,
) -> list[PublicationJob]:
    from .social_publishers import platform_readiness

    project = resolve_project_for_publications(session, project_slug)
    copy = apply_project_publication_copy_rules(project.slug, copy)
    trial_targets = normalize_publication_target_keys(trial_target_keys) & {"instagram:ru"}
    accounts = upsert_project_publish_accounts_from_env(session, project)
    account_index = {
        (row.platform, row.language): row
        for row in accounts
        if row.is_active
    }
    bundle_key = str(uuid.uuid4())
    normalized_variant = normalize_publication_video_variant(video_variant)
    video_paths = export_publication_video_assets(
        post_production,
        bundle_key=bundle_key,
        video_variant=normalized_variant,
    )
    cover_paths = export_publication_cover_assets(
        cover_sources,
        bundle_key=bundle_key,
    )
    approved_at = datetime.now(timezone.utc)
    normalized_scheduled_at = normalize_publication_scheduled_at(scheduled_at)
    jobs: list[PublicationJob] = []
    for target in build_publication_targets(copy, selected_target_keys=selected_target_keys):
        video_path = video_paths.get(target.language)
        if not video_path or not target.description:
            continue
        account = account_index.get((target.platform, target.language))
        readiness = platform_readiness(project.slug, target.platform, target.language)
        status = (
            PUBLICATION_STATUS_APPROVED
            if readiness.ready and account and account.external_account_id
            else PUBLICATION_STATUS_NEEDS_MANUAL_ACTION
        )
        if normalized_scheduled_at is not None and status == PUBLICATION_STATUS_APPROVED:
            status = PUBLICATION_STATUS_SCHEDULED
        account_key = account.account_key if account else "default"
        external_account_id = account.external_account_id if account else None
        publish_policy = account.publish_policy if account else "manual_review_required"
        source_label = str((video_source_labels or {}).get(target.language) or "").strip()
        variants: list[tuple[str, bool, str]] = []
        if target.key in trial_targets:
            trial_description = strip_instagram_ru_attribution(
                str((trial_copy or {}).get("instagram_ru") or "")
            )
            if not trial_description:
                raise ValueError("Instagram RU Trial Reel requires its own description.")
            variants.append((trial_description, True, f"{bundle_key}-trial"))
        variants.append((target.description, False, bundle_key))
        predecessor: PublicationJob | None = None
        for description, is_trial, job_bundle_key in variants:
            job = PublicationJob(
                id=uuid.uuid4(),
                project_id=project.id,
                bundle_key=job_bundle_key,
                origin=origin,
                platform=target.platform,
                language=target.language,
                account_key=account_key,
                external_account_id=external_account_id,
                status=status,
                video_path=video_path,
                video_label=source_label or f"{target.language.upper()} video / {PUBLICATION_VIDEO_VARIANT_LABELS[normalized_variant]}",
                title=target.title,
                description=description,
                hashtags_json=extract_hashtags(description),
                recipe=recipe,
                publish_policy=publish_policy,
                scheduled_at=normalized_scheduled_at,
                approved_at=approved_at if status in {PUBLICATION_STATUS_APPROVED, PUBLICATION_STATUS_SCHEDULED} else None,
                payload_json={
                    "bundle_key": job_bundle_key,
                    "platform": target.platform,
                    "language": target.language,
                    "video_path": video_path,
                    "title": target.title,
                    "description": description,
                    "hashtags": extract_hashtags(description),
                    "target_key": target.key,
                    "target_label": publication_target_label(target),
                    "video_variant": normalized_variant,
                    "video_source_label": source_label or None,
                    "cover_path": cover_paths.get(target.language),
                    "instagram_trial": is_trial,
                    "publish_after_job_id": str(predecessor.id) if predecessor is not None else None,
                    "scheduled_at": normalized_scheduled_at.isoformat() if normalized_scheduled_at else None,
                    "direct_api_ready": readiness.ready,
                    "missing_configuration": list(readiness.missing),
                },
            )
            if not readiness.ready:
                job.publish_error = readiness.note or f"Missing configuration: {', '.join(readiness.missing)}"
            session.add(job)
            jobs.append(job)
            if is_trial:
                predecessor = job
    session.flush()
    return jobs


def publication_jobs_summary(jobs: list[PublicationJob]) -> str:
    if not jobs:
        return "Публикационные jobs не созданы."
    approved = [job for job in jobs if job.status == PUBLICATION_STATUS_APPROVED]
    scheduled = [job for job in jobs if job.status == PUBLICATION_STATUS_SCHEDULED]
    manual = [job for job in jobs if job.status == PUBLICATION_STATUS_NEEDS_MANUAL_ACTION]
    lines = [
        f"Создано публикационных jobs: {len(jobs)}.",
        f"Готовы к отправке через официальные API: {len(approved) + len(scheduled)}.",
        f"Запланированы: {len(scheduled)}.",
        f"Требуют ручной/полуручной ветки: {len(manual)}.",
    ]
    for job in jobs:
        lines.append(
            f"- {job.platform} {job.language}: {job.status} -> {job.video_path}"
        )
    return "\n".join(lines)
