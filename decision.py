from __future__ import annotations

from datetime import datetime
from typing import Dict

from config import LUNCH_BLOCK_END, LUNCH_BLOCK_START


def time_bucket(time_et: datetime) -> str:
    minutes = time_et.hour * 60 + time_et.minute
    if 9 * 60 + 30 <= minutes <= 10 * 60 + 30:
        return 'morning'
    if 11 * 60 <= minutes <= 14 * 60:
        return 'midday'
    if 15 * 60 <= minutes <= 16 * 60:
        return 'power_hour'
    return 'other'


def is_lunch_block(time_et: datetime) -> bool:
    sh, sm = map(int, LUNCH_BLOCK_START.split(':'))
    eh, em = map(int, LUNCH_BLOCK_END.split(':'))
    start = time_et.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = time_et.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= time_et <= end


def regime_trade_decision(model_scores: Dict[str, int], time_et: datetime, relative_strength_vs_spy: float) -> str:
    if is_lunch_block(time_et):
        return 'WATCH'
    bucket = time_bucket(time_et)
    if bucket == 'morning':
        # Prioritize fast opening opportunities for quick flips.
        if model_scores['opportunity'] > 80:
            return 'BUY NOW'
        return 'WATCH FOR BREAKOUT'
    if bucket == 'midday':
        if model_scores['opportunity'] > 95 and model_scores['entry_quality'] > 90:
            return 'BUY NOW'
        return 'WATCH FOR BREAKOUT'
    if bucket == 'power_hour':
        if relative_strength_vs_spy > 2.0 and model_scores['tradability'] > 55:
            return 'BUY NOW'
        return 'WATCH FOR BREAKOUT'
    return 'WATCH FOR BREAKOUT'
