# Content Fabric Agent Rules

- Start, stop, restart, and inspect the Telegram bot only through `scripts/botctl.sh`.
- Do not run the bot directly with `.venv/bin/python main.py` unless the user explicitly asks for a foreground debug run.
- Do not start the bot with `launchctl`, `nohup`, PyCharm, or ad hoc shell commands from Codex.
- Before changing bot process state, run `scripts/botctl.sh status`.
- If `scripts/botctl.sh status` reports an unmanaged bot-like process, do not kill it unless the user explicitly asks. Adopt it with `scripts/botctl.sh adopt <pid>` when needed to continue normal bot management, unless the user explicitly says not to.
- After code changes that affect the running bot, restart it yourself through `scripts/botctl.sh restart` unless the user explicitly says not to restart.
- After changes to the admin panel (including models, forms, templates, configuration, or admin registration), restart the admin service yourself unless the user explicitly says not to restart. Restarting the Telegram bot does not restart the admin panel. Use the existing admin service manager and verify the affected admin page after restart.
- After each deployed change to the marketer, give the existing Codex chat “Ревьюер маркетинга Content Fabric” the live links to both blogs and the saved editions. Have it open the reports, check original sources and the filming decisions, and return its findings. Follow the review procedure in `docs/marketer.md`. This review is triggered by the update.
- Start or inspect final video build workers only through `scripts/botctl.sh final-worker <run_id>`, `scripts/botctl.sh final-worker-foreground <run_id>`, and `scripts/botctl.sh final-worker-status`.
- After every code change, review the complete task diff for correctness, regressions, security, and accidental unrelated edits; fix every issue found and run the relevant tests.
- When the reviewed change is complete, commit only the task-related files and push the current branch to `origin` immediately, without waiting for a separate request, unless the user explicitly says not to commit or push.

## Audience-facing writing, every project and language

- Apply `app/writing_style.py` to new scripts, voiceovers, captions, posts and replies. Review meaning and speakability, not only forbidden punctuation or stock phrases.
- Establish who speaks, the concrete situation, what the character wants, the action and its consequence. Reject unexplained fragments such as “Сначала путался” or “Я уже немного другой” when the surrounding speech does not explain what happened.
- Keep one coherent point of view and natural connections. Do not assemble generic aphorisms, forced jokes or repeated abstract lessons. Never invent historical facts or quotations to make prose vivid.
- Read scripts as continuous spoken speech and check them against the storyboard. Editorial acceptance must name concrete defects and fixes; a checklist or prior approval is not evidence that a script sounds natural.
- Preserve the user's requested ending, factual constraints and approved wording unless the current task explicitly requests editing. Refer to Wikipedia:Signs_of_AI_writing as editorial guidance, not an authorship detector or promise of virality.
