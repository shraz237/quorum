# Brent Crude Oil Trading Bot

An automated trading analysis bot for Brent crude oil, combining technical analysis, macro data, sentiment analysis, and shipping metrics.

## Getting Started

Copy the example environment file and fill in your API keys:

```bash
cp .env.example .env
```

Start all infrastructure services:

```bash
docker compose up
```

## Architecture

- **shared/** — shared Python package with DB models, schemas, and Redis helpers
- **docs/** — specifications and implementation plans
