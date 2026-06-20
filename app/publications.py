"""Publication bundle assembly and queue persistence for n8n/social delivery."""

from __future__ import annotations

import base64
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Project, ProjectPublishAccount, PublicationJob


PUBLICATION_STATUS_DRAFT = "draft"
PUBLICATION_STATUS_APPROVED = "approved"
PUBLICATION_STATUS_SCHEDULED = "scheduled"
PUBLICATION_STATUS_PUBLISHING = "publishing"
PUBLICATION_STATUS_PUBLISHED = "published"
PUBLICATION_STATUS_FAILED = "failed"
PUBLICATION_STATUS_NEEDS_MANUAL_ACTION = "needs_manual_action"

PUBLICATION_ARTIFACTS_DIR_ENV_NAME = "PUBLICATION_ARTIFACTS_DIR"
DEFAULT_PUBLICATION_ARTIFACTS_DIR = "artifacts/publications"

_HASHTAG_RE = re.compile(r"#[\wа-яА-ЯёЁ]+", flags=re.UNICODE)
_ENV_ACCOUNT_SPECS = (
    ("youtube", "ru", "default", "YOUTUBE_CHANNEL_ID_RU"),
    ("youtube", "en", "default", "YOUTUBE_CHANNEL_ID_EN"),
    ("instagram", "ru", "default", "INSTAGRAM_BUSINESS_ACCOUNT_ID_RU"),
    ("instagram", "en", "default", "INSTAGRAM_BUSINESS_ACCOUNT_ID_EN"),
    ("tiktok", "ru", "default", "TIKTOK_OPEN_ID"),
    ("vk", "ru", "default", "VK_GROUP_ID_RU"),
    ("rutube", "ru", "default", "RUTUBE_CHANNEL_ID_RU"),
)


@dataclass(frozen=True, slots=True)
class PublicationTarget:
    platform: str
    language: str
    title: str | None
    description: str
    video_label: str


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


def build_publication_targets(copy: dict[str, str]) -> list[PublicationTarget]:
    return [
        PublicationTarget(
            platform="youtube",
            language="ru",
            title=str(copy.get("youtube_ru_title") or "").strip() or None,
            description=str(copy.get("youtube_ru_description") or "").strip(),
            video_label="RU video / final master",
        ),
        PublicationTarget(
            platform="rutube",
            language="ru",
            title=str(copy.get("rutube_ru_title") or "").strip() or None,
            description=str(copy.get("rutube_ru_description") or "").strip(),
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
        external_account_id = (os.getenv(env_name) or "").strip()
        if not external_account_id:
            row = index.get(key)
            if row is not None:
                row.external_account_id = None
                row.is_active = False
                row.meta_json = {**(row.meta_json or {}), "source_env": env_name}
            continue
        row = index.get(key)
        if row is None:
            row = ProjectPublishAccount(
                project_id=project.id,
                platform=platform,
                language=language,
                account_key=account_key,
                external_account_id=external_account_id,
                meta_json={"source_env": env_name},
            )
            session.add(row)
            out.append(row)
            index[key] = row
        else:
            row.external_account_id = external_account_id
            row.is_active = True
            row.meta_json = {**(row.meta_json or {}), "source_env": env_name}
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
) -> dict[str, str]:
    target_dir = (base_dir or publication_artifacts_dir()) / bundle_key
    target_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    renders = post_production.get("renders_by_language")
    if not isinstance(renders, dict):
        return out
    for language, payload in renders.items():
        if not isinstance(payload, dict):
            continue
        source = payload.get("subtitled_music") or payload.get("clean") or {}
        filename = f"publish_{language}.mp4"
        path = target_dir / filename
        video_path = str((source or {}).get("video_path") or "").strip()
        if video_path and Path(video_path).exists():
            path.write_bytes(Path(video_path).read_bytes())
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


def queue_publication_bundle(
    session: Session,
    *,
    project_slug: str | None,
    recipe: str,
    copy: dict[str, str],
    post_production: dict[str, Any],
    origin: str = "production_final",
) -> list[PublicationJob]:
    project = resolve_project_for_publications(session, project_slug)
    accounts = upsert_project_publish_accounts_from_env(session, project)
    account_index = {
        (row.platform, row.language): row
        for row in accounts
        if row.is_active
    }
    bundle_key = str(uuid.uuid4())
    video_paths = export_publication_video_assets(post_production, bundle_key=bundle_key)
    approved_at = datetime.now(timezone.utc)
    jobs: list[PublicationJob] = []
    for target in build_publication_targets(copy):
        video_path = video_paths.get(target.language)
        if not video_path or not target.description:
            continue
        account = account_index.get((target.platform, target.language))
        status = PUBLICATION_STATUS_APPROVED if account and account.external_account_id else PUBLICATION_STATUS_NEEDS_MANUAL_ACTION
        account_key = account.account_key if account else "default"
        external_account_id = account.external_account_id if account else None
        publish_policy = account.publish_policy if account else "manual_review_required"
        job = PublicationJob(
            project_id=project.id,
            bundle_key=bundle_key,
            origin=origin,
            platform=target.platform,
            language=target.language,
            account_key=account_key,
            external_account_id=external_account_id,
            status=status,
            video_path=video_path,
            video_label=target.video_label,
            title=target.title,
            description=target.description,
            hashtags_json=extract_hashtags(target.description),
            recipe=recipe,
            publish_policy=publish_policy,
            approved_at=approved_at if status == PUBLICATION_STATUS_APPROVED else None,
            payload_json={
                "bundle_key": bundle_key,
                "platform": target.platform,
                "language": target.language,
                "video_path": video_path,
                "title": target.title,
                "description": target.description,
                "hashtags": extract_hashtags(target.description),
            },
        )
        session.add(job)
        jobs.append(job)
    session.flush()
    return jobs


def publication_jobs_summary(jobs: list[PublicationJob]) -> str:
    if not jobs:
        return "Публикационные jobs не созданы."
    approved = [job for job in jobs if job.status == PUBLICATION_STATUS_APPROVED]
    manual = [job for job in jobs if job.status == PUBLICATION_STATUS_NEEDS_MANUAL_ACTION]
    lines = [
        f"Создано публикационных jobs: {len(jobs)}.",
        f"Автоготовы для n8n: {len(approved)}.",
        f"Требуют ручной/полуручной ветки: {len(manual)}.",
    ]
    for job in jobs:
        lines.append(
            f"- {job.platform} {job.language}: {job.status} -> {job.video_path}"
        )
    return "\n".join(lines)
