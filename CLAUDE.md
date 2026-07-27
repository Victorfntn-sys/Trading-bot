# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Vue d'ensemble

Un bot de trading crypto BTC/USD sur Kraken, déployé sur Railway. C'est un projet minimaliste et
peu dépendant — pas de structure de package, pas de tests, pas d'étape de build. Deux scripts
autonomes, exécutés un à la fois via le "Custom Start Command" de Railway :

- **`live_bot.py`** — le bot en production. Tourne en continu (`main_loop`), interroge Kraken
  toutes les `POLL_SECONDS` (60s), gère deux « poches » de trading indépendantes (tendance +
  scalp), et envoie des notifications Telegram à chaque trade / changement d'état.
- **`backtest_on_railway.py`** — un script à exécution unique qui récupère l'historique des
  bougies depuis Kraken, rejoue la même logique de stratégie, et envoie les résultats (résumé
  texte + graphique de la courbe d'équité) sur Telegram. Conçu pour être lancé temporairement en
  changeant la commande de démarrage sur Railway, puis remis en place ensuite.

Les commentaires de code ainsi que les messages de logs/Telegram sont écrits en **français** —
respecte cette convention en modifiant les chaînes existantes ou en en ajoutant de nouvelles dans
ces fichiers.

## Développement / exécution

Il n'y a ni suite de tests, ni linter, ni étape de build configurés. Le développement consiste
essentiellement à : modifier l'un des deux scripts, l'exécuter en local avec les bonnes variables
d'environnement, puis observer les logs et/ou la sortie Telegram.

```bash
pip install -r requirements.txt   # ccxt, requests, matplotlib

# Paper trading (mode par défaut, aucun ordre réel, pas besoin de clés API hormis Telegram) :
python live_bot.py

# Backtest (récupère BACKTEST_DAYS de bougies 1m depuis Kraken et envoie le résultat sur Telegram) :
BACKTEST_DAYS=14 python backtest_on_railway.py
```

Variables d'environnement nécessaires (voir le docstring en tête de `live_bot.py`) :

| Variable | Rôle |
|---|---|
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Notifications ; le bot log un warning et ne fait rien si absentes |
| `TRADING_MODE` | `"paper"` (défaut) ou `"live"` — voir la note de sécurité ci-dessous |
| `ANTHROPIC_API_KEY` | Permet le scoring de sentiment des news via Claude (`live_bot.py` uniquement) |
| `KRAKEN_API_KEY` / `KRAKEN_API_SECRET` | Nécessaires uniquement si `TRADING_MODE=live` |
| `STATE_FILE_PATH` / `TRADES_FILE_PATH` | Surcharge des chemins par défaut (`/data/state.json`, `/data/trades.json`) |
| `BACKTEST_DAYS` | `backtest_on_railway.py` uniquement, défaut 14 |

Sur Railway, `live_bot.py` a besoin d'un **Volume monté sur `/data`** pour que `state.json` et
`trades.json` survivent aux redéploiements ; sans cela le bot fonctionne quand même mais perd sa
position/son historique à chaque déploiement.

### Sécurité du mode de trading

Le mode par défaut est le **paper trading** (simulation, aucun ordre/argent réel). Le docstring
du module dans `live_bot.py` prévient explicitement : ne jamais passer à `TRADING_MODE=live` sans
avoir observé le bot tourner en paper trading pendant plusieurs semaines au minimum. En mode
`TRADING_MODE=live`, les clés API Kraken doivent avoir **uniquement la permission de trading,
jamais de retrait**.

## Architecture

Les deux scripts implémentent la *même* logique de stratégie de façon indépendante (aucun module
partagé) — en modifiant les paramètres ou la logique de la stratégie, vérifie s'il faut répercuter
le changement dans `live_bot.py` **et** `backtest_on_railway.py` pour que les backtests restent
représentatifs du comportement réel.

### Deux poches de trading indépendantes

Le capital est réparti en deux poches qui tradent indépendamment sur les mêmes bougies `BTC/USD`
en 1 minute :

- **Poche tendance** (`1 - SCALP_ALLOCATION_PCT` du capital, 70% par défaut) : croisement de
  moyennes mobiles MA10/MA25. Position longue quand `short_ma > long_ma`. La taille de position
  varie entre `MIN_POSITION_PCT` et `MAX_POSITION_PCT` selon la force de la tendance
  (`compute_position_pct`). Les sorties sont vérifiées par ordre de priorité dans `main_loop` :
  stop-loss dur (`TREND_STOP_LOSS_PCT`) → stop-loss traînant (`check_trailing_stop`, s'active
  après `TRAILING_STOP_ACTIVATION_PCT` de profit) → objectif ROI dégressif (`ROI_TABLE`, la cible
  diminue plus la position est tenue longtemps) → signal de croisement inverse des MA.
- **Poche scalp** (`SCALP_ALLOCATION_PCT`, 30% par défaut) : cassure de momentum. Entrée quand le
  prix bouge de `SCALP_ENTRY_THRESHOLD_PCT` sur `SCALP_MOMENTUM_WINDOW` minutes. Sortie sur
  take-profit, stop-loss, ou expiration après `SCALP_MAX_HOLD_MINUTES` (`check_scalp_exit`).

### Garde-fous du bot live (`live_bot.py` uniquement, non modélisés dans le backtest)

- **Limite de perte journalière** — `DAILY_LOSS_LIMIT_PCT` : si l'équité totale du jour chute
  sous ce seuil par rapport à `day_start_equity`, `state.halted = True` et le bot arrête de
  trader jusqu'à intervention manuelle.
- **Filtre de volatilité** — `MIN_VOLATILITY_PCT` bloque les achats tendance sur un marché
  plat/calme.
- **Filtre de force de tendance** — `MIN_TREND_STRENGTH_PCT` bloque les achats tendance sur un
  croisement de MA quasi nul (bruit). Les deux seuils de filtre ont été ajustés à partir de
  données réelles observées en live — voir les commentaires en ligne près de leur définition pour
  le raisonnement/les dates avant de les modifier.
- **Stoploss guard** (`is_stoploss_guard_active`) — met en pause les nouveaux achats tendance
  pendant `STOPLOSS_GUARD_PAUSE_MINUTES` après `STOPLOSS_GUARD_TRADE_LIMIT` stop-loss survenus en
  moins de `STOPLOSS_GUARD_LOOKBACK_MINUTES`.
- **Cooldown** (`is_in_cooldown`) — bloque les nouveaux achats tendance pendant
  `COOLDOWN_MINUTES` après toute vente.
- **Filtre de sentiment news** — `fetch_news_sentiment` récupère des titres BTC depuis
  CryptoCompare, les fait scorer via un appel à l'API Claude
  (`analyze_sentiment_with_claude`, modèle `claude-haiku-4-5-20251001`), et bloque les nouveaux
  achats (les deux poches) quand le score est inférieur ou égal à `NEWS_NEGATIVE_THRESHOLD`. Mis
  en cache pendant `NEWS_REFRESH_SECONDS` (15 min). Ce filtre **n'est pas** disponible dans le
  backtest (pas d'archive news historique disponible), donc les résultats du backtest sont
  optimistes par rapport au comportement réel sur ce point.

### État et persistance (`live_bot.py`)

`BotState` détient les soldes cash/coins et les infos de position des deux poches, chargés depuis
`STATE_FILE_PATH` au démarrage et sauvegardés après chaque action modifiant l'état via
`state.save()`. Chaque trade clôturé (les deux poches) est ajouté à `TRADES_FILE_PATH` via
`log_trade` (liste plafonnée à `MAX_TRADES_LOGGED`) ; ce journal est aussi relu par
`is_stoploss_guard_active` et par `compute_trade_stats` (taux de réussite / PnL moyen, reportés
dans le message d'état Telegram périodique envoyé toutes les `STATUS_EVERY_N_LOOPS` boucles).

### Structure du backtest (`backtest_on_railway.py`)

`fetch_history` paginate les OHLCV Kraken via ccxt pour reconstituer jusqu'à `BACKTEST_DAYS` de
bougies 1 minute. `run_backtest` rejoue la logique tendance+scalp bougie par bougie sur ces
données (sans les garde-fous du live, sans filtre news). `summarize_period` est appelé quatre
fois — période complète, plus les données découpées en trois tiers (le plus ancien / milieu / le
plus récent) — pour vérifier si la stratégie tient dans différentes phases de marché plutôt que
de se fier à un seul résultat agrégé. Les résultats et un graphique de la courbe d'équité
(`plot_full_period`, sauvegardé dans `/tmp` car le système de fichiers de Railway est éphémère en
dehors de `/data`) sont envoyés sur Telegram ; rien n'est écrit de façon persistante sur disque.
