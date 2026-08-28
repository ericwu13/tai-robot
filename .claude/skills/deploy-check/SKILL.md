---
name: deploy-check
description: Verify a tai-robot deployment is wired correctly (news/vote paths, Discord notifications, regime leg strategies) and explain how to redeploy or restart a bot. Use when a bot needs restarting, a deploy looks misconfigured, votes or news signals never reach the bot, or the user asks how to redeploy.
---

# Deploy Check

```bash
C:/Python313/python.exe -s scripts/monitor/check_deploy.py [--settings PATH]
```

Reports whether the wiring exists and where it points — **never the
secret values**. Secrets live only in `settings.yaml` (gitignored) and
must never be echoed, committed, or posted.

## What it verifies

| Setting | Missing means |
|---|---|
| `news.signal_path` | the circuit-breaker signal can never be read |
| `news.regime_vote_path` | **W2/W3/W4 votes expire UNREAD** — the historical "votes never consumed" bug |
| `news.events_path` | the macro-event gate fails open |
| `news.rss_state_file` | W3 liveness cannot be checked |
| `notifications.discord_bot_token` + `discord_channel_id` | bot Discord disabled — no trade or daily-report messages |
| `news.discord_webhook` | monitor notifications unavailable (P3) |

Per bot it also checks `session.json`: for `regime_mode: true` both
`long_strategy` and `short_strategy` must be non-empty and `active_leg`
must be one of `long` / `short` / `idle`.

## Manual redeploy (the only way)

**There is NO headless deploy** — the deploy dialog is a GUI modal.

1. start `dist\tai_backtest\tai_backtest.exe`
2. log in (Capital API credentials)
3. open the 部署 (deploy) tab
4. pick the saved session row for the bot — the dialog pre-fills
   strategy / trading mode / legs from that bot's `session.json`
5. deploy; the runner re-acquires `.lock` and resumes from `session.json`

## Gotchas

- **`mode_override.json` can only flip `trading_mode` on an ALREADY
  RUNNING bot.** It cannot start one, and it is not a deploy mechanism.
- **Empty `news.regime_vote_path` is the historical miswiring**: with `""`
  the runner's `_read_regime_vote` is inert, so every W2/W3/W4 vote file
  expires unread and the regime brain silently runs vote-free. It looks
  identical to "the bridges produced no votes" — check the setting first.
- Relative news paths resolve against the project root, not the EXE's
  cwd. Absolute paths are what n8n actually writes.
- `session.json` is the deploy dialog's pre-fill source; if it is corrupt
  the dialog cannot restore the bot and a resume would start flat.
- Never hardcode channel IDs, role IDs, tokens, or webhook URLs in source
  or in a report. Read them from `settings.yaml` at runtime only.
- Verify a packaged EXE with `dist/tai_backtest/BUILD_INFO.json` — a
  release EXE has silently diverged from its tag before.

## Next steps

- after redeploying → `bot-status` to confirm the lock and a fresh log line
- votes still not consumed → `bridge-health`
