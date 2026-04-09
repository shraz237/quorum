# WTI Crude Oil Trading Research Bot

> ⚠️ **Research tool, not financial advice.** Leveraged trading can lose you more than your deposit. Read [LICENSE](LICENSE) and [SECURITY.md](SECURITY.md) before running anything that touches real money.

A self-hosted, AI-assisted analytics stack for discretionary WTI crude oil trading. Pulls real-time market data, positioning, microstructure, macro, news, sentiment, and user performance history into a single dashboard; layers a 6-agent adversarial committee on top for decision support; writes trades to an internal "paper book" that the user can mirror manually on a broker of their choice.

Built on Docker Compose microservices with Postgres + TimescaleDB + Redis Streams. Python backend, React frontend, Claude + Grok for LLM reasoning, Binance USD-M futures (`CLUSDT` TRADIFI perpetual) as the primary price feed.

---

## What the dashboard actually shows

The frontend is a single-page React app with the following sections, top to bottom:

**Header** — Live tick-by-tick price ticker via direct browser → Binance WebSocket. Colour-coded direction of last change, 24h range, tick counter.

**Decision Support** — three synthesis widgets that turn the firehose of raw numbers into something a human can read in 5 seconds:
- **Now Brief** — AI-generated structured brief (Claude Haiku, 45s cache) reading the entire dashboard state and emitting `{headline, market_state, your_position, next_action, watch_for, risk_level, risk_reason}`.
- **Signal Confluence** — pure-logic matrix classifying every current signal as BULL / BEAR / NEUTRAL, with per-signal reason and a dominant-side balance bar.
- **Anomaly Radar** — threshold-based detector for rare/extreme conditions (funding blowout, OI spike, liquidation cluster, retail-vs-smart divergence, range breaks, etc.) with severity 1–10 and a 24h history log.

**Account State** — 7 cards covering equity, wallet balance, margin used, margin level %, free margin, realized PnL all-time, open campaigns count, and equity Δ / drawdown with a bar to the −50% hard stop.

**Risk & Scenario Tools** — 4 widgets:
- **Scenario Calculator** — table of PnL / equity / margin level / status at 11 price offsets (±5%) plus computed key levels: breakeven, stop-out price, −50% hard-stop price, and distance to each.
- **Monte Carlo** — 2000-path GBM simulation over 24h using rolling 1h log-return σ from 7d history. Outputs P(margin call) and P(−50% hard stop) probabilities plus P5/P50/P95 equity percentiles.
- **VWAP** — session (24h) and weekly (7d) VWAP with distance in %.
- **Events Calendar** — EIA weekly reports, FOMC, OPEC MOMR, IEA reports for the next 14 days with live countdown.

**Cross-Asset Context & Flow** — 2 widgets:
- **Cross-Asset Correlations** — DXY, SPX, Gold, BTC, VIX with 1h/24h change and rolling 24h Pearson correlation vs CLUSDT. Shows when oil decouples from broader risk.
- **Cumulative Volume Delta** — CVD computed from 1-min klines' taker buy volume; price/CVD divergence detection (bullish/bearish hidden flow).

**Learning & Feedback Loop** — 3 widgets:
- **Trade Journal** — automatically captures a full dashboard-state snapshot at every campaign open and close. Running stats: win rate, total PnL, profit factor, avg win / loss, Sharpe-like ratio, by-close-reason breakdown. Scrollable entry list.
- **Historical Pattern Match** — weighted Euclidean similarity search over a background-captured feature vector (scores, funding, positioning, taker flow). Returns top-10 most similar past moments with their actual forward returns at 1h / 4h / 24h horizons and an aggregate win-rate distribution.
- **Smart Alerts** — confluence-based alert builder. JSON expression tree with AND/OR over 18 metrics and six operators. Live match indicator. Fires to the existing Telegram notifier pipeline.

**Conviction Meter** — composite 0..100 gauge combining unified score, technical, 60-min momentum, funding extreme (contrarian-signed), retail-vs-smart delta, alerts, breaking news. Shows BULL/BEAR/MIXED direction and top 5 drivers with individual contributions.

**Binance Derivatives Metrics** — 4 cards: funding rate gauge + 7d sparkline, open interest + 24h change, long/short ratios (top vs retail vs taker flow), liquidations 24h USD totals + live event feed.

**Binance Pro (Market Microstructure)** — 3 cards: order book heatmap with bid/ask walls + imbalance %, whale trades feed (≥$10k aggregated), volume profile with POC + value area (70% volume range).

**Analysis Scores** — 5 full-width bipolar cards: Technical, Fundamental, Sentiment, Shipping, Unified. Each on a −100..+100 axis with 7-level sentiment band label.

