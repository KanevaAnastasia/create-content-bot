.PHONY: test test-cov run final-worker final-worker-once start stop restart status logs

test:
	.venv/bin/python -m pytest

test-cov:
	.venv/bin/python -m pytest

run:
	scripts/botctl.sh start

final-worker:
	.venv/bin/python admin_panel/manage.py final_build_worker

final-worker-once:
	.venv/bin/python admin_panel/manage.py final_build_worker --once

start:
	scripts/botctl.sh start

stop:
	scripts/botctl.sh stop

restart:
	scripts/botctl.sh restart

status:
	scripts/botctl.sh status

logs:
	scripts/botctl.sh logs
