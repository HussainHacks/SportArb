# SportArb

A real time sports arbitrage scanner written in Python. SportArb polls live odds from several bookmakers, normalises them into one format, detects pricing discrepancies where every outcome of an event can be backed for a guaranteed margin, and sends the opportunity as a Telegram alert with stakes already calculated.

## How it works

1. Collectors poll bookmaker odds through The Odds API on a short interval.
2. A normalisation layer converts each bookmaker feed into shared data models.
3. A matching engine resolves the same event and market across different bookmakers, including team name variations.
4. The detection engine compares best available prices per selection, checks whether the combined implied probability is below one, and applies seven risk filters (minimum and maximum arb percentage, stale odds, and more).
5. Surviving opportunities are recorded by the paper trading module and pushed out as Telegram alerts. Redis caches odds and PostgreSQL persists results.

## Project layout

- `backend/collectors` — bookmaker feed polling
- `backend/normalization` — shared odds data models and feed conversion
- `backend/matcher` — cross bookmaker event and market matching
- `backend/engine` — arbitrage detection, stake calculation and risk filters
- `backend/paper_trading` — simulated trade recording and review
- `backend/alerts` — Telegram alert delivery
- `backend/storage` — Redis and PostgreSQL persistence
- `backend/tests` — test suite

## Running it

Copy `.env.example` to `.env` and add your own Odds API key and Telegram bot details, then start the stack with Docker Compose:

```
docker compose up
```

## Notes

Built as a personal research project into market pricing and arbitrage detection. Paper trading only; no real wagers are placed by this software.