**Price Chart** — TradingView Lightweight Charts candlestick view of Binance CLUSDT across 1m / 5m / 15m / 1H / 1D / 1W with per-layer campaign markers (`C#<id> L<n> SHORT/LONG`) for entry/SL/TP lines.

**Open Campaigns** — expandable DCA campaign cards with per-layer table, layer margin stop bar, and a **Next DCA Layer Preview** table — simulated outcomes (new avg, new total lots, new breakeven) at 5 trigger price levels.

Plus: **Positions**, **Signal History**, **@marketfeed digest panel**, **Chat panel** with full LLM tool access, **Logs panel** streaming container stdout over WebSocket.

---

## Architecture

```
┌──────────────┐  PriceEvents   ┌──────────┐  ScoresEvents  ┌──────────┐
│ data-collector│──────────────▶│ analyzer │──────────────▶│ ai-brain │
│ (Binance WS,  │  liquidations │ (scoring │                │ (Opus +  │
│  cross-asset, │  funding, OI, │  engine) │                │ committee│
│  macro)       │  L/S ratios   └────┬─────┘                │  Haiku,  │
└──────┬────────┘                    │                      │  Grok)   │
       │                             │                      └────┬─────┘
       ▼  Redis Streams ─────────────┴───────────────────────────┤
   ┌───┴────────────────────────────────────────────────────┐    │
   │  postgres + timescaledb                                │    │
   │  ohlcv, scores, signals, campaigns, positions,         │    │
   │  binance_metrics, signal_snapshots, anomalies, …       │    │
   └───▲────────────────────────────────────────────────────┘    │
       │                                                          │
   ┌───┴──────────┐           ┌─────────┐         ┌──────────┐   │
   │  sentiment   │           │ notifier│◀───────▶│ Telegram │   │
   │  (RSS,       │           │ (bot +  │         └──────────┘   │
   │   @marketfeed│           │ outbound)│                        │
   │   Twitter)   │           └────▲────┘                         │
   └──────┬───────┘                │                              │
          │                        │                              │
          ▼                        │                              │
   ┌──────┴────────────────────────┴──────┐                       │
   │            dashboard                 │◀─────────────────────┘
   │  FastAPI backend + plugin system     │
   │  (40 REST routes, 2 WS, 18 plugins)  │
   │                  +                   │
   │  React frontend (Vite, Tailwind,     │
   │  Lightweight Charts, 30+ components) │
   └──────────────────────────────────────┘
```

### Services

| Service | Responsibility |
|---|---|
| **data-collector** | Pulls Binance klines (REST + WebSocket), liquidations stream, derivatives metrics (funding, OI, L/S ratios, taker flow), cross-asset bars (Yahoo: DXY/SPX/Gold/BTC/VIX), and macro series (EIA, FRED, COT, JODI). Writes OHLCV + metrics tables. Publishes `PriceEvent` to Redis. |
| **analyzer** | Subscribes to `PriceEvent`. Computes multi-timeframe technical score (RSI/MACD/MA/ADX), fundamental score (rolling z-scores of inventories/COT/JODI), sentiment score (news + Twitter + @marketfeed knowledge), shipping score, unified composite. Publishes `ScoresEvent`. Also runs the alerts evaluator loop (price alerts, keyword alerts, score crosses, confluence smart alerts). |
| **ai-brain** | Opus strategist: reads the full state, emits structured `AIRecommendation` with entry/SL/TP. Haiku: per-minute @marketfeed digest classification + scoring. Grok: Twitter/X sentiment agent. Live watch worker: edits a single Telegram message in place with real-time price/score/verdict updates. Breaking-news watcher: triggers immediate reassessment on @marketfeed shocks. |
| **sentiment** | RSS news scraper (multi-source), Grok-powered Twitter narrative fetcher, @marketfeed Telegram channel scraper + Haiku classifier + 5-min digest builder. Writes sentiment + knowledge tables. |
| **dashboard** | FastAPI backend exposing ~40 REST routes and 2 WebSocket endpoints. Plugin architecture for LLM tools: each `plugin_*.py` file registers `PLUGIN_TOOLS` + `execute()` and is auto-loaded by the chat service. React/Vite/Tailwind frontend with 30+ components. Embedded chat with full tool access. |
| **notifier** | Telegram outbound: formats `alert.triggered`, `position.event`, `signal.recommendation`, `knowledge.summary`, `live_watch.update` Redis streams into Markdown messages. Inbound: long-polls Telegram for user messages, forwards to `/api/chat`, streams SSE progress back into a single edited message (with tool-call indicators). |
| **postgres** | TimescaleDB extension. Hypertables for all time-series data. Compression policies for chunks older than 30 days. |
| **redis** | Consumer-group-based streaming bus for inter-service events. |
| **docker-proxy** | `tecnativa/docker-socket-proxy` in read-only mode; lets the dashboard query container status + stream logs without exposing the full Docker socket. |

