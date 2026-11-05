"""Read tagged Instagram videos and their current lifetime view counts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from .character_catalog import GRANDMA_KITCHEN_PROJECT_SLUG
from .publication_config import project_env, shared_env
from .social_publishers import _instagram_access_token

MOSCOW = ZoneInfo("Europe/Moscow")
MENTION = re.compile(r"(?<![\w.@])@syntx_ai(?![\w]|\.[\w])", re.IGNORECASE)


class InstagramStatsError(RuntimeError):
    """Safe to display; never includes credentials or raw API responses."""


class InstagramInsightsPermissionError(InstagramStatsError):
    """Avoid repeating a known permission failure for every video in an account."""


@dataclass(frozen=True)
class InstagramAccount:
    project: str
    language: str
    account_id: str

    @property
    def label(self) -> str:
        return f"{self.project} / {self.language.upper()}"


@dataclass(frozen=True)
class InstagramVideo:
    media_id: str
    account: str
    published_at: datetime
    permalink: str
    views: int | None


@dataclass
class InstagramReport:
    start: date
    as_of: datetime
    videos: list[InstagramVideo] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    accounts: int = 0
    listing_complete: bool = True


def moscow_now() -> datetime:
    return datetime.now(MOSCOW)


def default_start(today: date) -> date:
    month = today if today.day >= 10 else today.replace(day=1) - timedelta(days=1)
    return month.replace(day=10)


def configured_accounts() -> list[InstagramAccount]:
    """The sponsor report is exclusively for Grandma Kitchen's Russian account."""
    account_id = project_env(GRANDMA_KITCHEN_PROJECT_SLUG, "INSTAGRAM_BUSINESS_ACCOUNT_ID_RU")
    if not account_id:
        return []
    return [InstagramAccount(GRANDMA_KITCHEN_PROJECT_SLUG, "ru", account_id)]


def _get(session: requests.Session, path: str, **params) -> dict:
    version = shared_env("META_GRAPH_API_VERSION", "v25.0")
    try:
        response = session.get(
            f"https://graph.facebook.com/{version}/{path}",
            params=params, timeout=(10, 30), allow_redirects=False,
        )
        payload = response.json()
    except (requests.RequestException, ValueError):
        raise InstagramStatsError("Instagram временно недоступен. Попробуйте ещё раз.") from None
    if not isinstance(payload, dict):
        raise InstagramStatsError("Instagram вернул некорректный ответ.")
    if not response.ok or payload.get("error"):
        error = payload.get("error") or {}
        code = error.get("code") if isinstance(error, dict) else None
        if code in {10, 200}:
            error_type = InstagramInsightsPermissionError if path.endswith("/insights") else InstagramStatsError
            raise error_type(
                "Нет доступа к данным Instagram. Для просмотров нужно разрешение "
                "instagram_manage_insights при повторном подключении аккаунта."
            )
        if code == 190:
            raise InstagramStatsError("Доступ Instagram истёк: подключите аккаунт повторно.")
        raise InstagramStatsError("Instagram не отдал данные. Попробуйте ещё раз.")
    return payload


def _media(session: requests.Session, account_id: str):
    # Traverse every page: the media edge does not support time-based pagination
    # or a guaranteed sort order. Never follow API-provided URLs with our token.
    after = None
    seen = set()
    for _ in range(500):
        params = {"fields": "id,caption,media_type,media_product_type,permalink,timestamp", "limit": 100}
        if after:
            params["after"] = after
        payload = _get(session, f"{account_id}/media", **params)
        items = payload.get("data")
        if not isinstance(items, list):
            raise InstagramStatsError("Instagram не отдал список публикаций полностью.")
        yield from items
        paging = payload.get("paging") or {}
        if not paging.get("next"):
            return
        after = (paging.get("cursors") or {}).get("after")
        if not after or after in seen:
            break
        seen.add(after)
    raise InstagramStatsError("Не удалось прочитать все страницы публикаций Instagram; итог неполный.")


def _views(session: requests.Session, media_id: str) -> int | None:
    payload = _get(session, f"{media_id}/insights", metric="views")
    for metric in payload.get("data", []):
        if metric.get("name") != "views":
            continue
        values = metric.get("values") or []
        value = values[0].get("value") if values else (metric.get("total_value") or {}).get("value")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def fetch_instagram_report(start: date, *, now: datetime | None = None) -> InstagramReport:
    now = (now or moscow_now()).astimezone(MOSCOW)
    if start > now.date():
        raise ValueError("Start date must not be in the future")
    report = InstagramReport(start=start, as_of=now)
    accounts = configured_accounts()
    report.accounts = len(accounts)
    seen_media = set()
    for account in accounts:
        try:
            try:
                token = _instagram_access_token(account.project, account.language)
            except Exception:
                raise InstagramStatsError("Не удалось получить доступ: проверьте подключение Instagram.") from None
            if not token:
                raise InstagramStatsError("Не настроен токен доступа Instagram.")
            with requests.Session() as session:
                session.headers.update({"Authorization": f"Bearer {token}"})
                insights_denied = False
                for item in _media(session, account.account_id):
                    if item.get("media_type") != "VIDEO" or item.get("media_product_type") == "STORY":
                        continue
                    if not MENTION.search(str(item.get("caption") or "")):
                        continue
                    try:
                        published = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
                        if published.tzinfo is None:
                            raise ValueError("Missing timezone")
                        published = published.astimezone(MOSCOW)
                    except (KeyError, ValueError, TypeError, AttributeError):
                        raise InstagramStatsError("Instagram не отдал дату ролика; итог неполный.") from None
                    if published.date() < start or published > now:
                        continue
                    media_id = str(item.get("id") or "")
                    if not media_id:
                        raise InstagramStatsError("Instagram не отдал идентификатор ролика; итог неполный.")
                    if media_id in seen_media:
                        continue
                    seen_media.add(media_id)
                    views = None
                    try:
                        if not insights_denied:
                            views = _views(session, media_id)
                    except InstagramStatsError as exc:
                        insights_denied = isinstance(exc, InstagramInsightsPermissionError)
                        warning = f"{account.label}: {exc}"
                        if warning not in report.warnings:
                            report.warnings.append(warning)
                    report.videos.append(InstagramVideo(
                        media_id, account.label, published, str(item.get("permalink") or ""), views,
                    ))
        except InstagramStatsError as exc:
            report.listing_complete = False
            report.warnings.append(f"{account.label}: {exc}")
    report.videos.sort(key=lambda video: (video.published_at, video.media_id), reverse=True)
    return report
