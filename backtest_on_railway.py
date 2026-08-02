"""
Backtest multi-périodes exécuté sur Railway — résultats envoyés sur Telegram
==============================================================================

Version alignée sur la config EXACTE du bot en direct (au 28/07) :
MA30/75, filtre de volatilité, seuil de force minimum, stop-loss, trailing
stop, ROI dégressif, cooldown, pause protectrice après stop-loss répétés.

COMMENT L'UTILISER :
1. Sur Railway, Settings du service -> Custom Start Command ->
   python backtest_on_railway.py
2. Sauvegarde, attends les résultats sur Telegram
3. Remets ensuite le Custom Start Command sur : python live_bot.py

Le filtre news n'est pas inclus (pas d'archive historique disponible).
"""

import os
import time
import logging
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import ccxt
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backtest")

# ============================================================
# CONFIGURATION — identique au bot en direct
# ============================================================
SYMBOL = "BTC/USD"
TIMEFRAME = "1m"
BACKTEST_DAYS = int(os.environ.get("BACKTEST_DAYS", "21"))

SHORT_WINDOW = 30
LONG_WINDOW = 75
VOLATILITY_WINDOW = 25
INITIAL_CAPITAL = 1000.0
FEE_RATE = 0.001

MAX_POSITION_PCT = 0.95
MIN_POSITION_PCT = 0.40
TREND_STRENGTH_CAP_PCT = 1.0

TREND_STOP_LOSS_PCT = 3.0
TRAILING_STOP_ACTIVATION_PCT = 1.0
TRAILING_STOP_PCT = 2.0

ROI_TABLE = [(0, 5.0), (60, 2.5), (180, 1.0)]

STOPLOSS_GUARD_LOOKBACK_MINUTES = 240
STOPLOSS_GUARD_TRADE_LIMIT = 2
STOPLOSS_GUARD_PAUSE_MINUTES = 120

COOLDOWN_MINUTES = 15
MIN_VOLATILITY_PCT = 0.15
MIN_TREND_STRENGTH_PCT = 0.05

SCALP_ALLOCATION_PCT = 0.30
SCALP_MOMENTUM_WINDOW = 5
SCALP_ENTRY_THRESHOLD_PCT = 1.5
SCALP_TAKE_PROFIT_PCT = 1.0
SCALP_STOP_LOSS_PCT = 0.5
SCALP_MAX_HOLD_MINUTES = 30

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def send_telegram_text(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram non configuré")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=15)
    except Exception as e:
        log.error("Échec envoi texte Telegram : %s", e)


def send_telegram_photo(filepath, caption=""):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        with open(filepath, "rb") as f:
            requests.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"photo": f},
                timeout=30,
            )
    except Exception as e:
        log.error("Échec envoi photo Telegram : %s", e)


def fetch_history(symbol, timeframe, days):
    exchange = ccxt.kraken({"enableRateLimit": True})
    ms_per_candle = exchange.parse_timeframe(timeframe) * 1000
    since = exchange.milliseconds() - days * 24 * 60 * 60 * 1000

    all_candles = []
    attempts = 0
    while attempts < 300:
        batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=720)
        attempts += 1
        if not batch:
            break
        all_candles += batch
        new_since = batch[-1][0] + ms_per_candle
        if new_since <= since:
            break
        since = new_since
        if since >= exchange.milliseconds():
            break

    return all_candles


def mean(values):
    return sum(values) / len(values)


def compute_position_pct(trend_strength_pct):
    ratio = min(1.0, max(0.0, trend_strength_pct / TREND_STRENGTH_CAP_PCT))
    return MIN_POSITION_PCT + (MAX_POSITION_PCT - MIN_POSITION_PCT) * ratio


