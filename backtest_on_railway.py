"""
Backtest multi-périodes exécuté sur Railway — résultats envoyés sur Telegram
==============================================================================

Découpe l'historique disponible en 3 tiers (début / milieu / fin) et teste la
stratégie sur chacun séparément, en plus du test sur la période complète.
Objectif : voir si la stratégie tient dans différentes phases de marché,
plutôt que de se fier à un seul résultat global qui peut cacher de grosses
variations.

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

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import ccxt
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backtest")

SYMBOL = "BTC/USD"
TIMEFRAME = "1m"
BACKTEST_DAYS = int(os.environ.get("BACKTEST_DAYS", "14"))
SHORT_WINDOW = 10
LONG_WINDOW = 25
INITIAL_CAPITAL = 1000.0
FEE_RATE = 0.001

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
    while attempts < 200:
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


def run_backtest(candles):
    closes = [c[4] for c in candles]
    timestamps = [c[0] for c in candles]
    n = len(closes)

    trend_cash = INITIAL_CAPITAL * (1 - SCALP_ALLOCATION_PCT)
    trend_coins = 0.0
    trend_position = False

    scalp_cash = INITIAL_CAPITAL * SCALP_ALLOCATION_PCT
    scalp_coins = 0.0
    scalp_position = False
    scalp_entry_price = None
    scalp_entry_index = None

    equity_curve = []
    equity_timestamps = []
    trend_trades = 0
    scalp_trades = 0
    scalp_wins = 0

    for i in range(LONG_WINDOW, n):
        price = closes[i]
        short_ma = sum(closes[i - SHORT_WINDOW:i]) / SHORT_WINDOW
        long_ma = sum(closes[i - LONG_WINDOW:i]) / LONG_WINDOW
        signal_long = short_ma > long_ma

        if signal_long and not trend_position:
            spend = trend_cash * 0.95
            fee = spend * FEE_RATE
            trend_coins = (spend - fee) / price
            trend_cash -= spend
            trend_position = True
            trend_trades += 1
        elif not signal_long and trend_position:
            proceeds = trend_coins * price
            fee = proceeds * FEE_RATE
            trend_cash += proceeds - fee
            trend_coins = 0.0
            trend_position = False

        if scalp_position:
            change_pct = (price - scalp_entry_price) / scalp_entry_price * 100
            held_minutes = i - scalp_entry_index
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
                scalp_entry_index = None
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
                    scalp_entry_index = i
                    scalp_trades += 1

        total_equity = (trend_cash + trend_coins * price) + (scalp_cash + scalp_coins * price)
        equity_curve.append(total_equity)
        equity_timestamps.append(timestamps[i])

    buy_hold_curve = [INITIAL_CAPITAL * (closes[i] / closes[LONG_WINDOW]) for i in range(LONG_WINDOW, n)]

    return {
        "equity": equity_curve,
        "buy_hold": buy_hold_curve,
        "timestamps": equity_timestamps,
        "trend_trades": trend_trades,
        "scalp_trades": scalp_trades,
        "scalp_wins": scalp_wins,
    }


def summarize_period(label, candles):
    if len(candles) < LONG_WINDOW + 10:
        return f"[{label}] Pas assez de données ({len(candles)} bougies)\n"

    result = run_backtest(candles)
    equity = result["equity"]
    buy_hold = result["buy_hold"]

    total_return_pct = (equity[-1] / INITIAL_CAPITAL - 1) * 100
    buy_hold_return_pct = (buy_hold[-1] / INITIAL_CAPITAL - 1) * 100

    running_max = equity[0]
    max_drawdown = 0.0
    for e in equity:
        running_max = max(running_max, e)
        max_drawdown = min(max_drawdown, (e - running_max) / running_max * 100)

    scalp_trades = result["scalp_trades"]
    scalp_wins = result["scalp_wins"]
    scalp_win_rate = (scalp_wins / scalp_trades * 100) if scalp_trades > 0 else 0

    first_date = time.strftime("%d/%m", time.gmtime(candles[0][0] / 1000))
    last_date = time.strftime("%d/%m", time.gmtime(candles[-1][0] / 1000))

    verdict = "✅" if total_return_pct > buy_hold_return_pct else "❌"

    return (
        f"[{label}] {first_date} → {last_date}\n"
        f"  Stratégie : {total_return_pct:+.2f}% | Buy&Hold : {buy_hold_return_pct:+.2f}% {verdict}\n"
        f"  Max drawdown : {max_drawdown:.2f}% | Trades tendance : {result['trend_trades']} | "
        f"Scalp : {scalp_trades} ({scalp_win_rate:.0f}% réussite)\n"
    )


def plot_full_period(candles):
    result = run_backtest(candles)
    plt.figure(figsize=(10, 5))
    dates = [t / 1000 for t in result["timestamps"]]
    plt.plot(dates, result["equity"], label="Stratégie combinée")
    plt.plot(dates, result["buy_hold"], label="Buy & Hold", linestyle="--")
    plt.title(f"Backtest {SYMBOL} — période complète")
    plt.ylabel("Portefeuille (USD)")
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    path = "/tmp/backtest_result.png"
    plt.savefig(path)
    return path


if __name__ == "__main__":
    log.info("Démarrage du backtest multi-périodes (%d jours demandés)", BACKTEST_DAYS)
    send_telegram_text(f"🔬 Backtest multi-périodes lancé sur Railway ({BACKTEST_DAYS} jours demandés)...")

    try:
        candles = fetch_history(SYMBOL, TIMEFRAME, BACKTEST_DAYS)
        log.info("%d bougies récupérées", len(candles))

        if len(candles) < (LONG_WINDOW + 10) * 3:
            send_telegram_text(
                f"⚠️ Pas assez de données pour découper en 3 périodes ({len(candles)} bougies). "
                f"Kraken limite la profondeur d'historique en 1 minute."
            )
        else:
            n = len(candles)
            third = n // 3
            period1 = candles[:third]
            period2 = candles[third:2 * third]
            period3 = candles[2 * third:]

            summary = "📊 Backtest multi-périodes (tendance + scalp)\n\n"
            summary += summarize_period("Période complète", candles) + "\n"
            summary += summarize_period("Tiers 1 (le plus ancien)", period1) + "\n"
            summary += summarize_period("Tiers 2 (milieu)", period2) + "\n"
            summary += summarize_period("Tiers 3 (le plus récent)", period3) + "\n"
            summary += (
                "\n⚠️ Filtre news exclu. Si les résultats varient beaucoup entre "
                "tiers, la stratégie est sensible au contexte de marché — normal, "
                "mais à garder en tête. Résultat passé ≠ garantie future."
            )

            send_telegram_text(summary)

            chart_path = plot_full_period(candles)
            send_telegram_photo(chart_path, caption="Courbe sur la période complète")

            log.info("Backtest multi-périodes terminé et envoyé sur Telegram")

    except Exception as e:
        log.error("Erreur pendant le backtest : %s", e)
        send_telegram_text(f"⚠️ Erreur pendant le backtest : {e}")

    log.info("Script terminé. Remets le Custom Start Command sur 'python live_bot.py'.")
