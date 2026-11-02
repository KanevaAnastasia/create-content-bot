# Content Fabric Agent Rules

- Start, stop, restart, and inspect the Telegram bot only through `scripts/botctl.sh`.
- Do not run the bot directly with `.venv/bin/python main.py` unless the user explicitly asks for a foreground debug run.
- Do not start the bot with `launchctl`, `nohup`, PyCharm, or ad hoc shell commands from Codex.
- Before changing bot process state, run `scripts/botctl.sh status`.
- If `scripts/botctl.sh status` reports an unmanaged bot-like process, do not kill it unless the user explicitly asks. Adopt it with `scripts/botctl.sh adopt <pid>` when needed to continue normal bot management, unless the user explicitly says not to.
- After code changes that affect the running bot, restart it yourself through `scripts/botctl.sh restart` unless the user explicitly says not to restart.
- Start or inspect final video build workers only through `scripts/botctl.sh final-worker <run_id>`, `scripts/botctl.sh final-worker-foreground <run_id>`, and `scripts/botctl.sh final-worker-status`.
