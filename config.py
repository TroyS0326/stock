import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env')

APP_TITLE = 'Veteran Day Trading Playbook Pro'
SECRET_KEY = os.getenv('SECRET_KEY', 'change-me')
DEBUG = os.getenv('FLASK_DEBUG', '0') == '1'
HOST = os.getenv('HOST', '0.0.0.0')
PORT = int(os.getenv('PORT', '5000'))

ALPACA_API_KEY = os.getenv('ALPACA_API_KEY', '').strip()
ALPACA_API_SECRET = os.getenv('ALPACA_API_SECRET', '').strip()
ALPACA_PAPER_BASE = os.getenv('ALPACA_PAPER_BASE', 'https://paper-api.alpaca.markets').rstrip('/')
ALPACA_DATA_BASE = os.getenv('ALPACA_DATA_BASE', 'https://data.alpaca.markets').rstrip('/')
ALPACA_FEED = os.getenv('ALPACA_FEED', 'iex').strip() or 'iex'
FINNHUB_API_KEY = os.getenv('FINNHUB_API_KEY', '').strip()
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '').strip()
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash').strip() or 'gemini-2.5-flash'

DB_PATH = str(BASE_DIR / 'veteran_trades.db')
SCAN_CANDIDATE_LIMIT = int(os.getenv('SCAN_CANDIDATE_LIMIT', '20'))
WATCHLIST_SIZE = int(os.getenv('WATCHLIST_SIZE', '3'))
MAX_BUY_SHARES = int(os.getenv('MAX_BUY_SHARES', '999'))
DEFAULT_RISK_CAPITAL = float(os.getenv('DEFAULT_RISK_CAPITAL', '300'))
CURRENT_BANKROLL = float(os.getenv('CURRENT_BANKROLL', '300.0'))

# --- Dynamic Risk Sizing Parameters (Replaces RISK_PCT_PER_TRADE) ---
RISK_PCT_PER_TRADE = 0.02
KELLY_FRACTION = float(os.getenv('KELLY_FRACTION', '0.25'))  # We will risk 25% of the mathematically optimal Full Kelly size
MAX_PORTFOLIO_HEAT = float(os.getenv('MAX_PORTFOLIO_HEAT', '0.06'))  # Hard cap single-trade risk at 6% of portfolio equity
VIX_PENALTY_MULTIPLIER = float(os.getenv('VIX_PENALTY_MULTIPLIER', '0.5'))  # Cut Kelly sizing in half if VIX circuit breaker triggers

# Kept as a fallback for any legacy references.
MAX_DOLLAR_LOSS_PER_TRADE = float(os.getenv('MAX_DOLLAR_LOSS_PER_TRADE', '10'))
MAX_FAILED_TRADES_PER_DAY = int(os.getenv('MAX_FAILED_TRADES_PER_DAY', '2'))
WATCHLIST_PUSH_SECONDS = float(os.getenv('WATCHLIST_PUSH_SECONDS', '4'))
ORDER_STATUS_POLL_SECONDS = float(os.getenv('ORDER_STATUS_POLL_SECONDS', '8'))
MIN_SCORE_TO_EXECUTE = int(os.getenv('MIN_SCORE_TO_EXECUTE', '25'))

# --- CALIBRATED ENGINE SETTINGS (LOOSENED FOR MORE ACTION) ---
MIN_CATALYST_SCORE = int(os.getenv('MIN_CATALYST_SCORE', '2'))
NO_BUY_BEFORE_ET = os.getenv('NO_BUY_BEFORE_ET', '09:45').strip() or '09:45'
OPENING_RANGE_START_ET = os.getenv('OPENING_RANGE_START_ET', '09:30').strip() or '09:30'
OPENING_RANGE_END_ET = os.getenv('OPENING_RANGE_END_ET', '09:45').strip() or '09:45'
MAX_SPREAD_PCT = float(os.getenv('MAX_SPREAD_PCT', '0.003'))