---

## Features & tools catalogue

### Price & market data (Binance CLUSDT)

- Klines REST + WebSocket upserts for 1m / 5m / 15m / 1h / 4h / 1d / 1w
- Live `@aggTrade` stream for the header ticker (direct browser → Binance, no proxy)
- Orderbook snapshot with imbalance computation
- `@forceOrder` (liquidations) WebSocket with event persistence
- Funding rate history, open interest history, long/short ratios (top traders, global retail, taker buy/sell)
- Cumulative Volume Delta computed from 1m kline `takerBuyBaseAssetVolume`
- Volume profile (POC + value area) computed from 5m klines
- Cross-asset context: DXY (DX-Y.NYB), SPX (^SPX), Gold (GC=F), BTC (BTC-USD), VIX (^VIX) with rolling correlation

### Macro data

- **EIA Open Data** — weekly crude/gasoline/distillate inventories
- **FRED** — DXY, Fed Funds Rate, CPI, 10Y–2Y spread, …
- **CFTC COT** — managed money and producer positioning
- **JODI Oil World** — global supply/demand

### Sentiment & news

- RSS multi-source scraper (oilprice.com, Reuters energy, …)
- **Grok** agent for real-time Twitter/X narrative summarisation
- **@marketfeed** Telegram channel scraper with **Claude Haiku** classification + 5-minute digest builder
- Breaking-news watcher triggers immediate AI reassessment

### AI reasoning

- **Opus strategist** — emits structured recommendation via Anthropic tools API, supports manage-existing-position decisions
- **6-agent Committee** — adversarial debate with three bull specialists (geopolitics, technical, macro), three bear specialists, and an Opus judge. Agents receive a **full 22-source dashboard context** (scores, conviction, anomalies, all Binance metrics, orderbook, whales, CVD, cross-assets, Monte Carlo, trade journal, pattern match, smart alerts, volume profile). Judge computes deterministic R:R from returned trade levels, flags same-domain neutralization, handles failed agents.
- **Claude Haiku Now Brief** — 45-second-cached synthesis brief reading the entire dashboard and emitting structured JSON.
- **Anomaly Radar** — pure-logic detector for rare conditions with severity 1–10 and history log.
- **Conviction Meter** — composite 0–100 gauge over 7 weighted inputs.
- **Historical Pattern Matching** — weighted Euclidean distance in feature space over accumulated signal snapshots, returns forward-return distribution of the closest N moments.
- **Signal Performance Tracker** — per-feature bucket stats showing whether signals actually predict forward returns.

### Trading logic

- **DCA campaigns** — layered margin `[3k, 6k, 10k, 20k, 30k, 30k]` USD on a $100k starting balance at x10 leverage (defaults in `shared/sizing.py`)
- **Hard stop** — −50% account equity drawdown force-closes all campaigns
- **Scenario calculator** — PnL/margin/status at 11 price offsets + key levels
- **Monte Carlo** — GBM margin-call probability simulator
- **Next DCA preview** — simulated outcomes if you add a layer at various offsets
- **Trade journal** — auto-captured entry + exit snapshots
- **Smart alerts** — confluence-based trigger trees

### Dashboard UX

- 30+ React components organised into logical rows
- Chat panel with streaming tool-call visualisation
- Logs panel streaming container stdout over WebSocket
- Marketfeed panel with sentiment colouring
- Signal detail drawer with full AI reasoning trace

### Telegram bot

- Every AI recommendation formatted as a Markdown alert
- `/state`, `/positions` shortcuts
- Free-text messages forwarded to `/api/chat` with SSE progress
- Live watch sessions pin and update a single message per session

---

## Getting started

### Prerequisites

- Docker + Docker Compose
- An Anthropic API key (required)
- Optional: xAI API key (Grok), Telegram bot token + chat ID, EIA / FRED keys

### Setup

```bash
git clone https://github.com/devnerdly/WTI-Oil-Trading-Bot-Telegram-.git
cd WTI-Oil-Trading-Bot-Telegram-

cp .env.example .env
$EDITOR .env       # fill in ANTHROPIC_API_KEY at minimum

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
curl -s 'localhost:8001/api/ohlcv?timeframe=5min&limit=5' | jq
```

### Enabling authentication (REQUIRED if not localhost-only)

