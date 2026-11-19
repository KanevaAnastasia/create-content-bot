from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pytest
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory

from test_publication_upload_admin import admin_module
from pipeline_admin import tasks
from pipeline_admin.models import Project, PublicationJob


@pytest.fixture
def recovery_admin(monkeypatch, tmp_path):
    video = tmp_path / "approved.mp4"
    video.write_bytes(b"approved")
    job = PublicationJob(
        id=uuid.uuid4(), project=Project(id=uuid.uuid4(), slug="discipline"),
        bundle_key="bundle", platform="instagram", language="ru", status="failed",
        video_path=str(video), title="Approved title", description="Approved caption",
        payload_json={"instagram_upload": {"stage": "upload_rejected", "account_id": "account"}},
    )
    job.save = Mock()
    model_admin = admin_module.PublicationJobAdmin(PublicationJob, admin_module.admin.site)
    monkeypatch.setattr(model_admin, "get_object", lambda *_: job)
    monkeypatch.setattr(model_admin, "has_change_permission", lambda *_: True)
    monkeypatch.setattr(model_admin.admin_site, "each_context", lambda *_: {})
    monkeypatch.setattr(model_admin, "message_user", Mock())
    manager = Mock()
    manager.select_for_update.return_value.get.return_value = job
    monkeypatch.setattr(PublicationJob, "objects", manager)
    monkeypatch.setattr(admin_module.transaction, "atomic", nullcontext)
    dispatch = Mock()
    monkeypatch.setattr(tasks, "dispatch_publication_jobs", dispatch)

    def request(method="post", data=None):
        req = getattr(RequestFactory(), method)("/recovery/", data or {})
        req.user = SimpleNamespace(pk="operator")
        return req

    return model_admin, job, manager, dispatch, request


def test_recovery_get_previews_exact_scope_without_dispatch(recovery_admin):
    model_admin, job, _, dispatch, request = recovery_admin
    response = model_admin.instagram_recovery_view(request("get"), job.pk)
    html = response.render().content.decode()
    for expected in ("discipline", "account", "RU", "Approved caption", "tmpfiles.org", "через час"):
        assert expected in html
    dispatch.assert_not_called()
    job.save.assert_not_called()


def test_recovery_post_pins_consent_preserves_container_and_dispatches_only_selected_job(recovery_admin):
    model_admin, job, _, dispatch, request = recovery_admin
    response = model_admin.instagram_recovery_view(request(data={"confirm": "tmpfiles"}), job.pk)
    assert response.status_code == 302
    assert job.status == "approved"
    assert job.payload_json["instagram_upload"]["account_id"] == "account"
    consent = job.payload_json["instagram_url_recovery"]
    assert consent["requested_by"] == "operator"
    assert consent["scope"]["job_id"] == str(job.id)
    assert consent["scope"]["project_id"] == str(job.project_id)
    dispatch.assert_called_once_with([str(job.id)])
    model_admin.instagram_recovery_view(request(data={"confirm": "tmpfiles"}), job.pk)
    assert dispatch.call_count == 1


def test_recovery_requires_change_permission(recovery_admin, monkeypatch):
    model_admin, job, _, dispatch, request = recovery_admin
    monkeypatch.setattr(model_admin, "has_change_permission", lambda *_: False)
    with pytest.raises(PermissionDenied):
        model_admin.instagram_recovery_view(request(data={"confirm": "tmpfiles"}), job.pk)
    dispatch.assert_not_called()


@pytest.mark.parametrize("blocked", ["published", "publishing", "approved", "ambiguous"])
def test_stale_recovery_page_cannot_override_new_state(recovery_admin, blocked):
    model_admin, job, manager, dispatch, request = recovery_admin
    locked = SimpleNamespace(**job.__dict__)
    if blocked == "ambiguous":
        locked.payload_json = {"instagram_upload": {"stage": "upload_rejected", "publish_ambiguous": True}}
    else:
        locked.status = blocked
    manager.select_for_update.return_value.get.return_value = locked
    model_admin.instagram_recovery_view(request(data={"confirm": "tmpfiles"}), job.pk)
    dispatch.assert_not_called()
    job.save.assert_not_called()


def test_queue_failure_restores_only_unchanged_request(recovery_admin):
    model_admin, job, manager, dispatch, request = recovery_admin
    old_payload = job.payload_json
    dispatch.side_effect = RuntimeError("queue unavailable")
    model_admin.instagram_recovery_view(request(data={"confirm": "tmpfiles"}), job.pk)
    assert job.status == "failed"
    assert job.payload_json == old_payload
    assert manager.select_for_update.call_count == 2
    assert job.save.call_count == 2


def test_queue_error_cannot_revert_job_already_claimed_by_worker(recovery_admin):
    model_admin, job, _, dispatch, request = recovery_admin

    def dispatch_then_fail(*_):
        job.status = "publishing"
        raise RuntimeError("queue acknowledgement lost")

    dispatch.side_effect = dispatch_then_fail
    model_admin.instagram_recovery_view(request(data={"confirm": "tmpfiles"}), job.pk)
    assert job.status == "publishing"
    assert job.payload_json["instagram_url_recovery"]["provider"] == "tmpfiles"
    assert job.save.call_count == 1


def test_plain_post_does_not_authorize_hosting(recovery_admin):
    model_admin, job, _, dispatch, request = recovery_admin
    response = model_admin.instagram_recovery_view(request(), job.pk)
    assert response.status_code == 200
    dispatch.assert_not_called()
