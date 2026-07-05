from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from app.scheduled_generation import process_due_scheduled_generations, process_one_scheduled_generation


class Command(BaseCommand):
    help = "Process due scheduled generation queue items."

    def add_arguments(self, parser):
        parser.add_argument("--due", action="store_true", help="Process due pending items.")
        parser.add_argument("--once", action="store_true", help="Process once and exit. Present for botctl/cron symmetry.")
        parser.add_argument("--limit", type=int, default=0, help="Maximum queue items to process; 0 means all due.")
        parser.add_argument("--item-id", default="", help="Process one scheduled generation item by UUID.")

    def handle(self, *args, **options):
        close_old_connections()
        if options.get("item_id"):
            status = process_one_scheduled_generation(options["item_id"])
            self.stdout.write(f"{options['item_id']}: {status}")
            return
        if not options.get("due"):
            self.stdout.write("Nothing to do: pass --due or --item-id to process scheduled generation items.")
            return
        results = process_due_scheduled_generations(limit=max(0, int(options.get("limit") or 0)))
        if not results:
            self.stdout.write("No due scheduled generation items.")
            return
        for item_id, status in results:
            self.stdout.write(f"{item_id}: {status}")
