# TikTok via Zernio (opt-in, free-only)

Other platforms keep their existing direct API connections. Existing TikTok
credentials are preserved. No job or connection switches automatically.

## Connect

1. Sign in at https://zernio.com/signup. Do not add a card, buy a plan or enable
   metered messaging/ads/phone services. Verify the actual dashboard terms:
   the current offer includes two connected accounts, full API and publishing.
   This is not a guarantee of permanently unchanged pricing.
2. Connect only the intended TikTok accounts, one for each project. Check the
   actual @handle, not just the display name. Do not reconnect a different
   identity over an existing account. All team connections count toward billing.
3. Create an API key with visibility of the team's connections (the free-only
   account-count guard must not see just a subset). Keep it out of git and chat.
4. Run with the existing Python environment:

   `.venv/bin/python scripts/connect_zernio_tiktok.py grandma_kitchen`

   Enter the key at the hidden prompt. The script lists TikTok IDs/handles only.
   Then pass `--account-id ID --username HANDLE --save` to verify the exact
   identity and atomically store project-prefixed credentials in `.env` (0600).
   Repeat for `discipline` with its own ID/handle. Existing direct tokens stay intact.
5. Reload the publication worker using `scripts/botctl.sh celery-worker-reload`
   and the admin server through its existing deployment mechanism so changed
   environment values are picked up. Do not restart an active upload blindly.
6. In the upload form load creator info, verify @handle, explicitly select
   privacy, preview the video/caption and confirm publication and music rights.
   No actual test post is created by the connection script.

Settings per project: `TIKTOK_PROVIDER=zernio`, `ZERNIO_API_KEY`,
`ZERNIO_TIKTOK_ACCOUNT_ID`, `ZERNIO_TIKTOK_USERNAME`, all prefixed with
`PUBLISH_<PROJECT>_`. No global credential fallback is used.

## Delivery and safety

- More than two visible connected accounts blocks sending. This guard cannot
  remove fees from other services or verify a legacy subscription; check billing.
- Local video/optional cover use presigned storage upload; the API key is never
  sent to storage. AI disclosure, interaction controls and chosen privacy are
  carried through. Captions over 2200 characters fail rather than being truncated.
- New Business-app TikTok connections support public video direct posts only.
  The adapter never silently changes privacy or downgrades to drafts.
- Each job pins its provider/account and saves its request before posting with
  `Idempotency-Key=<job UUID>`. A retry with a remote ID queries the existing post;
  only a confirmed failed post calls `/retry`. Unknown responses reuse the same
  key within 23 hours; after that manual reconciliation is required because the
  provider's deduplication window is 24 hours.
- Accepted/queued is not published. The existing minute-based Celery beat task
  polls pending jobs and resolves public links, without creating/retrying posts.
  Beat and the worker must be running. An interrupted job without a remote ID
  becomes manual-action-needed after 15 minutes; explicit retry reconciles it.
- A remote draft is never labelled publicly published. No automatic draft flow
  is enabled. The monitor stores only a validated TikTok permalink.

## Official references

- https://docs.zernio.com/pricing
- https://docs.zernio.com/platforms/tiktok/posts
- https://docs.zernio.com/guides/media-uploads
- https://docs.zernio.com/guides/idempotency
- https://docs.zernio.com/posts/retry-post

Integration tests use mocks only. Live connection and end-to-end publication
must be verified with the owner after signing in; passing tests is not proof
that an account is connected or that a video has gone live.
