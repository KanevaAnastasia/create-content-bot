# Content Fabric Agent Rules

- Start, stop, restart, and inspect the Telegram bot only through `scripts/botctl.sh`.
- Do not run the bot directly with `.venv/bin/python main.py` unless the user explicitly asks for a foreground debug run.
- Do not start the bot with `launchctl`, `nohup`, PyCharm, or ad hoc shell commands from Codex.
- Before changing bot process state, run `scripts/botctl.sh status`.
- If `scripts/botctl.sh status` reports an unmanaged bot-like process, do not kill it unless the user explicitly asks. Ask whether to adopt it with `scripts/botctl.sh adopt <pid>` or let the user stop it from PyCharm.
- After code changes that affect the running bot, use `scripts/botctl.sh restart` only when the user approves the restart.
- Start or inspect final video build workers only through `scripts/botctl.sh final-worker <run_id>`, `scripts/botctl.sh final-worker-foreground <run_id>`, and `scripts/botctl.sh final-worker-status`.
