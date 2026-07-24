"""
Backtest exécuté directement sur Railway — résultats envoyés sur Telegram
===========================================================================

Contrairement à un backtest local, ce script tourne dans le même environnement
que le bot (Railway), utilise les mêmes variables d'environnement Telegram,
et envoie le résultat (résumé texte + graphique) directement dans ta
conversation Telegram. Aucune installation sur ton téléphone/ordinateur.

COMMENT L'UTILISER :
1. Sur Railway, va dans Settings du service → cherche "Custom Start Command"
2. Remplace temporairement par : python backtest_on_railway.py
3. Sauvegarde (ça redéploie automatiquement)
4. Attends quelques minutes — les résultats arrivent sur Telegram
5. Une fois reçus, REMETS le Custom Start Command sur : python live_bot.py
   (sinon le bot de trading normal ne tournera plus)

Le filtre news n'est pas inclus (pas d'archive historique de news gratuite
disponible). Seules les stratégies tendance + scalp sont testées, sur les
données réellement disponibles chez Kraken (leur profondeur d'historique en
bougies 1 minute est limitée ; le résumé indique la période réellement couverte).
"""

import os
import time
import logging

import matplotlib
matplotlib.use("Agg")  # pas d'affichage graphique sur un serveur, on sauvegarde en fichier
import matplotlib.pyplot as plt

import ccxt
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backtest")

# ============================================================
# CONFIGURATION (identique au bot en production)
# ============================================================
SYMBOL = "BTC/USD"
TIMEFRAME = "1m"
BACKTEST_DAYS = int(os.environ.get("BACKTEST_DAYS", "14"))  # ajustable via variable d'env
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
    while attempts < 200:  # garde-fou pour ne jamais tourner indéfiniment
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
            exit_now = False
            if change_pct >= SCALP_TAKE_PROFIT_PCT:
                exit_now, win = True, True
            elif change_pct <= -SCALP_STOP_LOSS_PCT:
                exit_now, win = True, False
            elif held_minutes >= SCALP_MAX_HOLD_MINUTES:
                exit_now, win = True, change_pct > 0
            else:
                win = False

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
        dd = (e - running_max) / running_max * 100
        max_drawdown = min(max_drawdown, dd)

    scalp_trades = result["scalp_trades"]
    scalp_wins = result["scalp_wins"]
    scalp_win_rate = (scalp_wins / scalp_trades * 100) if scalp_trades > 0 else 0

    first_ts = candles[0][0] / 1000
    last_ts = candles[-1][0] / 1000
    actual_days = (last_ts - first_ts) / 86400

    verdict = (
        "✅ La stratégie bat le buy & hold sur cette période."
        if total_return_pct > buy_hold_return_pct
        else "❌ Le simple buy & hold fait mieux ici."
    )

    return (
        f"📊 Résultat du backtest (tendance + scalp)\n\n"
        f"Symbole : {SYMBOL}\n"
        f"Période réellement couverte : {actual_days:.1f} jours ({len(candles)} bougies 1min)\n\n"
        f"Capital initial : {INITIAL_CAPITAL:.0f} USD\n"
        f"Capital final : {final_equity:.2f} USD ({total_return_pct:+.2f}%)\n"
        f"Buy & hold équivalent : {final_buy_hold:.2f} USD ({buy_hold_return_pct:+.2f}%)\n"
        f"Max drawdown : {max_drawdown:.2f}%\n\n"
        f"Trades tendance : {result['trend_trades']}\n"
        f"Trades scalp : {scalp_trades} (réussite ~{scalp_win_rate:.0f}%)\n\n"
        f"{verdict}\n\n"
        f"⚠️ Filtre news exclu (pas d'historique dispo). Résultat passé ≠ garantie future."
    )


def plot_and_save(result):
    plt.figure(figsize=(10, 5))
    dates = [t / 1000 for t in result["timestamps"]]
    plt.plot(dates, result["equity"], label="Stratégie combinée")
    plt.plot(dates, result["buy_hold"], label="Buy & Hold", linestyle="--")
    plt.title(f"Backtest {SYMBOL} — Tendance + Scalp")
    plt.ylabel("Portefeuille (USD)")
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    path = "/tmp/backtest_result.png"
    plt.savefig(path)
    return path


if __name__ == "__main__":
    log.info("Démarrage du backtest (%d jours demandés)", BACKTEST_DAYS)
    send_telegram_text(f"🔬 Backtest lancé sur Railway ({BACKTEST_DAYS} jours demandés)...")

    try:
        candles = fetch_history(SYMBOL, TIMEFRAME, BACKTEST_DAYS)
        log.info("%d bougies récupérées", len(candles))

        if len(candles) < LONG_WINDOW + 10:
            send_telegram_text(
                f"⚠️ Pas assez de données historiques disponibles ({len(candles)} bougies). "
                f"Kraken limite la profondeur d'historique en 1 minute."
            )
        else:
            result = run_backtest(candles)
            summary = build_summary(result, candles)
            send_telegram_text(summary)

            chart_path = plot_and_save(result)
            send_telegram_photo(chart_path, caption="Courbe du portefeuille (stratégie vs buy & hold)")

            log.info("Backtest terminé et envoyé sur Telegram")

    except Exception as e:
        log.error("Erreur pendant le backtest : %s", e)
        send_telegram_text(f"⚠️ Erreur pendant le backtest : {e}")

    log.info("Script terminé. Remets le Custom Start Command sur 'python live_bot.py' pour reprendre le trading normal.")