Set a long random value in `.env`:

```bash
DASHBOARD_API_KEY=$(openssl rand -hex 32)
```

Restart the dashboard container. All mutating endpoints, `/api/chat`, and `/api/logs` will now require the `X-API-Key: <value>` header. Read [SECURITY.md](SECURITY.md) for the full threat model and sensitive-endpoint list.

---

## Tech stack

- **Python 3.12** — all backend services
- **FastAPI + Uvicorn** — dashboard HTTP + WebSocket
- **SQLAlchemy 2.x + psycopg v3** — DB layer
- **PostgreSQL 16 + TimescaleDB** — hypertables, compression, continuous aggregates
- **Redis 7** — streaming bus with consumer groups
- **APScheduler** — job scheduling in data-collector
- **websocket-client** — Binance WebSocket streams
- **pandas + pandas-ta** — technical indicators
- **numpy** — Monte Carlo simulation
- **anthropic** — Claude SDK (Opus, Sonnet, Haiku)
- **openai** — Grok (via x.ai OpenAI-compatible endpoint)
- **python-telegram-bot** — bot framework
- **BeautifulSoup4 + httpx** — web scraping (SSRF-guarded `fetch_url`)
- **React 18 + Vite + TypeScript** — frontend
- **Tailwind CSS** — styling
- **TradingView Lightweight Charts** — candlestick chart

---

## Project layout

```
trading/
├── docker-compose.yml               # full stack orchestration
├── LICENSE                          # MIT + trading disclaimer
├── SECURITY.md                      # threat model, auth, sensitive endpoints
├── .env.example                     # all env vars, non-secret
├── shared/                          # shared Python package
│   ├── models/                      # SQLAlchemy models (18 tables)
│   ├── schemas/                     # Pydantic events for Redis streams
│   ├── config.py                    # settings via pydantic-settings
│   ├── redis_streams.py             # publish/subscribe helpers
│   ├── account_manager.py           # account state recompute
│   ├── position_manager.py          # DCA campaign logic
│   ├── sizing.py                    # DCA layer margins + lot sizing
│   └── db_init.py                   # TimescaleDB hypertable setup
├── services/
│   ├── data-collector/              # Binance + macro + cross-asset collectors
│   ├── analyzer/                    # scoring engine + indicators
│   ├── ai-brain/                    # Opus/Haiku/Grok agents
│   ├── sentiment/                   # RSS/Twitter/@marketfeed scrapers
│   ├── notifier/                    # Telegram in/out
│   └── dashboard/
│       ├── backend/                 # FastAPI + 18 plugin_*.py files
│       └── frontend/                # React + Vite + Tailwind
└── tests/                           # pytest suites (per-service)
```

---

## Extending

### Add a new data source

1. Drop a collector under `services/data-collector/collectors/<name>.py`
2. Register a job in `services/data-collector/main.py`
3. Expose via an endpoint in `services/dashboard/backend/main.py`
4. Add a React card under `services/dashboard/frontend/src/components/`

### Add a new LLM tool

1. Create `services/dashboard/backend/plugin_<name>.py` with `PLUGIN_TOOLS: list[dict]` and `def execute(name, input) -> dict`
2. Add the import to `_PLUGINS` in `chat.py`
3. The tool is immediately available to the dashboard chat and Telegram bot

### Add a new committee specialist

Edit `plugin_committee.py`:
1. Write a new system prompt constant
2. Append to `_BULL_TEAM` or `_BEAR_TEAM`
3. The judge schema auto-adjusts

---

## Known limitations

- **No database migrations.** Schema changes require `docker compose down -v` or manual `ALTER TABLE`. Alembic is on the roadmap.
- **Dashboard API auth is opt-in.** `DASHBOARD_API_KEY` defaults to empty for local dev; you MUST set it outside localhost.
- **Tests cover only parts of the stack.** Dashboard plugins and shared trading logic are thin on coverage.
- **Not a trade executor.** The bot manages an **internal paper book**. Real order execution would require adding a broker adapter.
- **Data cost.** Running the committee every 30 minutes costs about $0.20–$0.40/day in Anthropic API fees. Now Brief adds ~$0.10/day. Haiku @marketfeed classification is nearly free.

---

## Disclaimer

This is an **experimental research tool**. Running it against a live broker account is at your own risk. The authors accept no liability for any losses. Read the full disclaimer in [LICENSE](LICENSE).

Leveraged derivatives (CFDs, futures, perpetuals) can lose you more than your deposit. The software does not predict price. Signals are hypotheses, not guarantees. Trade with money you can afford to lose.

## License

MIT — see [LICENSE](LICENSE).