def run_backtest(candles):
    closes = [c[4] for c in candles]
    timestamps = [c[0] for c in candles]  # en millisecondes
    n = len(closes)

    warmup = max(LONG_WINDOW, VOLATILITY_WINDOW)

    trend_cash = INITIAL_CAPITAL * (1 - SCALP_ALLOCATION_PCT)
    trend_coins = 0.0
    trend_position = False
    entry_price = None
    entry_ts = None
    trend_highest_price = None
    last_sell_ts = None
    recent_stoplosses = []  # timestamps (ms) des sorties stop_loss/trailing_stop
    guard_paused_until = None

    scalp_cash = INITIAL_CAPITAL * SCALP_ALLOCATION_PCT
    scalp_coins = 0.0
    scalp_position = False
    scalp_entry_price = None
    scalp_entry_ts = None

    equity_curve = []
    equity_timestamps = []
    trend_trades = 0
    trend_wins = 0
    scalp_trades = 0
    scalp_wins = 0

    for i in range(warmup, n):
        price = closes[i]
        ts = timestamps[i]

        short_ma = mean(closes[i - SHORT_WINDOW + 1:i + 1])
        long_ma = mean(closes[i - LONG_WINDOW + 1:i + 1])
        signal_long = short_ma > long_ma
        trend_strength_pct = (short_ma - long_ma) / long_ma * 100 if long_ma else 0

        vol_recent = closes[i - VOLATILITY_WINDOW + 1:i + 1]
        vol_avg = mean(vol_recent)
        volatility_pct = (max(vol_recent) - min(vol_recent)) / vol_avg * 100 if vol_avg else 0

        # --- Sorties tendance ---
        if trend_position and entry_price:
            change_pct = (price - entry_price) / entry_price * 100
            exit_reason = None

            if change_pct <= -TREND_STOP_LOSS_PCT:
                exit_reason = "stop_loss"
            else:
                if trend_highest_price is None or price > trend_highest_price:
                    trend_highest_price = price
                profit_from_high = (trend_highest_price - entry_price) / entry_price * 100
                if profit_from_high >= TRAILING_STOP_ACTIVATION_PCT:
                    trailing_price = trend_highest_price * (1 - TRAILING_STOP_PCT / 100)
                    if price <= trailing_price:
                        exit_reason = "trailing_stop"
                if not exit_reason:
                    held_minutes = (ts - entry_ts) / 60000
                    target = ROI_TABLE[0][1]
                    for minutes_threshold, t in ROI_TABLE:
                        if held_minutes >= minutes_threshold:
                            target = t
                    if change_pct >= target:
                        exit_reason = "roi"

            if not exit_reason and not signal_long:
                exit_reason = "signal"

            if exit_reason:
                proceeds = trend_coins * price
                fee = proceeds * FEE_RATE
                trend_cash += proceeds - fee
                if change_pct > 0:
                    trend_wins += 1
                if exit_reason in ("stop_loss", "trailing_stop"):
                    recent_stoplosses.append(ts)
                last_sell_ts = ts
                trend_position = False
                trend_coins = 0.0
                entry_price = None
                entry_ts = None
                trend_highest_price = None

        # --- Entrée tendance ---
        if signal_long and not trend_position:
            recent_stoplosses[:] = [
                t for t in recent_stoplosses if ts - t <= STOPLOSS_GUARD_LOOKBACK_MINUTES * 60000
            ]
            guard_active = (guard_paused_until and ts < guard_paused_until) or (
                len(recent_stoplosses) >= STOPLOSS_GUARD_TRADE_LIMIT
            )
            if guard_active and not (guard_paused_until and ts < guard_paused_until):
                guard_paused_until = ts + STOPLOSS_GUARD_PAUSE_MINUTES * 60000

            cooldown_active = last_sell_ts is not None and (ts - last_sell_ts) < COOLDOWN_MINUTES * 60000

            if (
                volatility_pct >= MIN_VOLATILITY_PCT
                and trend_strength_pct >= MIN_TREND_STRENGTH_PCT
                and not cooldown_active
                and not guard_active
            ):
                position_pct = compute_position_pct(trend_strength_pct)
                spend = trend_cash * position_pct
                fee = spend * FEE_RATE
                trend_coins = (spend - fee) / price
                trend_cash -= spend
                trend_position = True
                entry_price = price
                entry_ts = ts
                trend_highest_price = price
                trend_trades += 1

        # --- Scalp ---
        if scalp_position:
            change_pct = (price - scalp_entry_price) / scalp_entry_price * 100
            held_minutes = (ts - scalp_entry_ts) / 60000
            exit_now, win = False, False
            if change_pct >= SCALP_TAKE_PROFIT_PCT:
                exit_now, win = True, True
            elif change_pct <= -SCALP_STOP_LOSS_PCT:
                exit_now, win = True, False
            elif held_minutes >= SCALP_MAX_HOLD_MINUTES:
                exit_now, win = True, change_pct > 0

            if exit_now:
                if win:
                    scalp_wins += 1
                proceeds = scalp_coins * price
                fee = proceeds * FEE_RATE
                scalp_cash = proceeds - fee
                scalp_coins = 0.0
                scalp_position = False
                scalp_entry_price = None
                scalp_entry_ts = None
        else:
            if i >= SCALP_MOMENTUM_WINDOW:
                past_price = closes[i - SCALP_MOMENTUM_WINDOW]
                momentum = (price - past_price) / past_price * 100 if past_price else 0
                if momentum >= SCALP_ENTRY_THRESHOLD_PCT:
                    spend = scalp_cash
                    fee = spend * FEE_RATE
                    scalp_coins = (spend - fee) / price
                    scalp_cash = 0.0
                    scalp_position = True
                    scalp_entry_price = price
                    scalp_entry_ts = ts
                    scalp_trades += 1

        total_equity = (trend_cash + trend_coins * price) + (scalp_cash + scalp_coins * price)
        equity_curve.append(total_equity)
        equity_timestamps.append(ts)

    buy_hold_curve = [
        INITIAL_CAPITAL * (closes[i] / closes[warmup]) for i in range(warmup, n)
    ]

    return {
        "equity": equity_curve,
        "buy_hold": buy_hold_curve,
        "timestamps": equity_timestamps,
        "trend_trades": trend_trades,
        "trend_wins": trend_wins,
        "scalp_trades": scalp_trades,
        "scalp_wins": scalp_wins,
    }