MAX_ENTRY_EXTENSION_PCT = float(os.getenv('MAX_ENTRY_EXTENSION_PCT', '0.01'))
OR_BREAKOUT_BUFFER_PCT = float(os.getenv('OR_BREAKOUT_BUFFER_PCT', '0.0015'))
PULLBACK_MAX_RETRACE_PCT = float(os.getenv('PULLBACK_MAX_RETRACE_PCT', '0.45'))
ENTRY_ORDER_TIMEOUT_SECONDS = float(os.getenv('ENTRY_ORDER_TIMEOUT_SECONDS', '30'))
ENTRY_LIMIT_PRICE_BUFFER_PCT = float(os.getenv('ENTRY_LIMIT_PRICE_BUFFER_PCT', '0.0015'))
ENTRY_ORDER_POLL_SECONDS = float(os.getenv('ENTRY_ORDER_POLL_SECONDS', '1'))
TARGET2_TRAILING_STOP_PCT = float(os.getenv('TARGET2_TRAILING_STOP_PCT', '5'))
MARKET_INTERNALS_BLOCK_ENABLED = os.getenv('MARKET_INTERNALS_BLOCK_ENABLED', '1') == '1'
MARKET_INTERNALS_TICK_SYMBOL = os.getenv('MARKET_INTERNALS_TICK_SYMBOL', 'TICK').strip().upper() or 'TICK'
MARKET_INTERNALS_ADD_SYMBOL = os.getenv('MARKET_INTERNALS_ADD_SYMBOL', 'ADD').strip().upper() or 'ADD'
CRYPTO_SCAN_ENABLED = os.getenv('CRYPTO_SCAN_ENABLED', '1') == '1'
CRYPTO_SYMBOLS = [s.strip().upper() for s in os.getenv('CRYPTO_SYMBOLS', 'BTC/USD,ETH/USD,SOL/USD,XRP/USD,DOGE/USD').split(',') if s.strip()]

# --- BROADER MARKET CAPS AND GAPS ---
MIN_PREMARKET_GAP_PCT = float(os.getenv('MIN_PREMARKET_GAP_PCT', '2.0'))
MIN_PREMARKET_DOLLAR_VOL = float(os.getenv('MIN_PREMARKET_DOLLAR_VOL', '2000000'))
MIN_SECTOR_SYMPATHY_SCORE = int(os.getenv('MIN_SECTOR_SYMPATHY_SCORE', '1'))
MIN_RVOL = float(os.getenv('MIN_RVOL', '1.5'))
MAX_FLOAT = int(os.getenv('MAX_FLOAT', '2000000000'))

A_PLUS_SCORE = int(os.getenv('A_PLUS_SCORE', '34'))
A_SCORE = int(os.getenv('A_SCORE', '30'))
TIMEZONE_LABEL = 'America/New_York'
LUNCH_BLOCK_START = os.getenv('LUNCH_BLOCK_START', '11:30').strip() or '11:30'
LUNCH_BLOCK_END = os.getenv('LUNCH_BLOCK_END', '13:00').strip() or '13:00'
VA_PERCENT = float(os.getenv('VA_PERCENT', '0.70'))
ATR_STOP_MULT = float(os.getenv('ATR_STOP_MULT', '2.0'))
RS_SECTOR_MULT = float(os.getenv('RS_SECTOR_MULT', '1.5'))
VIX_SYMBOL = os.getenv('VIX_SYMBOL', 'VIXY').strip().upper() or 'VIXY'
VIX_CIRCUIT_BREAKER_PCT = float(os.getenv('VIX_CIRCUIT_BREAKER_PCT', '5.0'))

PAPER_TRADING_DETECTED = 'paper-api.alpaca.markets' in ALPACA_PAPER_BASE
LIVE_TRADING_OVERRIDE = os.getenv('LIVE_TRADING_OVERRIDE', '0') == '1'
AUTO_START_EXECUTION_ENGINE = os.getenv('AUTO_START_EXECUTION_ENGINE', '1') == '1'
AUTO_TRADE_ENABLED = os.getenv('AUTO_TRADE_ENABLED', '1') == '1' if PAPER_TRADING_DETECTED else LIVE_TRADING_OVERRIDE and os.getenv('AUTO_TRADE_ENABLED', '0') == '1'
AUTO_SCAN_INTERVAL_SECONDS = int(os.getenv('AUTO_SCAN_INTERVAL_SECONDS', '45'))
POSITION_MONITOR_INTERVAL_SECONDS = int(os.getenv('POSITION_MONITOR_INTERVAL_SECONDS', '5'))
MORNING_SCAN_START_ET = os.getenv('MORNING_SCAN_START_ET', '09:35').strip() or '09:35'
MORNING_SCAN_END_ET = os.getenv('MORNING_SCAN_END_ET', '11:00').strip() or '11:00'

