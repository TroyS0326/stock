from __future__ import annotations

from typing import Tuple

from models import SymbolMarketStats


DILUTION_BLACKLIST = set()


def hard_reject_reason(
    stats: SymbolMarketStats,
    min_price: float = 1.0,
    max_price: float = 3.0,
    min_daily_dollar_volume: float = 2_000_000,
    max_spread_pct: float = 0.015,
) -> str:
    if not (min_price <= stats.price <= max_price):
        return 'price_out_of_range'
    if stats.daily_dollar_volume < min_daily_dollar_volume:
        return 'insufficient_liquidity'
    if stats.spread_pct > max_spread_pct:
        return 'spread_too_wide'
    if stats.symbol in DILUTION_BLACKLIST:
        return 'dilution_blacklist'
    return ''


def passes_hard_gatekeeper(stats: SymbolMarketStats, **kwargs) -> Tuple[bool, str]:
    reason = hard_reject_reason(stats, **kwargs)
    return (reason == '', reason)
