from __future__ import annotations

from pathlib import Path

from django.http import FileResponse, Http404

from app.publication_media import publication_media_token_is_valid

from .models import PublicationJob


def publication_media_view(request, job_id, token):
    job = PublicationJob.objects.filter(id=job_id).only("id", "video_path").first()
    try:
        valid = job is not None and publication_media_token_is_valid(job.id, job.video_path, token)
    except RuntimeError:
        valid = False
    if not valid:
        raise Http404("Publication media not found.")
    path = Path(job.video_path)
    if not path.is_file():
        raise Http404("Publication media file not found.")
    response = FileResponse(path.open("rb"), content_type="video/mp4")
    response["Content-Length"] = str(path.stat().st_size)
    response["Cache-Control"] = "private, max-age=300"
    response["X-Content-Type-Options"] = "nosniff"
    return response
