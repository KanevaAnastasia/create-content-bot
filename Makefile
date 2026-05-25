.PHONY: test test-cov run start stop restart status logs

test:
	.venv/bin/python -m pytest

test-cov:
	.venv/bin/python -m pytest

run:
	.venv/bin/python main.py

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