def build_summary(result, candles):
    equity = result["equity"]
    buy_hold = result["buy_hold"]

    final_equity = equity[-1]
    final_buy_hold = buy_hold[-1]
    total_return_pct = (final_equity / INITIAL_CAPITAL - 1) * 100
    buy_hold_return_pct = (final_buy_hold / INITIAL_CAPITAL - 1) * 100

    running_max = equity[0]
    max_drawdown = 0.0
    for e in equity:
        running_max = max(running_max, e)
        max_drawdown = min(max_drawdown, (e - running_max) / running_max * 100)

    trend_trades = result["trend_trades"]
    trend_win_rate = (result["trend_wins"] / trend_trades * 100) if trend_trades > 0 else 0
    scalp_trades = result["scalp_trades"]
    scalp_win_rate = (result["scalp_wins"] / scalp_trades * 100) if scalp_trades > 0 else 0

    first_ts = candles[0][0] / 1000
    last_ts = candles[-1][0] / 1000
    actual_days = (last_ts - first_ts) / 86400

    verdict = "✅" if total_return_pct > buy_hold_return_pct else "❌"

    return (
        f"📊 Backtest — config actuelle (MA{SHORT_WINDOW}/{LONG_WINDOW})\n\n"
        f"Période couverte : {actual_days:.1f} jours ({len(candles)} bougies 1min)\n\n"
        f"Capital initial : {INITIAL_CAPITAL:.0f} USD\n"
        f"Capital final : {final_equity:.2f} USD ({total_return_pct:+.2f}%)\n"
        f"Buy & hold équivalent : {final_buy_hold:.2f} USD ({buy_hold_return_pct:+.2f}%) {verdict}\n"
        f"Max drawdown : {max_drawdown:.2f}%\n\n"
        f"Trades tendance : {trend_trades} ({trend_win_rate:.0f}% réussite)\n"
        f"Trades scalp : {scalp_trades} ({scalp_win_rate:.0f}% réussite)\n\n"
        f"⚠️ Filtre news exclu. Résultat passé ≠ garantie future."
    )


def plot_full_period(result):
    plt.figure(figsize=(10, 5))
    dates = [t / 1000 for t in result["timestamps"]]
    plt.plot(dates, result["equity"], label="Stratégie (config actuelle)")
    plt.plot(dates, result["buy_hold"], label="Buy & Hold", linestyle="--")
    plt.title(f"Backtest {SYMBOL} — MA{SHORT_WINDOW}/{LONG_WINDOW} + protections")
    plt.ylabel("Portefeuille (USD)")
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    path = "/tmp/backtest_result.png"
    plt.savefig(path)
    return path


if __name__ == "__main__":
    log.info("Démarrage du backtest (%d jours demandés)", BACKTEST_DAYS)
    send_telegram_text(
        f"🔬 Backtest lancé sur Railway ({BACKTEST_DAYS} jours demandés)\n"
        f"Config : MA{SHORT_WINDOW}/{LONG_WINDOW}, stop-loss -{TREND_STOP_LOSS_PCT}%, "
        f"trailing, ROI dégressif, cooldown {COOLDOWN_MINUTES}min..."
    )

    try:
        candles = fetch_history(SYMBOL, TIMEFRAME, BACKTEST_DAYS)
        log.info("%d bougies récupérées", len(candles))

        warmup = max(LONG_WINDOW, VOLATILITY_WINDOW)
        if len(candles) < warmup + 20:
            send_telegram_text(
                f"⚠️ Pas assez de données historiques disponibles ({len(candles)} bougies, "
                f"il en faut au moins {warmup + 20}). Kraken limite la profondeur d'historique en 1 minute."
            )
        else:
            result = run_backtest(candles)
            summary = build_summary(result, candles)
            send_telegram_text(summary)

            chart_path = plot_full_period(result)
            send_telegram_photo(chart_path, caption="Courbe du portefeuille (stratégie vs buy & hold)")

            log.info("Backtest terminé et envoyé sur Telegram")

    except Exception as e:
        log.error("Erreur pendant le backtest : %s", e)
        send_telegram_text(f"⚠️ Erreur pendant le backtest : {e}")

    log.info("Script terminé. Remets le Custom Start Command sur 'python live_bot.py'.")
