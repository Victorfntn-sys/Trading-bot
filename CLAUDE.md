# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository overview

A BTC/USD crypto trading bot for Kraken, deployed on Railway. It is a tiny, dependency-light
Python project — no package structure, no tests, no build step. Two standalone scripts, run one
at a time via Railway's "Custom Start Command":

- **`live_bot.py`** — the production bot. Runs forever (`main_loop`), polling Kraken every
  `POLL_SECONDS` (60s), managing two independent trading "pockets" (trend + scalp), and sending
  Telegram notifications for every trade/state change.
- **`backtest_on_railway.py`** — a one-shot script that fetches historical candles from Kraken,
  replays the same strategy logic, and sends the results (text summary + equity chart) to
  Telegram. Meant to be run temporarily by swapping Railway's start command, then swapped back.

Code comments and log/Telegram messages are written in **French** — match that convention when
editing existing strings or adding new ones in these files.

## Running / developing

There is no test suite, linter, or build step configured. Development is essentially: edit one
of the two scripts, run it locally with the right env vars, watch the logs and/or Telegram
output.

```bash
pip install -r requirements.txt   # ccxt, requests, matplotlib

# Paper trading (default, no real orders, no API keys needed beyond Telegram):
python live_bot.py

# Backtest (fetches BACKTEST_DAYS of 1m candles from Kraken and reports via Telegram):
BACKTEST_DAYS=14 python backtest_on_railway.py
```

Required environment variables (see the module docstring at the top of `live_bot.py`):

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Notifications; bot logs a warning and no-ops if unset |
| `TRADING_MODE` | `"paper"` (default) or `"live"` — see safety note below |
| `ANTHROPIC_API_KEY` | Powers news-sentiment scoring via Claude (`live_bot.py` only) |
| `KRAKEN_API_KEY` / `KRAKEN_API_SECRET` | Only required when `TRADING_MODE=live` |
| `STATE_FILE_PATH` / `TRADES_FILE_PATH` | Override defaults (`/data/state.json`, `/data/trades.json`) |
| `BACKTEST_DAYS` | `backtest_on_railway.py` only, default 14 |

On Railway, `live_bot.py` needs a **Volume mounted at `/data`** so `state.json` and `trades.json`
survive redeploys; without it the bot still runs but resets its position/history on every deploy.

### Trading mode safety

Default mode is **paper trading** (simulated, no real money/orders). The module docstring in
`live_bot.py` explicitly warns: never switch `TRADING_MODE=live` without having observed paper
trading for at least several weeks first. When `TRADING_MODE=live`, Kraken API keys must have
**trading-only permissions, never withdrawal**.

## Architecture

Both scripts implement the *same* strategy logic independently (there's no shared module) — when
changing strategy parameters or logic, check whether the change should be mirrored in both
`live_bot.py` and `backtest_on_railway.py` to keep backtests representative of live behavior.

### Two independent trading pockets

Capital is split into two pockets that trade independently against the same `BTC/USD` 1-minute
candles:

- **Trend pocket** (`1 - SCALP_ALLOCATION_PCT` of capital, default 70%): MA10/MA25 crossover.
  Long when `short_ma > long_ma`. Position size scales with trend strength between
  `MIN_POSITION_PCT` and `MAX_POSITION_PCT` (`compute_position_pct`). Exits are checked in
  priority order in `main_loop`: hard stop-loss (`TREND_STOP_LOSS_PCT`) → trailing stop
  (`check_trailing_stop`, activates after `TRAILING_STOP_ACTIVATION_PCT` profit) → tiered ROI
  target (`ROI_TABLE`, target decreases the longer the position is held) → MA cross-back signal.
- **Scalp pocket** (`SCALP_ALLOCATION_PCT`, default 30%): momentum breakout. Enters when price
  moves `SCALP_ENTRY_THRESHOLD_PCT` over `SCALP_MOMENTUM_WINDOW` minutes. Exits on take-profit,
  stop-loss, or `SCALP_MAX_HOLD_MINUTES` timeout (`check_scalp_exit`).

### Live bot risk controls (`live_bot.py` only, not modeled in backtest)

- **Daily loss limit** — `DAILY_LOSS_LIMIT_PCT`: if today's total equity drops below this vs.
  `day_start_equity`, `state.halted = True` and the bot stops trading until manual intervention.
- **Volatility filter** — `MIN_VOLATILITY_PCT` blocks trend buys in flat/quiet markets.
- **Trend-strength filter** — `MIN_TREND_STRENGTH_PCT` blocks trend buys on a near-zero MA cross
  (noise). Both filter thresholds were tuned from observed live data — see inline comments near
  their definitions for the reasoning/dates before changing them.
- **Stoploss guard** (`is_stoploss_guard_active`) — pauses new trend buys for
  `STOPLOSS_GUARD_PAUSE_MINUTES` after `STOPLOSS_GUARD_TRADE_LIMIT` stop-losses within
  `STOPLOSS_GUARD_LOOKBACK_MINUTES`.
- **Cooldown** (`is_in_cooldown`) — blocks new trend buys for `COOLDOWN_MINUTES` after any sell.
- **News sentiment filter** — `fetch_news_sentiment` pulls BTC headlines from CryptoCompare,
  scores them via a Claude API call (`analyze_sentiment_with_claude`, model
  `claude-haiku-4-5-20251001`), and blocks new buys (both pockets) when the score is at/below
  `NEWS_NEGATIVE_THRESHOLD`. Cached for `NEWS_REFRESH_SECONDS` (15 min). This filter is **not**
  available in the backtest (no historical news archive), so backtest results are optimistic
  relative to live behavior in that respect.

### State and persistence (`live_bot.py`)

`BotState` holds cash/coin balances and position info for both pockets, loaded from
`STATE_FILE_PATH` on startup and saved after every state-changing action via `state.save()`. All
closed trades (both pockets) are appended to `TRADES_FILE_PATH` via `log_trade` (list capped at
`MAX_TRADES_LOGGED`); this log is also read back by `is_stoploss_guard_active` and
`compute_trade_stats` (win rate / avg PnL, reported in the periodic Telegram status message every
`STATUS_EVERY_N_LOOPS` loops).

### Backtest structure (`backtest_on_railway.py`)

`fetch_history` paginates Kraken OHLCV via ccxt to build up to `BACKTEST_DAYS` of 1-minute
candles. `run_backtest` replays the trend+scalp logic candle-by-candle over that data (no live
risk controls, no news filter). `summarize_period` is run four times — full period, plus the
data split into thirds (oldest/middle/newest) — to sanity-check whether the strategy holds up
across different market phases rather than relying on one aggregate result. Results and an
equity-curve chart (`plot_full_period`, saved to `/tmp` since Railway's filesystem is ephemeral
outside `/data`) are sent to Telegram; nothing is written to disk persistently.
