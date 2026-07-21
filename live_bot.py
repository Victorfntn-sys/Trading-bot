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
    BINANCE_API_KEY      -> uniquement nécessaire si TRADING_MODE=live
    BINANCE_API_SECRET   -> uniquement nécessaire si TRADING_MODE=live
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
SYMBOL = "BTC/USDT"
TIMEFRAME = "1m"
SHORT_WINDOW = 10
LONG_WINDOW = 25
INITIAL_CAPITAL = 1000.0
FEE_RATE = 0.001
POLL_SECONDS = 60          # vérifie le marché toutes les 60s
STATUS_EVERY_N_LOOPS = 60  # envoie un état Telegram toutes les ~60 boucles (~1h)

# Garde-fous de sécurité (actifs même en mode live)
MAX_POSITION_PCT = 0.95     # jamais investir plus de 95% du capital dans un seul trade
DAILY_LOSS_LIMIT_PCT = -5.0 # si le portefeuille chute de plus de 5% dans la journée, le bot s'arrête

TRADING_MODE = os.environ.get("TRADING_MODE", "paper")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("trading-bot")


# ============================================================
# TELEGRAM
# ============================================================
def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram non configuré, message ignoré : %s", message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
    except Exception as e:
        log.error("Échec envoi Telegram : %s", e)


# ============================================================
# ÉTAT DU BOT (persiste en mémoire pendant que le process tourne)
# ============================================================
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


# ============================================================
# EXCHANGE
# ============================================================
def build_exchange():
    if TRADING_MODE == "live":
        api_key = os.environ.get("BINANCE_API_KEY")
        api_secret = os.environ.get("BINANCE_API_SECRET")
        if not api_key or not api_secret:
            raise RuntimeError("TRADING_MODE=live mais BINANCE_API_KEY/SECRET manquants")
        return ccxt.binance({"apiKey": api_key, "secret": api_secret, "enableRateLimit": True})
    # En mode paper, on utilise quand même ccxt pour LIRE les prix (données publiques, pas besoin de clé)
    return ccxt.binance({"enableRateLimit": True})


def mean(values):
    return sum(values) / len(values)


def fetch_closes(exchange, symbol, timeframe, limit):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    return [c[4] for c in ohlcv]


# ============================================================
# EXÉCUTION D'ORDRE (paper ou live selon TRADING_MODE)
# ============================================================
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
        f"🟢 ACHAT {SYMBOL}\nPrix : {price:.2f} USDT\nMontant : {amount:.6f}\n"
        f"Mode : {TRADING_MODE.upper()}\nPortefeuille : {state.equity(price):.2f} USDT"
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
        f"🔴 VENTE {SYMBOL}\nPrix : {price:.2f} USDT\nMontant : {sold_amount:.6f}\n"
        f"Mode : {TRADING_MODE.upper()}\nPortefeuille : {state.equity(price):.2f} USDT"
    )


# ============================================================
# BOUCLE PRINCIPALE
# ============================================================
def main_loop():
    exchange = build_exchange()
    send_telegram(
        f"🤖 Bot démarré\nSymbole : {SYMBOL}\nMode : {TRADING_MODE.upper()}\n"
        f"Capital initial : {INITIAL_CAPITAL} USDT\n"
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

            # Garde-fou : limite de perte journalière
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
                    f"Portefeuille : {current_equity:.2f} USDT ({pnl_pct:+.2f}%)"
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


# ============================================================
# PASSER EN LIVE (argent réel) — À NE FAIRE QU'APRÈS SEMAINES DE PAPER TRADING
# ============================================================
# 1. Crée des clés API sur Binance avec UNIQUEMENT la permission "Enable Trading"
#    (jamais "Enable Withdrawals" — ça permettrait à un bug ou un vol de clé de vider ton compte)
# 2. Restreins les clés API par IP si Binance le permet (IP fixe de ton serveur Railway/Render)
# 3. Définis TRADING_MODE=live, BINANCE_API_KEY et BINANCE_API_SECRET dans les variables
#    d'environnement du serveur (jamais dans le code, jamais sur GitHub)
# 4. Commence avec un tout petit capital (ex: 50-100 USDT), pas ton épargne
# 5. Surveille de près les premiers jours
