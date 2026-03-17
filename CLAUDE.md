# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PolyBOt is a Python algorithmic trading system for Polymarket prediction markets, focused on BTC 15-minute up/down binary markets. It implements multiple strategies: arbitrage scanning, pre-order limit trading, and maker-based directional trading with real-time Binance price feeds.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run tests (6 unit tests for scanner logic)
python test_scanner.py

# Arbitrage scanner
python main.py                          # one-shot scan
python main.py --loop                   # continuous loop
python main.py --loop --tg              # with Telegram notifications

# Price observer (logs to data/ CSVs)
python observer.py

# Signal bot (price alerts)
python signaler.py --threshold 0.45 --pair-threshold 0.93

# Pre-order trader
python trader.py                        # dry-run
python trader.py --live --shares 5 --price-limit 0.45   # live (5s confirmation)

# Maker bot
python maker_bot.py                     # dry-run
python maker_bot.py --live --shares 5 --entry-time 30   # live
```

No linter or formatter is configured.

## Architecture

**Strategies** (each runnable independently):
- `main.py` + `scanner.py` — Arbitrage detection (intra-market: YES+NO < $1; intra-event: sum of YES > $1)
- `trader.py` — Pre-order strategy: places GTC limit buys on both YES and NO sides 2 min before window start, monitors fills, sells on danger signals
- `maker_bot.py` — Maker strategy: watches Binance BTC/USDT WebSocket, places single-sided maker order 30s before window close based on price delta
- `signaler.py` — Alert bot: monitors prices, sends Telegram alerts on dips below thresholds

**Shared modules**:
- `observer.py` — `MarketFinder` (locates active BTC 15-min markets via slug or fallback search) and `PriceLogger` (CSV logging)
- `notifier.py` — Telegram notification wrapper
- `config.py` — API URLs (`GAMMA_API`, `CLOB_API`), thresholds, trading parameters

**External APIs**:
- Polymarket Gamma API (`gamma-api.polymarket.com`) — event/market metadata
- Polymarket CLOB API (`clob.polymarket.com`) — order placement, book, midpoint, time
- Binance WebSocket — real-time BTC/USDT prices (maker_bot only)
- `py_clob_client` — Polymarket SDK for order signing/submission

## Key Patterns

- **Window timing**: All strategies use 15-minute windows (900s). Start = `floor(now/900)*900`, next = `ceil(now/900)*900`
- **MarketFinder flow**: server `/time` → slug generation from timestamp → `/markets` fallback → midpoint validation
- **Order management**: GTC limit orders only, fill polling every 0.5-2s, dry-run simulates fills when midpoint crosses limit price
- **All strategies support `--live` flag** — without it, orders are simulated (dry-run mode)
- **Comments and docs are in Russian**
- **Environment variables** (`.env`): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `POLY_PRIVATE_KEY`, `POLY_FUNDER_ADDRESS`
