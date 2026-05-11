from __future__ import annotations

from typing import Tuple

import config
from models import SymbolMarketStats


DILUTION_BLACKLIST = set()


def hard_reject_reason(
    stats: SymbolMarketStats,
    min_price: float | None = None,
    max_price: float | None = None,
    min_daily_dollar_volume: float | None = None,
    max_spread_pct: float | None = None,
) -> str:
    min_price = config.SCAN_MIN_PRICE if min_price is None else min_price
    max_price = config.SCAN_MAX_PRICE if max_price is None else max_price
    min_daily_dollar_volume = config.MIN_DAILY_DOLLAR_VOLUME if min_daily_dollar_volume is None else min_daily_dollar_volume
    max_spread_pct = config.MAX_SPREAD_PCT if max_spread_pct is None else max_spread_pct
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
