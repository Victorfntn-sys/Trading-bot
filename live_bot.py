"""
Bot de trading crypto — persistance d'état, stop-loss, filtre volatilité, journal
====================================================================================

Conçu pour tourner 24/7 sur Railway. Envoie des notifications Telegram.

MODE PAR DÉFAUT : PAPER TRADING (simulation, aucun argent réel)
Ne fais JAMAIS le switch vers "live" sans avoir observé le bot tourner en
paper trading pendant plusieurs semaines au minimum.

VARIABLES D'ENVIRONNEMENT NÉCESSAIRES :
    TELEGRAM_BOT_TOKEN   -> token donné par @BotFather sur Telegram
    TELEGRAM_CHAT_ID     -> ID de la conversation où recevoir les messages
    TRADING_MODE         -> "paper" (défaut, recommandé) ou "live"
    ANTHROPIC_API_KEY    -> clé API Anthropic pour l'analyse de news
    KRAKEN_API_KEY        -> uniquement nécessaire si TRADING_MODE=live
    KRAKEN_API_SECRET     -> uniquement nécessaire si TRADING_MODE=live

IMPORTANT — PERSISTANCE :
    Ce bot sauvegarde son état dans /data/state.json et son journal de trades
    dans /data/trades.json. Pour que ces fichiers survivent aux redéploiements,
    il FAUT ajouter un Volume sur Railway monté sur le chemin /data
    (Settings du service -> Volumes -> New Volume -> Mount path: /data).
    Sans ce volume, le bot fonctionne quand même mais perd son historique à
    chaque redéploiement (comme avant).
"""

import os
import time
import json
import logging
from datetime import datetime

import ccxt
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("trading-bot")

# ============================================================
# CONFIGURATION
# ============================================================
SYMBOL = "BTC/USD"
TIMEFRAME = "1m"
SHORT_WINDOW = 30
LONG_WINDOW = 75
VOLATILITY_WINDOW = 25  # séparée de LONG_WINDOW pour garder le seuil de volatilité calibré valide
INITIAL_CAPITAL = 1000.0
FEE_RATE = 0.001
POLL_SECONDS = 60
STATUS_EVERY_N_LOOPS = 60

MAX_POSITION_PCT = 0.95
MIN_POSITION_PCT = 0.40
TREND_STRENGTH_CAP_PCT = 1.0
DAILY_LOSS_LIMIT_PCT = -5.0

TREND_STOP_LOSS_PCT = 3.0

TRAILING_STOP_ACTIVATION_PCT = 1.0
TRAILING_STOP_PCT = 2.0

ROI_TABLE = [
    (0, 5.0),
    (60, 2.5),
    (180, 1.0),
]

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

TRADING_MODE = os.environ.get("TRADING_MODE", "paper")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

STATE_FILE = os.environ.get("STATE_FILE_PATH", "/data/state.json")
TRADES_FILE = os.environ.get("TRADES_FILE_PATH", "/data/trades.json")
MAX_TRADES_LOGGED = 500


def send_telegram(message, max_retries=3):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram non configuré, message ignoré : %s", message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(
                url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10
            )
            if resp.status_code == 200:
                return
            log.warning(
                "Envoi Telegram non confirmé (tentative %d/%d, code=%s) : %s",
                attempt, max_retries, resp.status_code, resp.text[:200],
            )
        except Exception as e:
            log.error("Échec envoi Telegram (tentative %d/%d) : %s", attempt, max_retries, e)
        if attempt < max_retries:
            time.sleep(2)
    log.error("Abandon de l'envoi Telegram après %d tentatives : %s", max_retries, message)


