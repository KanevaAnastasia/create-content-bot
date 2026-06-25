from __future__ import annotations

import os
import socket
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from pipeline_admin.admin import (
    _claim_next_final_build_batch,
    _run_final_build_batch,
)


class Command(BaseCommand):
    help = "Process queued final video build jobs."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process available jobs once, then exit.")
        parser.add_argument("--run-id", default="", help="Only process jobs for this generation run id.")
        parser.add_argument("--sleep", type=float, default=5.0, help="Seconds to sleep when the queue is empty.")
        parser.add_argument("--limit", type=int, default=0, help="Maximum jobs to process before exiting; 0 means unlimited.")
        parser.add_argument("--worker-id", default="", help="Stable worker id to write into job heartbeat payloads.")

    def handle(self, *args, **options):
        worker_id = str(options.get("worker_id") or "").strip()
        if not worker_id:
            worker_id = f"{socket.gethostname()}:{os.getpid()}"
        run_id = str(options.get("run_id") or "").strip() or None
        sleep_seconds = max(0.25, float(options.get("sleep") or 5.0))
        limit = max(0, int(options.get("limit") or 0))
        once = bool(options.get("once"))
        processed = 0

        self.stdout.write(f"final_build_worker started worker_id={worker_id}")
        while True:
            close_old_connections()
            batch = _claim_next_final_build_batch(worker_id, run_id=run_id)
            if not batch:
                if once:
                    break
                time.sleep(sleep_seconds)
                continue

            first = batch[0]
            variants = [str(item["variant"]) for item in batch]
            self.stdout.write(
                "processing final build batch "
                f"run={first['run_id']} language={first['language']} variants={','.join(variants)}"
            )
            _run_final_build_batch(
                str(first["run_id"]),
                str(first["language"]),
                variants,
                worker_id=worker_id,
            )
            processed += len(variants)
            if limit and processed >= limit:
                break

        self.stdout.write(f"final_build_worker stopped processed={processed}")
