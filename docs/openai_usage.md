# OpenAI requests and spending

Text requests keep the complete `app/writing_style.py` policy in a separate developer message. GPT-6 and GPT-5.6 use an explicit cache boundary after the policy, with 30-minute retention. Changing task input is not written to that cache. Output verbosity is low; required content, approved wording and JSON fields must remain complete. Only `short` tasks use low reasoning and an 8,192-token output ceiling. Incomplete answers are rejected rather than accepted as truncated results. Large scene plans retain their output capacity.

The V5 policy consolidates repeated guidance; saved V1–V4 prefixes upgrade without rewriting the task or approved text. Semantic editorial rules still reach the model: appending them after generation cannot fix meaning.

Validation on 2026-11-26: the policy shrank from 7,964 to 4,088 characters. Two accepted live Luna speech edits (RU/EN) reused a 1,274-token cached prefix, compared with the previous envelope's 2,298-token prefix. Their prompt-plus-policy character counts fell 46–49%; visible JSON was 76–79% shorter than serializing the same result with the full video prompt. This is a small example comparison, not a measured reduction of the total bill. One earlier probe was rejected by local validation; the accepted probes used 145/206 reasoning tokens in addition to 42/31 visible output tokens. Format compliance does not guarantee semantic quality or first-attempt success.

Speech shortening sends only the spoken line, language, character budget and applicable constraints. OpenAI uses a strict one-field JSON schema (`text`) and a 2,048-token ceiling including reasoning. Gemini retains prompt-based JSON with local validation. Code replaces the exact speech after a single `CHARACTERS SPEAKS:` marker and preserves every other byte of the video prompt, including camera and voice rules. Ambiguous/mismatched markers fail before a paid call. Empty, overlong, incomplete English or missing required closing text is rejected, not truncated. No global scene-output cap is introduced. This optimization does not yet replace full scene rewrites with field-level edits.

Scene-plan and production context excludes known transport/accounting fields (base64 media, generated-video result/candidate maps, local paths, delivery IDs and cost records) via `app/llm_payload.py`. Unknown creative fields, language variants, references, timing and speech remain unchanged. Payload JSON has no indentation. This does not alter stored scene state or guest-facing menu text.

## Local ledger

`runtime/openai_usage.sqlite3` records Responses and Images API usage received by this installation after deployment, including incomplete/refused text and images which fail local decoding. Costs are estimates from provider token counts and Standard API rates checked 2026-11-26; cached reads/writes are separate and reasoning tokens are already included in output tokens. Unknown models or absent usage have unknown cost, never zero. Other applications using the key and provider-billed requests whose responses never arrive cannot be fully accounted for here.

The ledger is shared between local bot/worker processes. Provider response/request IDs prevent double counting. Prompt/response content and credentials are not saved; only a prompt hash/length, operation name, profile, status and usage are stored. Accounting failures are logged and never cause another paid generation. `OPENAI_USAGE_DB` overrides the path (tests always use a temporary file).

## Owner statistics and alert

The private `/stat` and `/stats` response to the existing primary owner includes today's and this month's locally tracked cost, monthly text/image breakdown, unknown-cost count and tracking start time. Older spending is not reconstructed from incomplete logs.

A background task checks the ledger every 60 seconds. At **$1.50 or more per Moscow calendar day**, it sends a single warning to `bot_commands.owner_chat_id()`, the active admin with the established owner username. It never uses the requesting user, guest, group or arbitrary admin. Generation continues. The alert state survives restarts; SQLite serializes concurrent claims.

An explicit Telegram rejection allows a later retry. If delivery times out, its state is `delivery_unknown`, because Telegram may already have accepted it; it is not resent automatically. A process crash while `sending` also requires manual reconciliation to avoid duplicate messages.

## Balance

A verified Billing snapshot can be set with `openai_usage.set_balance_anchor(balance_usd=..., observed_at=<aware datetime>, expires_on='YYYY-MM-DD')`. This writes local accounting metadata only. Never infer a balance from a monthly budget.

`/stat` labels the remaining amount **calculated**, shows the snapshot time and subtracts tracked spending after the snapshot. Top-ups, other apps, delayed billing and unseen responses can cause differences from Billing. Unknown-priced events after the snapshot suppress the estimate. On the stated credit-expiry date it stops showing an estimate until a fresh snapshot is entered. No billing credentials or automated browser session are stored.

Sources:
- https://developers.openai.com/api/docs/pricing
- https://developers.openai.com/api/docs/guides/prompt-caching
- https://developers.openai.com/api/docs/guides/reasoning
- https://developers.openai.com/api/docs/guides/structured-outputs