# ============================================================
# PERSISTANCE (état + journal de trades)
# ============================================================
def load_json_file(path, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default


def save_json_file(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log.error("Échec sauvegarde %s : %s", path, e)


def log_trade(pocket, side, price, amount, reason=None, pnl_pct=None):
    trades = load_json_file(TRADES_FILE, [])
    trades.append({
        "time": datetime.utcnow().isoformat(),
        "pocket": pocket,
        "side": side,
        "price": price,
        "amount": amount,
        "reason": reason,
        "pnl_pct": pnl_pct,
    })
    trades = trades[-MAX_TRADES_LOGGED:]
    save_json_file(TRADES_FILE, trades)


def compute_trade_stats(since_timestamp=None):
    trades = load_json_file(TRADES_FILE, [])
    sells = [t for t in trades if t["side"] == "sell" and t.get("pnl_pct") is not None]
    if since_timestamp is not None:
        sells = [t for t in sells if datetime.fromisoformat(t["time"]).timestamp() >= since_timestamp]
    if not sells:
        return None

    def stats_for(pocket_sells):
        if not pocket_sells:
            return None
        wins = [t for t in pocket_sells if t["pnl_pct"] > 0]
        return {
            "total_trades": len(pocket_sells),
            "win_rate": len(wins) / len(pocket_sells) * 100,
            "avg_pnl_pct": sum(t["pnl_pct"] for t in pocket_sells) / len(pocket_sells),
        }

    trend_sells = [t for t in sells if t.get("pocket") == "trend"]
    scalp_sells = [t for t in sells if t.get("pocket") == "scalp"]

    return {
        "overall": stats_for(sells),
        "trend": stats_for(trend_sells),
        "scalp": stats_for(scalp_sells),
    }


# ============================================================
# ÉTAT DU BOT
# ============================================================
class BotState:
    def __init__(self):
        saved = load_json_file(STATE_FILE, None)
        if saved:
            log.info("État précédent restauré depuis %s", STATE_FILE)
            self.cash = saved["cash"]
            self.coins = saved["coins"]
            self.position = saved["position"]
            self.entry_price = saved.get("entry_price")
            self.entry_time = saved.get("entry_time")
            self.trend_highest_price = saved.get("trend_highest_price")
            self.last_sell_time = saved.get("last_sell_time")
            self.guard_paused_until = saved.get("guard_paused_until")
            self.day_start_equity = saved["day_start_equity"]
            self.day_start_date = datetime.fromisoformat(saved["day_start_date"]).date()
            self.loop_count = saved["loop_count"]
            self.halted = saved["halted"]
            self.scalp_cash = saved["scalp_cash"]
            self.scalp_coins = saved["scalp_coins"]
            self.scalp_position = saved["scalp_position"]
            self.scalp_entry_price = saved.get("scalp_entry_price")
            self.scalp_entry_time = saved.get("scalp_entry_time")
        else:
            log.info("Aucun état précédent trouvé, démarrage à neuf")
            self.cash = INITIAL_CAPITAL * (1 - SCALP_ALLOCATION_PCT)
            self.coins = 0.0
            self.position = False
            self.entry_price = None
            self.entry_time = None
            self.trend_highest_price = None
            self.last_sell_time = None
            self.guard_paused_until = None
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

    def save(self):
        save_json_file(STATE_FILE, {
            "cash": self.cash,
            "coins": self.coins,
            "position": self.position,
            "entry_price": self.entry_price,
            "entry_time": self.entry_time,
            "trend_highest_price": self.trend_highest_price,
            "last_sell_time": self.last_sell_time,
            "guard_paused_until": self.guard_paused_until,
            "day_start_equity": self.day_start_equity,
            "day_start_date": self.day_start_date.isoformat(),
            "loop_count": self.loop_count,
            "halted": self.halted,
            "scalp_cash": self.scalp_cash,
            "scalp_coins": self.scalp_coins,
            "scalp_position": self.scalp_position,
            "scalp_entry_price": self.scalp_entry_price,
            "scalp_entry_time": self.scalp_entry_time,
        })


state = BotState()
BOT_START_TIME = time.time()  # sert à isoler les stats "depuis ce déploiement" de l'historique complet


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


def compute_volatility_pct(closes, window):
    recent = closes[-window:]
    if not recent:
        return 0.0
    avg = mean(recent)
    if avg == 0:
        return 0.0
    return (max(recent) - min(recent)) / avg * 100


# ============================================================
# ANALYSE DE NEWS (via Claude)
# ============================================================
NEWS_CACHE = {"score": 0, "headlines": [], "last_fetch": 0}
NEWS_REFRESH_SECONDS = 900
NEWS_NEGATIVE_THRESHOLD = -3


def fetch_raw_headlines():
    try:
        url = "https://min-api.cryptocompare.com/data/v2/news/?lang=EN&categories=BTC"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        raw_articles = data.get("Data") if isinstance(data, dict) else None
        if not isinstance(raw_articles, list):
            return []
        return [
            str(a.get("title", "")) for a in raw_articles[:15]
            if isinstance(a, dict) and a.get("title")
        ]
    except Exception as e:
        log.error("Échec récupération headlines : %s", e)
        return []


def analyze_sentiment_with_claude(headlines):
    if not ANTHROPIC_API_KEY or not headlines:
        return None

    prompt = (
        "Voici les derniers titres de news sur le Bitcoin :\n\n"
        + "\n".join(f"- {h}" for h in headlines)
        + "\n\nDonne un score de sentiment de marché entre -5 (très négatif, "
        "risque de panique/vente) et +5 (très positif, climat favorable à l'achat). "
        "Réponds UNIQUEMENT avec un objet JSON de la forme "
        '{"score": <entier>, "raison": "<une phrase courte>"}, rien d\'autre.'
    )

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["content"][0]["text"].strip()
        text = text.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(text)
        return int(parsed.get("score", 0)), parsed.get("raison", "")
    except Exception as e:
        log.error("Échec analyse Claude : %s", e)
        return None


def fetch_news_sentiment():
    now = time.time()
    if now - NEWS_CACHE["last_fetch"] < NEWS_REFRESH_SECONDS and NEWS_CACHE["headlines"]:
        return NEWS_CACHE["score"], NEWS_CACHE["headlines"]

    headlines = fetch_raw_headlines()
    if not headlines:
        NEWS_CACHE["last_fetch"] = now
        return NEWS_CACHE["score"], NEWS_CACHE["headlines"]

    result = analyze_sentiment_with_claude(headlines)
    if result is None:
        NEWS_CACHE["last_fetch"] = now
        return NEWS_CACHE["score"], NEWS_CACHE["headlines"]

    score, raison = result
    log.info("Sentiment Claude : score=%d, raison=%s", score, raison)
    NEWS_CACHE["score"] = score
    NEWS_CACHE["headlines"] = headlines[:5]
    NEWS_CACHE["last_fetch"] = now
    return score, headlines[:5]


# ============================================================
# EXÉCUTION — POCHE TENDANCE
# ============================================================
def compute_position_pct(trend_strength_pct):
    ratio = min(1.0, max(0.0, trend_strength_pct / TREND_STRENGTH_CAP_PCT))
    return MIN_POSITION_PCT + (MAX_POSITION_PCT - MIN_POSITION_PCT) * ratio


def execute_buy(exchange, price, trend_strength_pct):
    position_pct = compute_position_pct(trend_strength_pct)
    spend = state.cash * position_pct
    fee = spend * FEE_RATE
    amount = (spend - fee) / price

    if TRADING_MODE == "live":
        order = exchange.create_market_buy_order(SYMBOL, amount)
        log.info("Ordre ACHAT réel envoyé : %s", order)

    state.coins = amount
    state.cash -= spend
    state.position = True
    state.entry_price = price
    state.entry_time = time.time()
    state.trend_highest_price = price
    state.save()
    log_trade("trend", "buy", price, amount)

    send_telegram(
        f"🟢 ACHAT {SYMBOL}\nPrix : {price:.2f} USD\nMontant : {amount:.6f}\n"
        f"Taille position : {position_pct*100:.0f}% (force tendance : {trend_strength_pct:.2f}%)\n"
        f"Mode : {TRADING_MODE.upper()}\nPortefeuille : {state.equity(price):.2f} USD"
    )


def execute_sell(exchange, price, reason="signal"):
    proceeds = state.coins * price
    fee = proceeds * FEE_RATE
    pnl_pct = ((price - state.entry_price) / state.entry_price * 100) if state.entry_price else None

    if TRADING_MODE == "live":
        order = exchange.create_market_sell_order(SYMBOL, state.coins)
        log.info("Ordre VENTE réel envoyé : %s", order)

    state.cash += proceeds - fee
    sold_amount = state.coins
    state.coins = 0.0
    state.position = False
    state.entry_price = None
    state.entry_time = None
    state.trend_highest_price = None
    state.last_sell_time = time.time()
    state.save()
    log_trade("trend", "sell", price, sold_amount, reason=reason, pnl_pct=pnl_pct)

    reason_labels = {
        "stop_loss": "🛑 Stop-loss (plancher) déclenché",
        "trailing_stop": "📉 Stop-loss traînant déclenché (gain protégé)",
        "roi": "🎯 Objectif de profit atteint",
        "signal": "Signal de sortie (croisement MA)",
    }
    reason_label = reason_labels.get(reason, reason)
    pnl_text = f"\nRésultat : {pnl_pct:+.2f}%" if pnl_pct is not None else ""
    send_telegram(
        f"🔴 VENTE {SYMBOL} (tendance)\n{reason_label}\nPrix : {price:.2f} USD\n"
        f"Montant : {sold_amount:.6f}{pnl_text}\n"
        f"Mode : {TRADING_MODE.upper()}\nPortefeuille : {state.equity(price):.2f} USD"
    )


def check_roi_exit(price):
    if not state.position or not state.entry_price or not state.entry_time:
        return None
    held_minutes = (time.time() - state.entry_time) / 60
    profit_pct = (price - state.entry_price) / state.entry_price * 100

    applicable_target = ROI_TABLE[0][1]
    for minutes_threshold, target in ROI_TABLE:
        if held_minutes >= minutes_threshold:
            applicable_target = target

    if profit_pct >= applicable_target:
        return "roi"
    return None


def check_trailing_stop(price):
    if not state.position or not state.entry_price:
        return None

    if state.trend_highest_price is None or price > state.trend_highest_price:
        state.trend_highest_price = price

    profit_from_entry_pct = (state.trend_highest_price - state.entry_price) / state.entry_price * 100
    if profit_from_entry_pct >= TRAILING_STOP_ACTIVATION_PCT:
        trailing_stop_price = state.trend_highest_price * (1 - TRAILING_STOP_PCT / 100)
        if price <= trailing_stop_price:
            return "trailing_stop"
    return None


def is_stoploss_guard_active():
    if state.guard_paused_until and time.time() < state.guard_paused_until:
        return True

    trades = load_json_file(TRADES_FILE, [])
    cutoff = time.time() - STOPLOSS_GUARD_LOOKBACK_MINUTES * 60
    recent_stoplosses = [
        t for t in trades
        if t.get("pocket") == "trend"
        and t.get("side") == "sell"
        and t.get("reason") in ("stop_loss", "trailing_stop")
        and datetime.fromisoformat(t["time"]).timestamp() >= cutoff
    ]
    if len(recent_stoplosses) >= STOPLOSS_GUARD_TRADE_LIMIT:
        state.guard_paused_until = time.time() + STOPLOSS_GUARD_PAUSE_MINUTES * 60
        state.save()
        log.warning(
            "Stoploss guard activé : %d stop-loss en %d min, pause achats tendance %d min",
            len(recent_stoplosses), STOPLOSS_GUARD_LOOKBACK_MINUTES, STOPLOSS_GUARD_PAUSE_MINUTES,
        )
        send_telegram(
            f"⏸️ Pause protectrice activée\n{len(recent_stoplosses)} stop-loss tendance récents.\n"
            f"Pas de nouvel achat tendance pendant {STOPLOSS_GUARD_PAUSE_MINUTES} minutes."
        )
        return True
    return False


def is_in_cooldown():
    if not state.last_sell_time:
        return False
    return (time.time() - state.last_sell_time) < COOLDOWN_MINUTES * 60


# ============================================================
# EXÉCUTION — POCHE SCALP
# ============================================================
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
    state.save()
    log_trade("scalp", "buy", price, amount)

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
    sold_amount = state.scalp_coins
    state.scalp_coins = 0.0
    state.scalp_position = False
    state.scalp_entry_price = None
    state.scalp_entry_time = None
    state.save()
    log_trade("scalp", "sell", price, sold_amount, reason=reason, pnl_pct=change_pct)

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


# ============================================================
# BOUCLE PRINCIPALE
# ============================================================
def main_loop():
    exchange = build_exchange()

    if not os.path.isdir("/data"):
        log.warning(
            "Le dossier /data n'existe pas : ajoute un Volume Railway monté sur "
            "/data pour que l'état survive aux redéploiements."
        )

    send_telegram(
        f"🤖 Bot démarré (ou redémarré)\nSymbole : {SYMBOL}\nMode : {TRADING_MODE.upper()}\n"
        f"Position tendance : {'LONG' if state.position else 'CASH'} | Scalp : {'LONG' if state.scalp_position else 'CASH'}\n"
        f"Stop-loss : -{TREND_STOP_LOSS_PCT}% | Trailing stop actif dès +{TRAILING_STOP_ACTIVATION_PCT}%\n"
        f"ROI dégressif, cooldown {COOLDOWN_MINUTES}min, pause protectrice après {STOPLOSS_GUARD_TRADE_LIMIT} stop-loss"
    )

    while True:
        try:
            if state.halted:
                log.warning("Bot en pause (limite de perte journalière atteinte).")
                time.sleep(POLL_SECONDS)
                continue

            closes = fetch_closes(exchange, SYMBOL, TIMEFRAME, max(LONG_WINDOW, VOLATILITY_WINDOW) + 5)
            price = closes[-1]

            state.reset_daily_tracker_if_needed(price)

            current_total_equity = state.total_equity(price)
            daily_change_pct = (current_total_equity / state.day_start_equity - 1) * 100
            if daily_change_pct <= DAILY_LOSS_LIMIT_PCT:
                state.halted = True
                state.save()
                send_telegram(
                    f"⚠️ ARRÊT AUTOMATIQUE\nPerte journalière : {daily_change_pct:.2f}%\n"
                    f"Le bot est mis en pause. Intervention manuelle nécessaire pour relancer."
                )
                continue

            # --- Sorties tendance, vérifiées par ordre de priorité ---
            if state.position and state.entry_price:
                change_pct = (price - state.entry_price) / state.entry_price * 100
                exit_reason = None

                if change_pct <= -TREND_STOP_LOSS_PCT:
                    exit_reason = "stop_loss"
                else:
                    trailing_reason = check_trailing_stop(price)
                    if trailing_reason:
                        exit_reason = trailing_reason
                    else:
                        roi_reason = check_roi_exit(price)
                        if roi_reason:
                            exit_reason = roi_reason

                if exit_reason:
                    execute_sell(exchange, price, reason=exit_reason)

            short_ma = mean(closes[-SHORT_WINDOW:])
            long_ma = mean(closes[-LONG_WINDOW:])
            signal_long = short_ma > long_ma
            trend_strength_pct = (short_ma - long_ma) / long_ma * 100 if long_ma else 0
            volatility_pct = compute_volatility_pct(closes, VOLATILITY_WINDOW)

            news_score, headlines = fetch_news_sentiment()
            news_blocks_buy = news_score <= NEWS_NEGATIVE_THRESHOLD
            volatility_blocks_buy = volatility_pct < MIN_VOLATILITY_PCT
            trend_too_weak = trend_strength_pct < MIN_TREND_STRENGTH_PCT

            if signal_long and not state.position:
                if news_blocks_buy:
                    log.info("Achat tendance bloqué par les news (score=%d)", news_score)
                elif volatility_blocks_buy:
                    log.info(
                        "Achat tendance bloqué par le filtre volatilité (%.2f%% < %.2f%%)",
                        volatility_pct, MIN_VOLATILITY_PCT,
                    )
                elif trend_too_weak:
                    log.info(
                        "Achat tendance bloqué : croisement trop faible (%.2f%% < %.2f%%)",
                        trend_strength_pct, MIN_TREND_STRENGTH_PCT,
                    )
                elif is_in_cooldown():
                    log.info("Achat tendance bloqué par le cooldown post-vente")
                elif is_stoploss_guard_active():
                    log.info("Achat tendance bloqué par le stoploss guard")
                else:
                    execute_buy(exchange, price, trend_strength_pct)
            elif not signal_long and state.position:
                execute_sell(exchange, price, reason="signal")

            # --- Poche court terme (scalping) ---
            if state.scalp_position:
                exit_reason = check_scalp_exit(price)
                if exit_reason:
                    execute_scalp_sell(price, exit_reason)
            else:
                opportunity, momentum_pct = check_scalp_opportunity(closes)
                if opportunity and not news_blocks_buy:
                    execute_scalp_buy(price, momentum_pct)

            state.loop_count += 1
            state.save()

            if state.loop_count % STATUS_EVERY_N_LOOPS == 0:
                pnl_pct = (current_total_equity / INITIAL_CAPITAL - 1) * 100
                stats_all_time = compute_trade_stats()
                stats_since_deploy = compute_trade_stats(since_timestamp=BOT_START_TIME)

                def fmt(label, s):
                    if not s:
                        return f"{label} : aucun trade clôturé"
                    return f"{label} : {s['total_trades']} trades, {s['win_rate']:.0f}% réussite, {s['avg_pnl_pct']:+.2f}% moy."

                if stats_since_deploy:
                    deploy_text = (
                        f"\n\n📌 Depuis ce déploiement :"
                        f"\n{fmt('Tendance', stats_since_deploy['trend'])}"
                        f"\n{fmt('Scalp', stats_since_deploy['scalp'])}"
                    )
                else:
                    deploy_text = "\n\n📌 Depuis ce déploiement : aucun trade clôturé pour l'instant"

                if stats_all_time:
                    stats_text = (
                        f"\n{fmt('Global (tout historique)', stats_all_time['overall'])}"
                        f"{deploy_text}"
                    )
                else:
                    stats_text = "\nHistorique : pas encore assez de trades clôturés"

                send_telegram(
                    f"📊 État du bot\nPrix {SYMBOL} : {price:.2f}\n"
                    f"Position tendance : {'LONG' if state.position else 'CASH'}\n"
                    f"Position scalp : {'LONG' if state.scalp_position else 'CASH'}\n"
                    f"Portefeuille total : {current_total_equity:.2f} USD ({pnl_pct:+.2f}%)\n"
                    f"Sentiment news : {news_score} | Volatilité : {volatility_pct:.2f}%"
                    f"{stats_text}"
                )

            log.info(
                "Prix=%.2f MA%d=%.2f MA%d=%.2f Tendance=%s Scalp=%s Equity=%.2f News=%d Vol=%.2f%%",
                price, SHORT_WINDOW, short_ma, LONG_WINDOW, long_ma,
                "LONG" if state.position else "CASH",
                "LONG" if state.scalp_position else "CASH",
                current_total_equity, news_score, volatility_pct,
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
#    (jamais de permission de retrait)
# 2. Définis TRADING_MODE=live, KRAKEN_API_KEY et KRAKEN_API_SECRET dans
#    les variables d'environnement du serveur
# 3. Commence avec un tout petit capital, pas ton épargne
# 4. Surveille de près les premiers jours
