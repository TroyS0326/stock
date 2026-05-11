"""Global pytest configuration for test-mode isolation.

This module is imported by pytest before test modules, so set environment
variables here to force deterministic non-live behavior during tests.
"""

import os

os.environ.setdefault("AUTO_START_EXECUTION_ENGINE", "0")
os.environ.setdefault("AUTO_TRADE_ENABLED", "1")
os.environ.setdefault("ACTIVE_PAPER_TRADING_MODE", "1")
os.environ.setdefault("SIMULATION_MODE", "1")
os.environ.setdefault("AUTO_CYCLE_REQUIRE_MARKET_OPEN", "0")
os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_API_SECRET", "test-secret")
os.environ.setdefault("ALPACA_PAPER_BASE", "https://paper-api.alpaca.markets")
