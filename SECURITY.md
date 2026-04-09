# Security Notes

This project is an experimental trading research tool, not a production
service. Before exposing it beyond localhost, read this entire document.

## Threat model

The dashboard backend runs as a trusted local-network service. It assumes:
- Only the operator has access to the host.
- The host is not on the public internet.
- Secrets live in a gitignored `.env` file on the host.

If any of those assumptions breaks, you need additional protection.

## Sensitive surfaces

These endpoints can move money, send messages, or leak information:

| Endpoint                                 | Risk                                         |
|------------------------------------------|----------------------------------------------|
| `POST /api/campaigns/{id}/close`         | Closes all DCA layers at market              |
| `POST /api/campaigns/{id}/dca`           | Adds the next DCA layer at current price     |
| `POST /api/positions/{id}/close`         | Closes an individual position                |
| `POST /api/chat`                         | LLM tool loop with write-access to campaigns |
| `POST /api/smart-alerts`                 | Creates an alert the bot will evaluate       |
| `DELETE /api/smart-alerts/{id}`          | Removes user alerts                          |
| `POST /api/smart-alerts/evaluate`        | Triggers immediate evaluation                |
| `GET /api/logs`, `WS /ws/logs`           | Streams raw container stdout/stderr          |

The `POST /api/chat` endpoint is particularly dangerous: it runs an
agentic tool-use loop where an LLM decides whether to call
`close_campaign`, `add_dca_layer`, `open_new_campaign`, or any other
write tool based on natural-language input. Anyone who can send a chat
message can, in principle, instruct the bot to close your entire book.

## Authentication

The dashboard supports a single shared-secret API key. Set it in `.env`:

```
DASHBOARD_API_KEY=<long-random-string>
```

When set, every sensitive endpoint in the table above requires the
`X-API-Key` header to match. When empty, auth is a no-op (convenient for
local dev, NOT safe for anything else).

**If you expose the dashboard beyond localhost, you MUST set this.**

Read endpoints (`GET /api/account`, `/api/ohlcv`, etc.) remain open to
support dashboard polling from the browser without CORS complications.
If you want full lockdown, put the whole thing behind a reverse proxy
with basic auth or a WireGuard/Tailscale tunnel.

## SSRF protection

The `fetch_url` LLM tool is protected by a DNS-resolving guard that
rejects loopback, private, link-local, multicast, reserved ranges and
cloud metadata hostnames. Up to 3 redirects are followed, each
re-validated. See `services/dashboard/backend/plugin_web.py`.

## Docker socket

The dashboard reads container logs via `tecnativa/docker-socket-proxy`
in read-only mode (CONTAINERS/INFO/PING/VERSION only). The raw
`/var/run/docker.sock` is never mounted into any application container.

## Logging

Avoid logging API responses that might contain secrets at INFO level.
If you add a new collector or external call, prefer `logger.debug` for
full payloads and `logger.info` for summaries only. The dashboard's
`/api/logs` endpoint will relay whatever is logged — assume it's
publicly visible the moment you port-forward 8001.

## Telegram bot

The notifier service polls Telegram for messages and forwards them to
the dashboard `/api/chat`. It whitelists by `TELEGRAM_CHAT_ID` — only
the configured chat can trigger tool use. Anyone else messaging the bot
gets an "Unauthorized" reply. Still, do NOT share the bot username
publicly, and rotate the token if it ever leaks.

## Secrets rotation

`.env` is gitignored with a broad glob (`.env`, `.env.*`). If you
accidentally commit a secret, rotate it immediately and scrub history
with `git-filter-repo`. The `.env.example` file ships with empty
placeholders — never commit live values there.

## What to rotate if the repo leaks

1. Anthropic API key
2. xAI API key
3. Telegram bot token
4. Binance API key (even read-only)
5. Any other `_API_KEY` in `.env`
6. Postgres password in `docker-compose.yml` if you customised it

## Reporting a vulnerability

Open a GitHub issue tagged `security`. For issues that would put other
users at risk, email the maintainer directly instead of filing publicly.
