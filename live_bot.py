"""
Bot de trading crypto avec notifications Telegram et filtre de sentiment news
==============================================================================

Conçu pour tourner 24/7 sur un serveur cloud (Railway, Render, etc.)
Envoie une notification Telegram à chaque trade (et un état périodique).

MODE PAR DÉFAUT : PAPER TRADING (simulation, aucun argent réel)
Pour passer en argent réel, voir la section "PASSER EN LIVE" tout en bas.
Ne fais JAMAIS ce switch sans avoir observé le bot tourner en paper trading
pendant plusieurs semaines au minimum.

VARIABLES D'ENVIRONNEMENT NÉCESSAIRES (à définir sur Railway, jamais en dur dans le code) :
    TELEGRAM_BOT_TOKEN   -> token donné par @BotFather sur Telegram
    TELEGRAM_CHAT_ID     -> ID de la conversation où recevoir les messages
    TRADING_MODE         -> "paper" (défaut, recommandé) ou "live"
    KRAKEN_API_KEY        -> uniquement nécessaire si TRADING_MODE=live
    KRAKEN_API_SECRET     -> uniquement nécessaire si TRADING_MODE=live
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

SCALP_ALLOCATION_PCT = 0.30
SCALP_MOMENTUM_WINDOW = 5
SCALP_ENTRY_THRESHOLD_PCT = 1.5
SCALP_TAKE_PROFIT_PCT = 1.0
SCALP_STOP_LOSS_PCT = 0.5
SCALP_MAX_HOLD_MINUTES = 30

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
        self.cash = INITIAL_CAPITAL * (1 - SCALP_ALLOCATION_PCT)
        self.coins = 0.0
        self.position = False
        self.day_start_equity = INITIAL_CAPITAL
        self.day_start_date = datetime.utcnow().date()
        self.loop_count = 0
        self.halted = False

        self.scalp_cash = INITIAL_CAPITAL * SCALP_ALLOCATION_PCT
        self.scalp_coins = 0.0
        self.scalp_position = False
        self.scalp_entry_price = None
        self.scalp_entry_time = None

    def equity(self, price):
        return self.cash + self.coins * price

    def scalp_equity(self, price):
        return self.scalp_cash + self.scalp_coins * price

    def total_equity(self, price):
        return self.equity(price) + self.scalp_equity(price)

    def reset_daily_tracker_if_needed(self, price):
        today = datetime.utcnow().date()
        if today != self.day_start_date:
            self.day_start_date = today
            self.day_start_equity = self.total_equity(price)


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


POSITIVE_WORDS = [
    "surge", "rally", "bullish", "gain", "adoption", "approval", "breakout",
    "record high", "inflow", "upgrade", "partnership", "institutional buy",
]
NEGATIVE_WORDS = [
    "crash", "plunge", "bearish", "hack", "exploit", "ban", "lawsuit",
    "investigation", "sell-off", "selloff", "liquidation", "collapse",
    "fraud", "scam", "outflow", "regulatory crackdown",
]

NEWS_CACHE = {"score": 0, "headlines": [], "last_fetch": 0}
NEWS_REFRESH_SECONDS = 900
NEWS_NEGATIVE_THRESHOLD = -3


def fetch_news_sentiment():
    now = time.time()
    if now - NEWS_CACHE["last_fetch"] < NEWS_REFRESH_SECONDS and NEWS_CACHE["headlines"]:
        return NEWS_CACHE["score"], NEWS_CACHE["headlines"]

    try:
        url = "https://min-api.cryptocompare.com/data/v2/news/?lang=EN&categories=BTC"
        resp = requests.get(url, timeout=10)
        data = resp.json()

        raw_articles = data.get("Data") if isinstance(data, dict) else None
        if not isinstance(raw_articles, list):
            log.warning("Réponse news inattendue (pas de liste d'articles), sentiment neutre par défaut")
            NEWS_CACHE["last_fetch"] = now
            return NEWS_CACHE["score"], NEWS_CACHE["headlines"]

        articles = raw_articles[:15]

        score = 0
        headlines = []
        for article in articles:
            if not isinstance(article, dict):
                continue
            title = str(article.get("title", "")).lower()
            headlines.append(article.get("title", ""))
            for word in POSITIVE_WORDS:
                if word in title:
                    score += 1
            for word in NEGATIVE_WORDS:
                if word in title:
                    score -= 1

        NEWS_CACHE["score"] = score
        NEWS_CACHE["headlines"] = headlines[:5]
        NEWS_CACHE["last_fetch"] = now
        return score, headlines[:5]

    except Exception as e:
        log.error("Échec récupération news : %s", e)
        NEWS_CACHE["last_fetch"] = now
        return NEWS_CACHE["score"], NEWS_CACHE["headlines"]


def execute_buy(exchange, price):
    spend = state.cash * MAX_POSITION_PCT
    fee = spend * FEE_RATE
    amount = (spend - fee) / price

    if TRADING_MODE == "live":
        order = exchange.create_market_buy_order(SYMBOL, amount)
        log.info("Ordre ACHAT réel envoyé : %s", order)

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
        f"🔴 VENTE {SYMBOL} (tendance)\nPrix : {price:.2f} USD\nMontant : {sold_amount:.6f}\n"
        f"Mode : {TRADING_MODE.upper()}\nPortefeuille : {state.equity(price):.2f} USD"
    )


def check_scalp_opportunity(closes):
    if len(closes) < SCALP_MOMENTUM_WINDOW + 1:
        return False, 0.0
    past_price = closes[-(SCALP_MOMENTUM_WINDOW + 1)]
    current_price = closes[-1]
    if past_price == 0:
        return False, 0.0
    change_pct = (current_price - past_price) / past_price * 100
    return change_pct >= SCALP_ENTRY_THRESHOLD_PCT, change_pct


def execute_scalp_buy(price, momentum_pct):
    spend = state.scalp_cash
    fee = spend * FEE_RATE
    amount = (spend - fee) / price

    state.scalp_coins = amount
    state.scalp_cash = 0.0
    state.scalp_position = True
    state.scalp_entry_price = price
    state.scalp_entry_time = time.time()

    send_telegram(
        f"⚡ ACHAT COURT TERME {SYMBOL}\nPrix : {price:.2f} USD\n"
        f"Mouvement détecté : +{momentum_pct:.2f}% sur {SCALP_MOMENTUM_WINDOW}min\n"
        f"Cible : +{SCALP_TAKE_PROFIT_PCT}% / Stop : -{SCALP_STOP_LOSS_PCT}% / Max {SCALP_MAX_HOLD_MINUTES}min"
    )


def check_scalp_exit(price):
    if not state.scalp_position:
        return None

    change_pct = (price - state.scalp_entry_price) / state.scalp_entry_price * 100
    held_minutes = (time.time() - state.scalp_entry_time) / 60

    if change_pct >= SCALP_TAKE_PROFIT_PCT:
        return "take_profit"
    if change_pct <= -SCALP_STOP_LOSS_PCT:
        return "stop_loss"
    if held_minutes >= SCALP_MAX_HOLD_MINUTES:
        return "timeout"
    return None


def execute_scalp_sell(price, reason):
    proceeds = state.scalp_coins * price
    fee = proceeds * FEE_RATE
    change_pct = (price - state.scalp_entry_price) / state.scalp_entry_price * 100

    state.scalp_cash = proceeds - fee
    state.scalp_coins = 0.0
    state.scalp_position = False
    state.scalp_entry_price = None
    state.scalp_entry_time = None

    reason_labels = {
        "take_profit": "🎯 Objectif atteint",
        "stop_loss": "🛑 Stop loss déclenché",
        "timeout": "⏱️ Délai max atteint",
    }
    send_telegram(
        f"⚡ VENTE COURT TERME {SYMBOL}\n{reason_labels.get(reason, reason)}\n"
        f"Prix : {price:.2f} USD\nRésultat : {change_pct:+.2f}%\n"
        f"Poche scalp : {state.scalp_cash:.2f} USD"
    )


def main_loop():
    exchange = build_exchange()
    send_telegram(
        f"🤖 Bot démarré\nSymbole : {SYMBOL}\nMode : {TRADING_MODE.upper()}\n"
        f"Capital initial : {INITIAL_CAPITAL} USD\n"
        f"Poche tendance (MA{SHORT_WINDOW}/MA{LONG_WINDOW}) : {INITIAL_CAPITAL * (1 - SCALP_ALLOCATION_PCT):.0f} USD\n"
        f"Poche court terme (scalp) : {INITIAL_CAPITAL * SCALP_ALLOCATION_PCT:.0f} USD\n"
        f"Filtre news actif"
    )

    while True:
        try:
            if state.halted:
                log.warning("Bot en pause (limite de perte journalière atteinte).")
                time.sleep(POLL_SECONDS)
                continue

            closes = fetch_closes(exchange, SYMBOL, TIMEFRAME, LONG_WINDOW + 5)
            price = closes[-1]

            state.reset_daily_tracker_if_needed(price)

            current_total_equity = state.total_equity(price)
            daily_change_pct = (current_total_equity / state.day_start_equity - 1) * 100
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

            news_score, headlines = fetch_news_sentiment()
            news_blocks_buy = news_score <= NEWS_NEGATIVE_THRESHOLD

            if signal_long and not state.position:
                if news_blocks_buy:
                    log.info("Achat bloqué par le filtre news (score=%d)", news_score)
                    send_telegram(
                        f"⏸️ Achat bloqué par les news\nScore sentiment : {news_score}\n"
                        f"Le signal technique était à l'achat, mais le climat des news "
                        f"est trop négatif pour agir."
                    )
                else:
                    execute_buy(exchange, price)
            elif not signal_long and state.position:
                execute_sell(exchange, price)

            if state.scalp_position:
                exit_reason = check_scalp_exit(price)
                if exit_reason:
                    execute_scalp_sell(price, exit_reason)
            else:
                opportunity, momentum_pct = check_scalp_opportunity(closes)
                if opportunity and not news_blocks_buy:
                    execute_scalp_buy(price, momentum_pct)

            state.loop_count += 1
            if state.loop_count % STATUS_EVERY_N_LOOPS == 0:
                pnl_pct = (current_total_equity / INITIAL_CAPITAL - 1) * 100
                send_telegram(
                    f"📊 État du bot\nPrix {SYMBOL} : {price:.2f}\n"
                    f"Position tendance : {'LONG' if state.position else 'CASH'}\n"
                    f"Position scalp : {'LONG' if state.scalp_position else 'CASH'}\n"
                    f"Portefeuille total : {current_total_equity:.2f} USD ({pnl_pct:+.2f}%)\n"
                    f"Sentiment news : {news_score}"
                )

            log.info(
                "Prix=%.2f MA%d=%.2f MA%d=%.2f Tendance=%s Scalp=%s Equity=%.2f News=%d",
                price, SHORT_WINDOW, short_ma, LONG_WINDOW, long_ma,
                "LONG" if state.position else "CASH",
                "LONG" if state.scalp_position else "CASH",
                current_total_equity, news_score,
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
# 1. Crée des clés API sur Kraken avec UNIQUEMENT la permission de trading
#    (jamais de permission de retrait — ça permettrait à un bug ou un vol
#    de clé de vider ton compte)
# 2. Définis TRADING_MODE=live, KRAKEN_API_KEY et KRAKEN_API_SECRET dans
#    les variables d'environnement du serveur (jamais dans le code)
# 3. Commence avec un tout petit capital, pas ton épargne
# 4. Surveille de près les premiers jours