AUTO_SCAN_END_ET = os.getenv('AUTO_SCAN_END_ET', '15:15').strip() or '15:15'
ACTIVE_PAPER_TRADING_MODE = os.getenv('ACTIVE_PAPER_TRADING_MODE', '1') == '1'
MIN_AUTO_SETUP_GRADE = os.getenv('MIN_AUTO_SETUP_GRADE', 'WATCH').strip().upper() or 'WATCH'
ALLOW_WATCH_GRADE_AUTO_TRADES = os.getenv('ALLOW_WATCH_GRADE_AUTO_TRADES', '1') == '1'
AUTO_TRADE_CANDIDATE_LIMIT = int(os.getenv('AUTO_TRADE_CANDIDATE_LIMIT', '5'))
MAX_DAILY_REALIZED_LOSS_PCT = float(os.getenv('MAX_DAILY_REALIZED_LOSS_PCT', '0.10'))
MAX_TRADE_RISK_PCT = float(os.getenv('MAX_TRADE_RISK_PCT', '0.015'))
MIN_MOMENTUM_SCORE_TO_AUTOTRADE = int(os.getenv('MIN_MOMENTUM_SCORE_TO_AUTOTRADE', '24'))
FALLBACK_ENTRY_ENABLED = os.getenv('FALLBACK_ENTRY_ENABLED', '1') == '1'
FALLBACK_ENTRY_MAX_SPREAD_PCT = float(os.getenv('FALLBACK_ENTRY_MAX_SPREAD_PCT', '0.006'))
MAX_INTRADAY_POSITION_MINUTES = int(os.getenv('MAX_INTRADAY_POSITION_MINUTES', '90'))
MIN_DAILY_DOLLAR_VOLUME = float(os.getenv('MIN_DAILY_DOLLAR_VOLUME', '1000000'))
MAX_AUTO_TRADES_PER_DAY = int(os.getenv('MAX_AUTO_TRADES_PER_DAY', '2'))
ALLOW_DUPLICATE_SYMBOL_TRADES_PER_DAY = os.getenv('ALLOW_DUPLICATE_SYMBOL_TRADES_PER_DAY', '0') == '1'
QUICK_PROFIT_TAKE_PCT = float(os.getenv('QUICK_PROFIT_TAKE_PCT', '3.0'))
BREAKEVEN_TRIGGER_PCT = float(os.getenv('BREAKEVEN_TRIGGER_PCT', '1.5'))
HARD_EXIT_TIME_ET = os.getenv('HARD_EXIT_TIME_ET', '15:45').strip() or '15:45'
SCAN_MIN_PRICE = float(os.getenv('SCAN_MIN_PRICE', '1.00'))
SCAN_MAX_PRICE = float(os.getenv('SCAN_MAX_PRICE', '20.00'))
HARD_GATEKEEPER_ENABLED = os.getenv('HARD_GATEKEEPER_ENABLED', '1') == '1'


SIMULATION_MODE = os.getenv('SIMULATION_MODE', '0') == '1'
SIMULATED_ORDER_FILL_DELAY_SECONDS = float(os.getenv('SIMULATED_ORDER_FILL_DELAY_SECONDS', '1'))
SIMULATED_STARTING_CASH = float(os.getenv('SIMULATED_STARTING_CASH', '25000'))
SIMULATED_ALLOW_FRACTIONAL = os.getenv('SIMULATED_ALLOW_FRACTIONAL', '0') == '1'
SIMULATED_DEFAULT_SPREAD_PCT = float(os.getenv('SIMULATED_DEFAULT_SPREAD_PCT', '0.002'))
