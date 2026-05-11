# Veteran Day Trading Playbook Pro

This local app is built to follow your playbook in order:
1. Screen for catalyst, RVOL/liquidity, spread, float proxy, and ATR
2. Validate with daily alignment, relative strength, and intraday tape proxies
3. Execute with a defined entry, stop, and 2 profit targets

## What it does
- Morning scan for top candidates
- Gemini-based catalyst scoring when a Gemini key is present
- Minimal operator console for bot status, best candidate, attempts, trades, and blockers
- Alpaca paper-trade managed execution:
  - bid/ask-pegged limit entry
  - 30-second entry timeout + auto-cancel
  - full-size protective stop immediately after fill; quick-profit monitor can scale out and re-protect the runner

- SQLite scan history and trade journal
- Optional market internals long-block filter using $TICK + $ADD
- Daily volume-profile POC gate (blocks buys below POC)
- Optional parallel crypto scanner for 24/7 reps
- Exact plain-English panel for:
  - Day of the Week: What Stock to Watch
  - Buy only after 10:00 AM ET if it is between $X and $Y
  - Buy 5 shares max
  - Stop
  - Take profit range

## Important truth
This app is a disciplined ranking and execution assistant. It does not guarantee profit.

## Windows setup
1. Install Python 3.11
2. Unzip this folder somewhere simple, such as `C:\veteran-best-app`
3. Open PowerShell in the folder
4. Run:

```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
python app.py
```

Then open `http://127.0.0.1:5000`

## Keys
Required:
- Alpaca paper/data key and secret

Recommended:
- Finnhub key for company news and profile data
- Gemini key for true catalyst scoring

## Notes
- The float rule uses Finnhub shares outstanding as a proxy when a true public float source is unavailable.
- The UI is intentionally minimal. Use bot status, auto-attempts, recent trades, and preflight blockers to judge whether the system is operating.
- Bracket orders are sent only to Alpaca paper trading.


## Upgrade notes
- If you already have an existing `.env`, add `AUTO_SCAN_END_ET=15:15`; otherwise a legacy `MORNING_SCAN_END_ET=11:00` can end auto scans too early.
- The scanner now classifies each day as A+, A, WATCH, or NO TRADE.
- Paper auto execution can trade A/A+ setups and, when ACTIVE_PAPER_TRADING_MODE=1, bounded WATCH-grade fallback setups.
- Premarket gap, premarket dollar volume, and sector sympathy now materially affect ranking.
- Use `python analyze_performance.py` to analyze `veteran_trades.db` for win-rate by confidence level and time-window lockout candidates.


## Startup reliability notes
- If SQLite cannot write to the default DB path, the app now falls back to `/tmp/veteran_trades.db` (or `DB_FALLBACK_DIR`).
- Check runtime status at `GET /api/runtime-health` to verify active DB path and websocket proxy hint.

## Autonomous paper-trading runtime

- Health endpoints:
  - `GET /api/runtime-health`
  - `GET /api/bot-status` (engine/scheduler/thread state, last scan/auto-trade/monitor statuses, recent scans/trades).
- Autonomous controls are configured via `.env`:
  - `AUTO_START_EXECUTION_ENGINE`, `AUTO_TRADE_ENABLED`, `AUTO_SCAN_INTERVAL_SECONDS`, `POSITION_MONITOR_INTERVAL_SECONDS`
  - `MORNING_SCAN_START_ET`, `MORNING_SCAN_END_ET`, `MAX_AUTO_TRADES_PER_DAY`, `ALLOW_DUPLICATE_SYMBOL_TRADES_PER_DAY`
  - `QUICK_PROFIT_TAKE_PCT`, `BREAKEVEN_TRIGGER_PCT`, `HARD_EXIT_TIME_ET`
  - `SCAN_MIN_PRICE`, `SCAN_MAX_PRICE`, `HARD_GATEKEEPER_ENABLED`
- Safety default: auto-trading is only enabled by default when `ALPACA_PAPER_BASE` points at Alpaca paper API. Use `LIVE_TRADING_OVERRIDE=1` plus explicit `AUTO_TRADE_ENABLED=1` to allow non-paper auto execution.
- The bot now auto-scans during the configured morning window and can auto-execute **paper** trades when a valid A/A+ setup, or active-mode WATCH fallback setup, passes execution validation.
- The UI is intentionally minimal. The bot is judged by execution diagnostics and paper-trade behavior, not by chart clutter.
- Kill switch flatten runs at `HARD_EXIT_TIME_ET` and position monitor runs in background every `POSITION_MONITOR_INTERVAL_SECONDS`.
