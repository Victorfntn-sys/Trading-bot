"""
Bot de trading crypto avec notifications Telegram
===================================================

Conçu pour tourner 24/7 sur un serveur cloud (Railway, Render, etc.)
Envoie une notification Telegram à chaque trade (et un état périodique).

MODE PAR DÉFAUT : PAPER TRADING (simulation, aucun argent réel)
Pour passer en argent réel, voir la section "PASSER EN LIVE" tout en bas.
Ne fais JAMAIS ce switch sans avoir observé le bot tourner en paper trading
pendant plusieurs semaines au minimum.

VARIABLES D'ENVIRONNEMENT NÉCESSAIRES (à définir sur Railway/Render, jamais en dur dans le code) :
    TELEGRAM_BOT_TOKEN   -> token donné par @BotFather sur Telegram
    TELEGRAM_CHAT_ID     -> ID de la conversation où recevoir les messages
    TRADING_MODE         -> "paper" (défaut, recommandé) ou "live"
    KRAKEN_API_KEY       -> uniquement nécessaire si TRADING_MODE=live
    KRAKEN_API_SECRET    -> uniquement nécessaire si TRADING_MODE=live
"""

import os
import time
import logging
from datetime import datetime

import ccxt
import requests

# ============================================================
# CONFIGURATION
# ============================================================
SYMBOL = "BTC/USD"
TIMEFRAME = "1m"
SHORT_WINDOW = 10
LONG_WINDOW = 25
INITIAL_CAPITAL = 1000.0
FEE_RATE = 0.001
POLL_SECONDS = 60
STATUS_EVERY_N_LOOPS = 60

MAX_POSITION_PCT = 0.95
DAILY_LOSS_LIMIT_PCT = -5.0

TRADING_MODE = os.environ.get("TRADING_MODE", "paper")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("trading-bot")


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram non configuré, message ignoré : %s", message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
    except Exception as e:
        log.error("Échec envoi Telegram : %s", e)


class BotState:
    def __init__(self):
        self.cash = INITIAL_CAPITAL
        self.coins = 0.0
        self.position = False
        self.day_start_equity = INITIAL_CAPITAL
        self.day_start_date = datetime.utcnow().date()
        self.loop_count = 0
        self.halted = False

    def equity(self, price):
        return self.cash + self.coins * price

    def reset_daily_tracker_if_needed(self, price):
        today = datetime.utcnow().date()
        if today != self.day_start_date:
            self.day_start_date = today
            self.day_start_equity = self.equity(price)


state = BotState()


def build_exchange():
    if TRADING_MODE == "live":
        api_key = os.environ.get("KRAKEN_API_KEY")
        api_secret = os.environ.get("KRAKEN_API_SECRET")
        if not api_key or not api_secret:
            raise RuntimeError("TRADING_MODE=live mais KRAKEN_API_KEY/SECRET manquants")
        return ccxt.kraken({"apiKey": api_key, "secret": api_secret, "enableRateLimit": True})
    return ccxt.kraken({"enableRateLimit": True})


def mean(values):
    return sum(values) / len(values)


def fetch_closes(exchange, symbol, timeframe, limit):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    return [c[4] for c in ohlcv]


def execute_buy(exchange, price):
    spend = state.cash * MAX_POSITION_PCT
    fee = spend * FEE_RATE

    if TRADING_MODE == "live":
        amount = (spend - fee) / price
        order = exchange.create_market_buy_order(SYMBOL, amount)
        log.info("Ordre ACHAT réel envoyé : %s", order)
    else:
        amount = (spend - fee) / price

    state.coins = amount
    state.cash -= spend
    state.position = True

    send_telegram(
        f"🟢 ACHAT {SYMBOL}\nPrix : {price:.2f} USD\nMontant : {amount:.6f}\n"
        f"Mode : {TRADING_MODE.upper()}\nPortefeuille : {state.equity(price):.2f} USD"
    )


def execute_sell(exchange, price):
    proceeds = state.coins * price
    fee = proceeds * FEE_RATE

    if TRADING_MODE == "live":
        order = exchange.create_market_sell_order(SYMBOL, state.coins)
        log.info("Ordre VENTE réel envoyé : %s", order)

    state.cash += proceeds - fee
    sold_amount = state.coins
    state.coins = 0.0
    state.position = False

    send_telegram(
        f"🔴 VENTE {SYMBOL}\nPrix : {price:.2f} USD\nMontant : {sold_amount:.6f}\n"
        f"Mode : {TRADING_MODE.upper()}\nPortefeuille : {state.equity(price):.2f} USD"
    )


def main_loop():
    exchange = build_exchange()
    send_telegram(
        f"🤖 Bot démarré\nSymbole : {SYMBOL}\nMode : {TRADING_MODE.upper()}\n"
        f"Capital initial : {INITIAL_CAPITAL} USD\n"
        f"Stratégie : MA{SHORT_WINDOW}/MA{LONG_WINDOW} crossover"
    )

    while True:
        try:
            if state.halted:
                log.warning("Bot en pause (limite de perte journalière atteinte). Vérification manuelle requise.")
                time.sleep(POLL_SECONDS)
                continue

            closes = fetch_closes(exchange, SYMBOL, TIMEFRAME, LONG_WINDOW + 5)
            price = closes[-1]

            state.reset_daily_tracker_if_needed(price)

            current_equity = state.equity(price)
            daily_change_pct = (current_equity / state.day_start_equity - 1) * 100
            if daily_change_pct <= DAILY_LOSS_LIMIT_PCT:
                state.halted = True
                send_telegram(
                    f"⚠️ ARRÊT AUTOMATIQUE\nPerte journalière : {daily_change_pct:.2f}%\n"
                    f"Le bot est mis en pause. Intervention manuelle nécessaire pour relancer."
                )
                continue

            short_ma = mean(closes[-SHORT_WINDOW:])
            long_ma = mean(closes[-LONG_WINDOW:])
            signal_long = short_ma > long_ma

            if signal_long and not state.position:
                execute_buy(exchange, price)
            elif not signal_long and state.position:
                execute_sell(exchange, price)

            state.loop_count += 1
            if state.loop_count % STATUS_EVERY_N_LOOPS == 0:
                pnl_pct = (current_equity / INITIAL_CAPITAL - 1) * 100
                send_telegram(
                    f"📊 État du bot\nPrix {SYMBOL} : {price:.2f}\n"
                    f"Position : {'LONG' if state.position else 'CASH'}\n"
                    f"Portefeuille : {current_equity:.2f} USD ({pnl_pct:+.2f}%)"
                )

            log.info(
                "Prix=%.2f MA%d=%.2f MA%d=%.2f Position=%s Equity=%.2f",
                price, SHORT_WINDOW, short_ma, LONG_WINDOW, long_ma,
                "LONG" if state.position else "CASH", current_equity,
            )

        except Exception as e:
            log.error("Erreur dans la boucle principale : %s", e)
            send_telegram(f"⚠️ Erreur bot : {e}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    log.info("Démarrage du bot en mode %s", TRADING_MODE.upper())
    main_loop()
