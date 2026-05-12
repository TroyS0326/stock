from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any, Dict, List, Tuple
from zoneinfo import ZoneInfo

import requests

from decision import regime_trade_decision
from filters import passes_hard_gatekeeper
from indicators import calc_rvol as indicators_calc_rvol, calc_spread_pct, calc_trend_efficiency as indicators_calc_trend_efficiency, calc_value_area
from models import ScoreTriplet, SymbolMarketStats, ComponentScores, WatchPanelDef, SymbolAnalysisResult
from setups import detect_orb
from utils import filter_bars_for_today_session, filter_bars_in_et_window, safe_num

from feature_store import store
from config import (
    ALPACA_API_KEY,
    ALPACA_API_SECRET,
    ALPACA_DATA_BASE,
    ALPACA_FEED,
    ALPACA_BARS_PAGE_LIMIT,
    ALPACA_BARS_MAX_PAGES,
    ALPACA_BARS_BATCH_SIZE,
    CURRENT_BANKROLL,
    DEFAULT_RISK_CAPITAL,
    FINNHUB_API_KEY,
    MAX_BUY_SHARES,
    MAX_FLOAT,
    MAX_ENTRY_EXTENSION_PCT,
    MAX_PORTFOLIO_HEAT,
    MAX_SPREAD_PCT,
    MARKET_INTERNALS_ADD_SYMBOL,
    MARKET_INTERNALS_BLOCK_ENABLED,
    MARKET_INTERNALS_TICK_SYMBOL,
    MIN_CATALYST_SCORE,
    MIN_PREMARKET_DOLLAR_VOL,
    MIN_RVOL,
    MIN_PREMARKET_GAP_PCT,
    MIN_SECTOR_SYMPATHY_SCORE,
    A_PLUS_SCORE,
    A_SCORE,
    ATR_STOP_MULT,
    RS_SECTOR_MULT,
    MIN_SCORE_TO_EXECUTE,
    NO_BUY_BEFORE_ET,
    OPENING_RANGE_END_ET,
    OPENING_RANGE_START_ET,
    OR_BREAKOUT_BUFFER_PCT,
    PULLBACK_MAX_RETRACE_PCT,
    KELLY_FRACTION,
    SCAN_CANDIDATE_LIMIT,
    TIMEZONE_LABEL,
    VA_PERCENT,
    VIX_CIRCUIT_BREAKER_PCT,
    VIX_PENALTY_MULTIPLIER,
    VIX_SYMBOL,
    WATCHLIST_SIZE,
    MORNING_SCAN_START_ET, AUTO_SCAN_END_ET, SCAN_MIN_PRICE, SCAN_MAX_PRICE, HARD_GATEKEEPER_ENABLED,
    ACTIVE_PAPER_TRADING_MODE, MAX_DOLLAR_LOSS_PER_TRADE, MAX_TRADE_RISK_PCT, MIN_DAILY_DOLLAR_VOLUME,
    ALPACA_PAPER_BASE, BROAD_UNIVERSE_SCAN_ENABLED, BROAD_UNIVERSE_CACHE_TTL_MINUTES, BROAD_UNIVERSE_MAX_SYMBOLS,
    BROAD_SNAPSHOT_BATCH_SIZE, BROAD_SCAN_TOP_N, DEEP_ANALYSIS_TOP_N, MIN_BROAD_PRICE, MAX_BROAD_PRICE,
    MIN_BROAD_DOLLAR_VOLUME, MIN_BROAD_INTRADAY_CHANGE_PCT, MAX_BROAD_SPREAD_PCT, BROAD_INCLUDE_ETFS,
    BROAD_INCLUDE_DOWN_MOVERS,
)
TIMEOUT = 20
HIGH_GAP_THRESHOLD_PCT = 20.0
HIGH_GAP_MIN_PREMARKET_DOLLAR_VOL = 5_000_000
VETERAN_BLACKLIST = {
    'NVD', 'NVDL', 'NVDX', 'NVDQ', 'TQQQ', 'SQQQ', 'QLD', 'QID', 'SOXL', 'SOXS',
    'UPRO', 'SPXU', 'SPXL', 'SPXS', 'UVXY', 'VIXY', 'SVIX', 'BOIL', 'KOLD', 'UCO',
    'SCO', 'YINN', 'YANG', 'JNUG', 'JDST', 'FAS', 'FAZ'
}
_BROAD_UNIVERSE_CACHE: Dict[str, Any] = {'symbols': [], 'expires_at': None}
_LAST_BROAD_SCAN_DIAGNOSTICS: Dict[str, Any] = {}
_LAST_BARS_FETCH_DIAGNOSTICS: Dict[str, Any] = {}


def _is_garbage_symbol(symbol: str) -> bool:
    s = (symbol or '').upper().strip()
    if not s:
        return True
    return any(token in s for token in ('WARRANT', ' RIGHTS', ' UNIT', '.W', '/W', '+W'))


def get_last_scan_diagnostics() -> dict:
    return dict(_LAST_BROAD_SCAN_DIAGNOSTICS)


class ScanError(Exception):
    pass


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_et() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE_LABEL))


def parse_hhmm(value: str) -> Tuple[int, int]:
    hh, mm = [int(x) for x in value.split(':', 1)]
    return hh, mm


def buy_window_open() -> bool:
    hh, mm = parse_hhmm(NO_BUY_BEFORE_ET)
    start = now_et().replace(hour=hh, minute=mm, second=0, microsecond=0)
    return now_et() >= start



def within_auto_scan_window() -> bool:
    now = now_et()
    sh, sm = parse_hhmm(MORNING_SCAN_START_ET)
    eh, em = parse_hhmm(AUTO_SCAN_END_ET)
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= now <= end and now.weekday() < 5

def within_morning_scan_window() -> bool:
    return within_auto_scan_window()

def _alpaca_headers() -> Dict[str, str]:
    if not ALPACA_API_KEY or not ALPACA_API_SECRET:
        raise ScanError('Missing Alpaca API credentials. Put ALPACA_API_KEY and ALPACA_API_SECRET in .env')
    return {
        'accept': 'application/json',
        'APCA-API-KEY-ID': ALPACA_API_KEY,
        'APCA-API-SECRET-KEY': ALPACA_API_SECRET,
    }


def _get_json(url: str, params: Dict[str, Any] | None = None, headers: Dict[str, str] | None = None) -> Any:
    resp = requests.get(url, params=params or {}, headers=headers or {}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def bar_dt_et(bar: Dict[str, Any]) -> datetime | None:
    ts = bar.get('t', '')
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace('Z', '+00:00')).astimezone(ZoneInfo(TIMEZONE_LABEL))
    except Exception:
        return None


def get_market_candidates(limit: int = SCAN_CANDIDATE_LIMIT) -> List[str]:
    headers = _alpaca_headers()
    candidates: List[str] = []
    endpoints = (
        '/v1beta1/screener/stocks/most-actives',
        '/v1beta1/screener/stocks/movers',
    )
    for endpoint in endpoints:
        try:
            data = _get_json(f'{ALPACA_DATA_BASE}{endpoint}', params={'top': limit}, headers=headers)
        except requests.RequestException:
            continue
        if isinstance(data, dict):
            for key in ('most_actives', 'gainers', 'data'):
                items = data.get(key) or []
                if isinstance(items, list):
                    for item in items:
                        symbol = (item.get('symbol') or '').upper()
                        if symbol and symbol.isalpha() and len(symbol) <= 5 and symbol not in VETERAN_BLACKLIST:
                            candidates.append(symbol)
    deduped, seen = [], set()
    for symbol in candidates:
        if symbol not in seen:
            seen.add(symbol)
            deduped.append(symbol)
    if 'SPY' not in seen:
        deduped.append('SPY')
    return deduped[: max(limit, 8)]



def _extract_symbols(items: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for item in items:
        symbol = str(item.get('symbol') or '').upper().strip()
        if symbol and symbol.isalpha() and len(symbol) <= 5 and symbol not in VETERAN_BLACKLIST:
            out.append(symbol)
    return out


def get_alpaca_movers(limit: int = SCAN_CANDIDATE_LIMIT) -> List[str]:
    try:
        data = _get_json(f'{ALPACA_DATA_BASE}/v1beta1/screener/stocks/movers', params={'top': limit}, headers=_alpaca_headers())
    except requests.RequestException:
        return []
    return _extract_symbols(data.get('gainers', []) if isinstance(data, dict) else [])


def get_premarket_leaders(limit: int = SCAN_CANDIDATE_LIMIT) -> List[str]:
    try:
        data = _get_json(f'{ALPACA_DATA_BASE}/v1beta1/screener/stocks/most-actives', params={'top': limit}, headers=_alpaca_headers())
    except requests.RequestException:
        return []
    return _extract_symbols(data.get('most_actives', []) if isinstance(data, dict) else [])


def get_unusual_relvol(limit: int = SCAN_CANDIDATE_LIMIT) -> List[str]:
    try:
        data = _get_json(f'{ALPACA_DATA_BASE}/v1beta1/screener/stocks/movers', params={'top': limit}, headers=_alpaca_headers())
    except requests.RequestException:
        return []
    return _extract_symbols(data.get('gainers', []) if isinstance(data, dict) else [])


def get_news_catalyst_list(candidates: List[str], per_symbol: int = 1) -> List[str]:
    out: List[str] = []
    for symbol in candidates[: max(6, min(len(candidates), SCAN_CANDIDATE_LIMIT))]:
        headlines = get_company_news(symbol, lookback_days=1)
        if len(headlines) >= per_symbol:
            out.append(symbol)
    return out


def _chunks(items: List[str], size: int) -> List[List[str]]:
    return [items[i:i + size] for i in range(0, len(items), max(1, size))]


def _get_broad_universe_symbols() -> List[str]:
    now = now_utc()
    cached = _BROAD_UNIVERSE_CACHE.get('symbols') or []
    expires_at = _BROAD_UNIVERSE_CACHE.get('expires_at')
    if cached and isinstance(expires_at, datetime) and now < expires_at:
        return cached[:BROAD_UNIVERSE_MAX_SYMBOLS]

    assets = _get_json(
        f'{ALPACA_PAPER_BASE}/v2/assets',
        params={'status': 'active', 'asset_class': 'us_equity'},
        headers=_alpaca_headers(),
    )
    symbols: List[str] = []
    for asset in assets if isinstance(assets, list) else []:
        if not asset.get('tradable', False):
            continue
        if not BROAD_INCLUDE_ETFS and str(asset.get('exchange', '')).upper() == 'ARCA':
            continue
        sym = str(asset.get('symbol', '')).upper().strip()
        name = str(asset.get('name', '')).upper()
        asset_name = str(asset.get('name', '')).upper()
        easy_to_borrow = bool(asset.get('easy_to_borrow', True))
        marginable = bool(asset.get('marginable', True))
        if _is_garbage_symbol(sym) or any(k in asset_name for k in ('WARRANT', 'RIGHT', 'UNIT', 'PREF', 'PREFERRED', 'ADR', 'DR', '3X', '2X', 'ULTRA', 'INVERSE', 'LEVERAGED', 'TRUST', 'FUND')):
            continue
        if not easy_to_borrow or not marginable:
            continue
        if sym and sym.isalpha() and len(sym) <= 5 and sym not in VETERAN_BLACKLIST:
            symbols.append(sym)

    deduped = list(dict.fromkeys(symbols))[:BROAD_UNIVERSE_MAX_SYMBOLS]
    _BROAD_UNIVERSE_CACHE['symbols'] = deduped
    _BROAD_UNIVERSE_CACHE['expires_at'] = now + timedelta(minutes=max(1, BROAD_UNIVERSE_CACHE_TTL_MINUTES))
    return deduped


def _get_snapshots_batched(symbols: List[str]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for batch in _chunks(symbols, BROAD_SNAPSHOT_BATCH_SIZE):
        try:
            merged.update(get_snapshots(batch))
        except Exception:
            continue
    return merged


def _get_quotes_batched(symbols: List[str]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for batch in _chunks(symbols, BROAD_SNAPSHOT_BATCH_SIZE):
        try:
            merged.update(get_latest_quotes(batch))
        except Exception:
            continue
    return merged


def get_refined_universe(limit: int = SCAN_CANDIDATE_LIMIT) -> Tuple[List[str], List[Dict[str, Any]]]:
    if BROAD_UNIVERSE_SCAN_ENABLED:
        return _get_refined_universe_broad(limit)

    candidates = set()
    candidates.update(get_alpaca_movers(limit))
    candidates.update(get_premarket_leaders(limit))
    candidates.update(get_unusual_relvol(limit))
    candidates.update(get_news_catalyst_list(list(candidates) or get_market_candidates(limit)))

    if 'SPY' not in candidates:
        candidates.add('SPY')

    snapshots = get_snapshots(list(candidates))
    quotes = get_latest_quotes(list(candidates))

    valid: List[str] = []
    rejected: List[Dict[str, Any]] = []
    for symbol in candidates:
        snap = snapshots.get(symbol, {})
        quote = quotes.get(symbol, {})
        daily = snap.get('dailyBar', {})
        minute = snap.get('minuteBar', {})
        prev = snap.get('prevDailyBar', {})
        price = safe_num(quote.get('ap')) or safe_num(minute.get('c')) or safe_num(daily.get('c')) or safe_num(prev.get('c'))

        # FIX 1: Tighten universe to low-priced names capped at $3.00
        if symbol != 'SPY' and not (SCAN_MIN_PRICE <= price <= SCAN_MAX_PRICE):
            rejected.append({'symbol': symbol, 'price': round(price, 4) if price else None, 'hard_reject_reasons': ['below_min_price' if price < SCAN_MIN_PRICE else 'above_max_price'], 'soft_warning_reasons': [], 'why_not_buying': ['outside_scan_price_range']})
            continue

        day_vol = safe_num(daily.get('v')) or safe_num(prev.get('v'))
        dollar_volume = day_vol * max(price, 0)

        # TEMPORARY: Lowering volume requirement slightly so we definitely get symbols
        if symbol != 'SPY' and dollar_volume < MIN_DAILY_DOLLAR_VOLUME:
            rejected.append({'symbol': symbol, 'price': round(price, 4) if price else None, 'hard_reject_reasons': ['low_daily_dollar_volume'], 'soft_warning_reasons': [], 'why_not_buying': ['low_daily_dollar_volume']})
            continue

        bid = safe_num(quote.get('bp'))
        ask = safe_num(quote.get('ap'))
        spread_pct = calc_spread_pct(bid, ask, price)

        if symbol != 'SPY':
            market_stats = SymbolMarketStats(symbol=symbol, price=price, daily_dollar_volume=dollar_volume, spread_pct=spread_pct)
            keep, reasons = passes_hard_gatekeeper(market_stats)
            if HARD_GATEKEEPER_ENABLED and not keep:
                reason_list = reasons if isinstance(reasons, list) else ([reasons] if reasons else [])
                rejected.append({'symbol': symbol, 'price': round(price, 4) if price else None, 'hard_reject_reasons': ['hard_gatekeeper_failed'] + [f'gatekeeper_{r}' for r in reason_list], 'soft_warning_reasons': ['spread_too_wide'] if spread_pct > MAX_SPREAD_PCT else [], 'why_not_buying': ['hard_gatekeeper_failed'] + reason_list})
                continue
        valid.append(symbol)

    if 'SPY' not in valid:
        valid.append('SPY')
    return valid[: max(limit, 12)], rejected




def dedupe_preserve_order(symbols: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for symbol in symbols:
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        ordered.append(symbol)
    return ordered
def _get_refined_universe_broad(limit: int = SCAN_CANDIDATE_LIMIT) -> Tuple[List[str], List[Dict[str, Any]]]:
    global _LAST_BROAD_SCAN_DIAGNOSTICS
    fallback_seed = dedupe_preserve_order(
        get_alpaca_movers(limit)
        + get_premarket_leaders(limit)
        + get_unusual_relvol(limit)
    )
    fallback_candidates = dedupe_preserve_order(
        fallback_seed + get_news_catalyst_list(fallback_seed or get_market_candidates(limit))
    )

    broad_symbols = _get_broad_universe_symbols()
    pulled_count = len(broad_symbols)
    if not broad_symbols:
        broad_symbols = list(fallback_candidates)
    snapshots = _get_snapshots_batched(broad_symbols)
    quotes = _get_quotes_batched(broad_symbols)

    ranked: List[Tuple[float, str]] = []
    rejected: List[Dict[str, Any]] = []
    for symbol in broad_symbols:
        snap = snapshots.get(symbol, {})
        quote = quotes.get(symbol, {})
        daily = snap.get('dailyBar', {})
        minute = snap.get('minuteBar', {})
        prev = snap.get('prevDailyBar', {})
        price = safe_num(quote.get('ap')) or safe_num(minute.get('c')) or safe_num(daily.get('c')) or safe_num(prev.get('c'))
        if price <= 0 or not (MIN_BROAD_PRICE <= price <= MAX_BROAD_PRICE):
            continue
        day_vol = safe_num(daily.get('v')) or safe_num(prev.get('v'))
        dollar_volume = day_vol * price
        if dollar_volume < MIN_BROAD_DOLLAR_VOLUME:
            continue
        prev_close = safe_num(prev.get('c'))
        intraday_change_pct = ((price - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0
        if BROAD_INCLUDE_DOWN_MOVERS:
            change_ok = abs(intraday_change_pct) >= MIN_BROAD_INTRADAY_CHANGE_PCT
        else:
            change_ok = intraday_change_pct >= MIN_BROAD_INTRADAY_CHANGE_PCT
        if not change_ok:
            continue
        bid = safe_num(quote.get('bp'))
        ask = safe_num(quote.get('ap'))
        spread_pct = calc_spread_pct(bid, ask, price)
        if spread_pct > MAX_BROAD_SPREAD_PCT:
            rejected.append({'symbol': symbol, 'price': round(price, 4), 'hard_reject_reasons': ['broad_spread_too_wide'], 'soft_warning_reasons': [], 'why_not_buying': ['broad_spread_too_wide']})
            continue
        rank_score = intraday_change_pct * 0.5 + (dollar_volume / 1_000_000.0)
        ranked.append((rank_score, symbol))

    max_candidates = max(limit, DEEP_ANALYSIS_TOP_N, 12)
    ranked_symbols = [s for _, s in sorted(ranked, reverse=True)[:BROAD_SCAN_TOP_N]]
    ordered_candidates = dedupe_preserve_order(
        ranked_symbols[:max(DEEP_ANALYSIS_TOP_N, limit)]
        + list(fallback_candidates)
        + ['SPY']
    )
    deep_candidates = ordered_candidates[:max_candidates]
    ranked_candidate_pool = ranked_symbols[:BROAD_SCAN_TOP_N]
    deep_analysis_target = max(limit, DEEP_ANALYSIS_TOP_N, 12)
    _LAST_BROAD_SCAN_DIAGNOSTICS = {
        # Stable fields
        'broad_universe_enabled': True,
        'broad_universe_count': pulled_count,
        'broad_candidates_ranked': len(ranked_symbols),
        'ranked_candidate_pool': ranked_candidate_pool,
        'ranked_candidate_pool_count': len(ranked_candidate_pool),
        'deep_analysis_target': deep_analysis_target,
        'deep_analysis_requested_count': len([s for s in deep_candidates if s != 'SPY']),
        'deep_backfill_used': False,
        'deep_backfill_chunks': 0,
        'candidate_pool_exhausted': False,
        'fallback_used': pulled_count == 0,
        'broad_scan_errors': [],
        # Legacy compatibility fields
        'broad_pulled_count': pulled_count,
        'broad_ranked_count': len(ranked_symbols),
        'deep_analysis_count': len([s for s in deep_candidates if s != 'SPY']),
    }
    return deep_candidates, rejected
def get_snapshots(symbols: List[str]) -> Dict[str, Any]:
    data = _get_json(
        f'{ALPACA_DATA_BASE}/v2/stocks/snapshots',
        params={'symbols': ','.join(symbols), 'feed': ALPACA_FEED},
        headers=_alpaca_headers(),
    )
    return data.get('snapshots', data)


def get_latest_quotes(symbols: List[str]) -> Dict[str, Any]:
    data = _get_json(
        f'{ALPACA_DATA_BASE}/v2/stocks/quotes/latest',
        params={'symbols': ','.join(symbols), 'feed': ALPACA_FEED},
        headers=_alpaca_headers(),
    )
    return data.get('quotes', data)


def get_bars(symbols: List[str], timeframe: str, start: datetime, end: datetime, limit: int) -> Dict[str, List[Dict[str, Any]]]:
    global _LAST_BARS_FETCH_DIAGNOSTICS

    if not symbols:
        _LAST_BARS_FETCH_DIAGNOSTICS[timeframe] = {
            'requested_symbol_count': 0,
            'requested_symbols_sample': [],
            'chunks_count': 0,
            'pages_fetched_total': 0,
            'alpaca_symbols_returned_count': 0,
            'symbols_with_bars_count': 0,
            'symbols_with_zero_bars_count': 0,
            'symbols_with_zero_bars_sample': [],
            'bars_returned_total_before_cap': 0,
            'bars_returned_total_after_cap': 0,
            'page_limit': max(1, ALPACA_BARS_PAGE_LIMIT),
            'max_pages': max(1, ALPACA_BARS_MAX_PAGES),
            'bars_fetch_errors': [],
            'bars_failed_batches': 0,
            'bars_failed_pages': 0,
            'bars_pages_truncated': 0,
        }
        return {}

    merged: Dict[str, List[Dict[str, Any]]] = {}
    chunk_size = max(1, ALPACA_BARS_BATCH_SIZE)
    page_limit = max(1, ALPACA_BARS_PAGE_LIMIT)
    max_pages = max(1, ALPACA_BARS_MAX_PAGES)

    pages_fetched_total = 0
    api_symbols_returned: set[str] = set()
    bars_fetch_errors: list[str] = []
    bars_failed_batches = 0
    bars_failed_pages = 0
    bars_pages_truncated = 0

    for chunk in _chunks(symbols, chunk_size):
        next_page_token: str | None = None
        pages_fetched = 0
        chunk_failed = False

        while pages_fetched < max_pages:
            params: Dict[str, Any] = {
                'symbols': ','.join(chunk),
                'timeframe': timeframe,
                'start': start.isoformat(),
                'end': end.isoformat(),
                'limit': page_limit,
                'adjustment': 'split',
                'feed': ALPACA_FEED,
            }
            if next_page_token:
                params['page_token'] = next_page_token

            try:
                data = _get_json(
                    f'{ALPACA_DATA_BASE}/v2/stocks/bars',
                    params=params,
                    headers=_alpaca_headers(),
                )
            except Exception as exc:
                bars_failed_pages += 1
                bars_fetch_errors.append(f"timeframe={timeframe} chunk={','.join(chunk)} page={pages_fetched + 1}: {exc}")
                chunk_failed = True
                break

            bars_page = data.get('bars', {}) if isinstance(data, dict) else {}
            for symbol, bars in bars_page.items():
                api_symbols_returned.add(symbol)
                if symbol not in merged:
                    merged[symbol] = []
                if isinstance(bars, list) and bars:
                    merged[symbol].extend(bars)

            pages_fetched += 1
            pages_fetched_total += 1
            next_page_token = data.get('next_page_token') if isinstance(data, dict) else None
            if not next_page_token:
                break

        if chunk_failed:
            bars_failed_batches += 1
            continue
        if pages_fetched >= max_pages and next_page_token:
            bars_pages_truncated += 1

    bars_before_cap = sum(len(v) for v in merged.values())
    per_symbol_cap = max(1, limit)
    for symbol, bars in merged.items():
        if len(bars) > per_symbol_cap:
            merged[symbol] = bars[-per_symbol_cap:]
    bars_after_cap = sum(len(v) for v in merged.values())

    requested_set = set(symbols)
    symbols_with_bars = sorted([s for s in symbols if len(merged.get(s, [])) > 0])
    zero_bars_symbols = sorted(list(requested_set - set(symbols_with_bars)))
    _LAST_BARS_FETCH_DIAGNOSTICS[timeframe] = {
        'requested_symbol_count': len(symbols),
        'requested_symbols_sample': symbols[:25],
        'chunks_count': len(_chunks(symbols, chunk_size)),
        'pages_fetched_total': pages_fetched_total,
        'alpaca_symbols_returned_count': len(api_symbols_returned),
        'alpaca_symbols_returned_sample': sorted(list(api_symbols_returned))[:25],
        'symbols_with_bars_count': len(symbols_with_bars),
        'symbols_with_zero_bars_count': len(zero_bars_symbols),
        'symbols_with_zero_bars_sample': zero_bars_symbols[:25],
        'bars_returned_total_before_cap': bars_before_cap,
        'bars_returned_total_after_cap': bars_after_cap,
        'page_limit': page_limit,
        'max_pages': max_pages,
        'bars_fetch_errors': bars_fetch_errors[:100],
        'bars_failed_batches': bars_failed_batches,
        'bars_failed_pages': bars_failed_pages,
        'bars_pages_truncated': bars_pages_truncated,
    }

    return merged


def get_vix_change() -> float:
    """Calculates the 1-hour percentage change for VIXY proxy volatility."""
    end = now_utc()
    start = end - timedelta(hours=1)
    try:
        bars = get_bars([VIX_SYMBOL], '1Min', start, end, 60).get(VIX_SYMBOL, [])
    except Exception:
        return 0.0
    if len(bars) < 2:
        return 0.0
    start_price = safe_num(bars[0].get('c'))
    curr_price = safe_num(bars[-1].get('c'))
    return ((curr_price - start_price) / start_price * 100.0) if start_price > 0 else 0.0


def check_vix_circuit_breaker() -> bool:
    """Return True when VIX proxy volatility spikes beyond configured threshold."""
    return get_vix_change() >= VIX_CIRCUIT_BREAKER_PCT


def has_positive_mtf_vwap_trend(minute_bars: List[Dict[str, Any]], chunk_size: int = 5) -> bool:
    session = filter_bars_for_today_session(minute_bars)
    if len(session) < chunk_size * 6:
        return False
    five_minute_blocks = [session[i:i + chunk_size] for i in range(0, len(session), chunk_size)]
    recent_blocks = [b for b in five_minute_blocks if len(b) == chunk_size][-6:]
    if len(recent_blocks) < 4:
        return False
    vwap_series = [calc_vwap(block) for block in recent_blocks]
    return all(vwap_series[i] >= vwap_series[i - 1] for i in range(1, len(vwap_series)))


def get_company_news(symbol: str, lookback_days: int = 3) -> List[Dict[str, Any]]:
    if not FINNHUB_API_KEY:
        return []
    today = datetime.utcnow().date()
    start = today - timedelta(days=lookback_days)
    try:
        payload = _get_json(
            'https://finnhub.io/api/v1/company-news',
            params={'symbol': symbol, 'from': start.isoformat(), 'to': today.isoformat(), 'token': FINNHUB_API_KEY},
        )
        return payload if isinstance(payload, list) else []
    except requests.RequestException:
        return []


def get_company_profile(symbol: str) -> Dict[str, Any]:
    if not FINNHUB_API_KEY:
        return {}
    try:
        payload = _get_json('https://finnhub.io/api/v1/stock/profile2', params={'symbol': symbol, 'token': FINNHUB_API_KEY})
        return payload if isinstance(payload, dict) else {}
    except requests.RequestException:
        return {}

def get_alpaca_asset(symbol: str) -> Dict[str, Any]:
    try:
        payload = _get_json(f'{ALPACA_DATA_BASE}/v2/assets/{symbol}', headers=_alpaca_headers())
        return payload if isinstance(payload, dict) else {}
    except requests.RequestException:
        return {}


def extract_float_shares(profile: Dict[str, Any], asset: Dict[str, Any]) -> float:
    float_candidates = (
        asset.get('float'),
        asset.get('shares_float'),
        asset.get('float_shares'),
        profile.get('floatShares'),
        profile.get('shareFloat'),
    )
    for raw in float_candidates:
        val = safe_num(raw)
        if val > 0:
            return val
    finnhub_float_millions = safe_num(profile.get('shareOutstanding'))
    if finnhub_float_millions > 0:
        return finnhub_float_millions * 1_000_000
    return 0.0


def calc_atr(bars: List[Dict[str, Any]], period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    true_ranges = []
    prev_close = safe_num(bars[0].get('c'))
    for bar in bars[1:]:
        high = safe_num(bar.get('h'))
        low = safe_num(bar.get('l'))
        close = safe_num(bar.get('c'))
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = close
    sample = true_ranges[-period:] if len(true_ranges) >= period else true_ranges
    return mean(sample) if sample else 0.0


def calc_vwap(minute_bars: List[Dict[str, Any]]) -> float:
    total_pv = 0.0
    total_v = 0.0
    for b in minute_bars:
        typical = (safe_num(b.get('h')) + safe_num(b.get('l')) + safe_num(b.get('c'))) / 3.0
        vol = safe_num(b.get('v'))
        total_pv += typical * vol
        total_v += vol
    return total_pv / total_v if total_v > 0 else 0.0
    
def calc_daily_volume_poc(minute_bars: List[Dict[str, Any]], min_tick: float = 0.01) -> float:
    session = filter_bars_for_today_session(minute_bars)
    if not session:
        return 0.0
    ladder: Dict[float, float] = {}
    tick = max(0.0001, min_tick)
    for bar in session:
        typical = (safe_num(bar.get('h')) + safe_num(bar.get('l')) + safe_num(bar.get('c'))) / 3.0
        vol = safe_num(bar.get('v'))
        if typical <= 0 or vol <= 0:
            continue
        px = round(round(typical / tick) * tick, 4)
        ladder[px] = ladder.get(px, 0.0) + vol
    if not ladder:
        return 0.0
    return max(ladder.items(), key=lambda kv: kv[1])[0]


def premarket_dollar_volume(minute_bars: List[Dict[str, Any]]) -> float:
    total = 0.0
    for b in minute_bars:
        dt = bar_dt_et(b)
        if not dt:
            continue
        mins = dt.hour * 60 + dt.minute
        if 4 * 60 <= mins < 9 * 60 + 30:
            total += safe_num(b.get('c')) * safe_num(b.get('v'))
    return total


def to_chart_bars(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for b in bars:
        ts = b.get('t')
        if not ts:
            continue
        try:
            epoch = int(datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp())
        except Exception:
            continue
        out.append({
            'time': epoch,
            'open': round(safe_num(b.get('o')), 4),
            'high': round(safe_num(b.get('h')), 4),
            'low': round(safe_num(b.get('l')), 4),
            'close': round(safe_num(b.get('c')), 4),
            'value': round(safe_num(b.get('v')), 2),
        })
    return out


def get_stock_chart_pack(symbol: str) -> Dict[str, Any]:
    end = now_utc()
    daily_start = end - timedelta(days=260)
    intraday_start = end - timedelta(days=3)
    daily = get_bars([symbol], '1Day', daily_start, end, 260).get(symbol, [])
    intraday = get_bars([symbol], '1Min', intraday_start, end, 1000).get(symbol, [])
    return {'symbol': symbol, 'daily': to_chart_bars(daily[-220:]), 'intraday': to_chart_bars(intraday[-390:])}


def score_float_liquidity(profile: Dict[str, Any], asset: Dict[str, Any], premarket_notional: float, day_volume: float, spread: float, atr: float, current_price: float) -> Tuple[int, Dict[str, Any]]:
    shares_out = safe_num(profile.get('shareOutstanding')) * 1_000_000
    float_shares = extract_float_shares(profile, asset)
    high_float_block = bool(float_shares and float_shares > MAX_FLOAT)
    float_proxy_ok = 10_000_000 <= shares_out <= 50_000_000 if shares_out > 0 else False
    spread_pct = spread / current_price if current_price > 0 else 1.0
    score = 1
    if premarket_notional >= 5_000_000 and spread_pct <= 0.0015 and atr > 0.25 and float_proxy_ok:
        score = 5
    elif premarket_notional >= 2_500_000 and spread_pct <= 0.0025 and atr > 0.18 and float_proxy_ok:
        score = 4
    elif premarket_notional >= 1_500_000 and spread_pct <= MAX_SPREAD_PCT and atr > 0.12:
        score = 3
    elif day_volume >= 1_000_000 and spread_pct <= 0.005:
        score = 2
    if high_float_block:
        score = 1
    return score, {
        'shares_outstanding_proxy': round(shares_out, 0) if shares_out else None,
        'float_shares': round(float_shares, 0) if float_shares else None,
        'high_float_block': high_float_block,
        'float_sweet_spot_proxy': float_proxy_ok,
        'premarket_dollar_volume': round(premarket_notional, 2),
        'spread': round(spread, 4),
        'spread_pct': round(spread_pct, 4),
        'atr': round(atr, 4),
        'wide_spread_block': spread_pct > MAX_SPREAD_PCT,
    }


def score_catalyst(symbol: str, price_change_pct: float) -> Tuple[int, Dict[str, Any]]:
    _ = price_change_pct
    ml_features = store.get_symbol_features(symbol)
    p_success = float(ml_features.get('p_success', 0.0) or 0.0)
    sentiment = float(ml_features.get('finbert_sentiment', 0.0) or 0.0)
    catalyst_score = max(1, min(5, int(round(p_success * 5))))

    return catalyst_score, {
        'used_ai': True,
        'model': 'FinBERT + XGBoost',
        'sentiment_score': sentiment,
        'p_success': p_success,
        'headline_count': int(ml_features.get('headline_count', 0) or 0),
        'hard_pass': p_success < 0.20,
        'catalyst_category_weight': catalyst_score,
        'direction': 'bullish' if sentiment >= 0 else 'mixed',
        'confidence': 'medium',
        'reason': 'Loaded from pre-market feature store.',
    }


SECTOR_ETF_MAP = {
    'technology': 'XLK',
    'semiconductors': 'SMH',
    'financial services': 'XLF',
    'banks': 'KBE',
    'healthcare': 'XLV',
    'biotechnology': 'XBI',
    'consumer defensive': 'XLP',
    'consumer cyclical': 'XLY',
    'communication services': 'XLC',
    'industrials': 'XLI',
    'energy': 'XLE',
    'utilities': 'XLU',
    'real estate': 'XLRE',
    'materials': 'XLB',
}


def classify_setup_grade(score_total: int, entry_trigger: str, hard_reject_reasons: List[str], component_scores: Dict[str, int], catalyst_score: int, spread_safe: bool, liquidity_score: int, qty: int) -> Tuple[str, str]:
    if hard_reject_reasons or not spread_safe or liquidity_score <= 1 or score_total < (A_SCORE - 10) or qty < 1:
        return 'NO TRADE', 'Hard reject, weak score, liquidity/spread risk, or qty below minimum.'
    trigger_valid = entry_trigger != 'NO_TRIGGER'
    momentum_stack = min(component_scores.get('premarket_gap_score', 1), component_scores.get('premarket_dollar_volume_score', 1), component_scores.get('relative_volume_score', 1), component_scores.get('opening_strength_score', 1))
    if trigger_valid and score_total >= A_PLUS_SCORE and liquidity_score >= 4 and catalyst_score >= 4 and momentum_stack >= 4:
        return 'A+', 'A+ via strong total score, valid trigger, strong liquidity/spread, and strong gap/volume/open strength.'
    if trigger_valid and score_total >= A_SCORE and liquidity_score >= 3 and spread_safe and qty >= 1 and catalyst_score >= MIN_CATALYST_SCORE:
        return 'A', 'A via strong score and any valid trigger (ORB/VWAP/momentum), with safe spread and executable sizing.'
    if score_total >= (A_SCORE - 6):
        return 'WATCH', 'Decent setup but not fully actionable yet (trigger, timing, or buy-zone condition pending).'
    return 'NO TRADE', 'Score and structure are not strong enough for watch/action.'
    
def required_premarket_volume_for_gap(premarket_gap_pct: float) -> float:
    return HIGH_GAP_MIN_PREMARKET_DOLLAR_VOL if premarket_gap_pct >= HIGH_GAP_THRESHOLD_PCT else MIN_PREMARKET_DOLLAR_VOL


def choose_sector_etf(profile: Dict[str, Any], symbol: str) -> str:
    text = ' '.join(str(profile.get(k, '')).lower() for k in ('finnhubIndustry', 'industry', 'name'))
    if any(k in symbol.upper() for k in ('ARM', 'NVDA', 'AMD', 'AVGO', 'MU', 'INTC')) or 'semiconductor' in text or 'chip' in text:
        return 'SMH'
    for key, etf in SECTOR_ETF_MAP.items():
        if key in text:
            return etf
    return 'SPY'


def score_sector_sympathy(symbol: str, symbol_change_pct: float, sector_symbol: str, sector_change_pct: float, catalyst_meta: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    edge = symbol_change_pct - sector_change_pct
    bullish = catalyst_meta.get('direction') not in {'bearish', 'mixed'}
    is_leader = symbol_change_pct >= (sector_change_pct * RS_SECTOR_MULT) if sector_change_pct > 0 else symbol_change_pct > 1.0
    score = 1
    if bullish and sector_change_pct > 0 and edge >= 4 and is_leader:
        score = 5
    elif bullish and sector_change_pct >= -0.2 and edge >= 2.5 and is_leader:
        score = 4
    elif edge >= 1.0:
        score = 3
    elif edge >= 0:
        score = 2
    return score, {
        'sector_symbol': sector_symbol,
        'sector_change_pct': round(sector_change_pct, 2),
        'edge_vs_sector_pct': round(edge, 2),
        'is_leader_vs_sector': is_leader,
    }


def score_daily_alignment(current_price: float, daily_bars: List[Dict[str, Any]]) -> Tuple[int, Dict[str, Any]]:
    highs20 = [safe_num(b.get('h')) for b in daily_bars[-20:]]
    closes200 = [safe_num(b.get('c')) for b in daily_bars[-200:]]
    highs60 = [safe_num(b.get('h')) for b in daily_bars[-60:]]
    ma200 = mean(closes200) if closes200 else current_price
    breakout_20 = max(highs20) if highs20 else current_price
    breakout_60 = max(highs60) if highs60 else current_price
    blue_sky = current_price >= breakout_20 * 0.995

    score = 1
    if blue_sky and current_price >= ma200 and current_price >= breakout_60 * 0.98:
        score = 5
    elif current_price >= ma200 and current_price >= breakout_20 * 0.985:
        score = 4
    elif current_price >= ma200:
        score = 3
    elif current_price >= ma200 * 0.97:
        score = 2
    return score, {'ma200': round(ma200, 2), 'breakout_20': round(breakout_20, 2), 'breakout_60': round(breakout_60, 2), 'blue_sky_proxy': blue_sky}


def get_opening_range_stats(minute_bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    session_bars = filter_bars_for_today_session(minute_bars)
    or_bars = filter_bars_in_et_window(session_bars, OPENING_RANGE_START_ET, OPENING_RANGE_END_ET)
    now_bar_count = len(session_bars)
    if not session_bars:
        return {
            'session_bars': 0,
            'or_complete': False,
            'or_high': None,
            'or_low': None,
            'or_open': None,
            'or_close': None,
            'or_mid': None,
            'or_range': None,
            'current_price': None,
            'breakout_price': None,
            'breakout_confirmed': False,
            'bars_above_breakout': 0,
        }

    if or_bars:
        or_high = max(safe_num(b.get('h')) for b in or_bars)
        or_low = min(safe_num(b.get('l')) for b in or_bars)
        or_open = safe_num(or_bars[0].get('o'))
        or_close = safe_num(or_bars[-1].get('c'))
        current_price = safe_num(session_bars[-1].get('c'))
        or_range = max(0.01, or_high - or_low)
        breakout_price = round(or_high * (1 + OR_BREAKOUT_BUFFER_PCT), 2)
        recent = session_bars[-3:]
        bars_above_breakout = sum(1 for b in recent if safe_num(b.get('c')) >= breakout_price)
        or_complete = buy_window_open() and len(or_bars) >= 5
        breakout_confirmed = or_complete and bars_above_breakout >= 2 and current_price >= breakout_price
        return {
            'session_bars': now_bar_count,
            'or_complete': or_complete,
            'or_high': round(or_high, 2),
            'or_low': round(or_low, 2),
            'or_open': round(or_open, 2),
            'or_close': round(or_close, 2),
            'or_mid': round((or_high + or_low) / 2, 2),
            'or_range': round(or_range, 2),
            'current_price': round(current_price, 2),
            'breakout_price': breakout_price,
            'breakout_confirmed': breakout_confirmed,
            'bars_above_breakout': bars_above_breakout,
        }

    current_price = safe_num(session_bars[-1].get('c'))
    return {
        'session_bars': now_bar_count,
        'or_complete': False,
        'or_high': None,
        'or_low': None,
        'or_open': None,
        'or_close': None,
        'or_mid': None,
        'or_range': None,
        'current_price': round(current_price, 2),
        'breakout_price': None,
        'breakout_confirmed': False,
        'bars_above_breakout': 0,
    }


def score_relative_strength_open(symbol_minute_bars: List[Dict[str, Any]], spy_minute_bars: List[Dict[str, Any]]) -> Tuple[int, Dict[str, Any]]:
    sym = filter_bars_for_today_session(symbol_minute_bars)
    spy = filter_bars_for_today_session(spy_minute_bars)
    if not sym or not spy:
        return 1, {'reason': 'Not enough opening session bars.'}
    sym_open = safe_num(sym[0].get('o')) or safe_num(sym[0].get('c'))
    sym_curr = safe_num(sym[-1].get('c'))
    spy_open = safe_num(spy[0].get('o')) or safe_num(spy[0].get('c'))
    spy_curr = safe_num(spy[-1].get('c'))
    sym_change = ((sym_curr - sym_open) / sym_open * 100.0) if sym_open else 0.0
    spy_change = ((spy_curr - spy_open) / spy_open * 100.0) if spy_open else 0.0
    edge = sym_change - spy_change
    score = 1
    if edge >= 3 and sym_change > 0:
        score = 5
    elif edge >= 2:
        score = 4
    elif edge >= 1:
        score = 3
    elif edge >= 0:
        score = 2
    return score, {
        'open_to_now_change_pct': round(sym_change, 2),
        'spy_open_to_now_change_pct': round(spy_change, 2),
        'edge': round(edge, 2),
    }

def detect_heavy_red_candle_trap(minute_bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    morning = filter_bars_in_et_window(filter_bars_for_today_session(minute_bars), OPENING_RANGE_START_ET, OPENING_RANGE_END_ET)
    if len(morning) < 2:
        return {'triggered': False, 'reason': 'Not enough opening bars to evaluate red-candle trap.'}
    green_vols = [safe_num(b.get('v')) for b in morning if safe_num(b.get('c')) > safe_num(b.get('o'))]
    if not green_vols:
        return {'triggered': False, 'reason': 'No green candles in opening range to compare against.'}
    max_green_vol = max(green_vols)
    heavy_red = []
    for idx, bar in enumerate(morning):
        open_px = safe_num(bar.get('o'))
        close_px = safe_num(bar.get('c'))
        vol = safe_num(bar.get('v'))
        if close_px < open_px and vol > max_green_vol:
            heavy_red.append((idx, bar, vol))
    if not heavy_red:
        return {
            'triggered': False,
            'max_green_volume': round(max_green_vol, 2),
            'reason': 'No heavy red candle exceeded the strongest green volume.',
        }
    first_idx, first_bar, first_vol = heavy_red[0]
    return {
        'triggered': True,
        'first_red_index': first_idx,
        'first_red_open': round(safe_num(first_bar.get('o')), 4),
        'first_red_close': round(safe_num(first_bar.get('c')), 4),
        'first_red_volume': round(first_vol, 2),
        'max_green_volume': round(max_green_vol, 2),
        'reason': 'Opening red candle volume exceeded all green candles in the opening range.',
    }



def get_market_internals_bias() -> Dict[str, Any]:
    meta = {
        'enabled': MARKET_INTERNALS_BLOCK_ENABLED,
        'tick_symbol': MARKET_INTERNALS_TICK_SYMBOL,
        'add_symbol': MARKET_INTERNALS_ADD_SYMBOL,
        'tick_persistently_negative': False,
        'add_dropping': False,
        'longs_blocked': False,
        'reason': '',
    }
    if not MARKET_INTERNALS_BLOCK_ENABLED:
        meta['reason'] = 'Market internals block disabled.'
        return meta
    end = now_utc()
    start = end - timedelta(minutes=30)
    try:
        bars = get_bars([MARKET_INTERNALS_TICK_SYMBOL, MARKET_INTERNALS_ADD_SYMBOL], '1Min', start, end, 60)
    except Exception as exc:
        meta['reason'] = f'Could not fetch internals: {exc}'
        return meta
    tick_series = [safe_num(b.get('c')) for b in bars.get(MARKET_INTERNALS_TICK_SYMBOL, []) if safe_num(b.get('c')) != 0]
    add_series = [safe_num(b.get('c')) for b in bars.get(MARKET_INTERNALS_ADD_SYMBOL, []) if safe_num(b.get('c')) != 0]
    if len(tick_series) >= 5:
        last5 = tick_series[-5:]
        meta['tick_persistently_negative'] = all(v < 0 for v in last5)
        meta['tick_last'] = round(last5[-1], 2)
    if len(add_series) >= 5:
        recent = add_series[-5:]
        meta['add_dropping'] = (recent[-1] < recent[0]) and all(recent[i] <= recent[i - 1] for i in range(1, len(recent)))
        meta['add_last'] = round(recent[-1], 2)
    meta['longs_blocked'] = bool(meta['tick_persistently_negative'] and meta['add_dropping'])
    if meta['longs_blocked']:
        meta['reason'] = 'Blocked: $TICK is persistently below 0 while $ADD is falling.'
    else:
        meta['reason'] = 'Breadth filter is not blocking longs.'
    return meta




def score_vwap_hold_reclaim(minute_bars: List[Dict[str, Any]]) -> Tuple[int, Dict[str, Any]]:
    session = filter_bars_for_today_session(minute_bars)
    if len(session) < 8:
        return 1, {'reason': 'Not enough session bars for VWAP check.'}
    vwap = calc_vwap(session)
    closes = [safe_num(b.get('c')) for b in session]
    last5 = closes[-5:]
    holds = sum(1 for c in last5 if c >= vwap)
    dipped_below = any(c < vwap * 0.998 for c in closes[:-3])
    reclaimed = all(c >= vwap * 0.999 for c in closes[-3:])
    recent_vol = [safe_num(b.get('v')) for b in session[-5:]]
    prior_vol = [safe_num(b.get('v')) for b in session[-12:-5]]
    drying = bool(prior_vol) and mean(recent_vol) <= mean(prior_vol) * 1.1
    score = 1
    if holds >= 4 and reclaimed and drying:
        score = 5
    elif holds >= 4 and reclaimed:
        score = 4
    elif holds >= 3:
        score = 3
    elif closes[-1] >= vwap * 0.997:
        score = 2
    return score, {
        'vwap': round(vwap, 2),
        'holds_last5': holds,
        'dipped_below_vwap': dipped_below,
        'reclaimed_vwap': reclaimed,
        'drying_volume': drying,
    }


def score_first_pullback_quality(minute_bars: List[Dict[str, Any]], or_stats: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    session = filter_bars_for_today_session(minute_bars)
    if len(session) < 10 or not or_stats.get('or_high'):
        return 1, {'reason': 'Not enough data for first pullback score.'}
    breakout_price = safe_num(or_stats.get('breakout_price') or or_stats.get('or_high'))
    vwap = calc_vwap(session)
    breakout_index = None
    for idx, bar in enumerate(session):
        if safe_num(bar.get('h')) >= breakout_price:
            breakout_index = idx
            break
    recent_slice = session[breakout_index:] if breakout_index is not None else session[-10:]
    high_after_break = max(safe_num(b.get('h')) for b in recent_slice)
    low_after_break = min(safe_num(b.get('l')) for b in recent_slice[-8:])
    pullback = max(0.0, high_after_break - low_after_break)
    or_range = max(0.01, safe_num(or_stats.get('or_range'), 0.01))
    retrace_pct = pullback / or_range
    low_holds_vwap = low_after_break >= vwap * 0.995
    vol_recent = [safe_num(b.get('v')) for b in recent_slice[-4:]]
    vol_prior = [safe_num(b.get('v')) for b in recent_slice[-8:-4]]
    drying = bool(vol_prior) and mean(vol_recent) <= mean(vol_prior) * 0.95
    score = 1
    if retrace_pct <= PULLBACK_MAX_RETRACE_PCT and low_holds_vwap and drying:
        score = 5
    elif retrace_pct <= 0.55 and low_holds_vwap:
        score = 4
    elif retrace_pct <= 0.7:
        score = 3
    elif low_holds_vwap:
        score = 2
    return score, {
        'high_after_breakout': round(high_after_break, 2),
        'low_after_breakout': round(low_after_break, 2),
        'pullback_retrace_pct_of_or': round(retrace_pct, 2),
        'low_holds_vwap': low_holds_vwap,
        'drying_volume': drying,
    }


def score_entry_quality(current_price: float, daily_bars: List[Dict[str, Any]], minute_bars: List[Dict[str, Any]], or_stats: Dict[str, Any], vwap_meta: Dict[str, Any], pullback_meta: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    recent_high = max([safe_num(b.get('h')) for b in daily_bars[-10:]] or [current_price])
    recent_low = min([safe_num(b.get('l')) for b in daily_bars[-10:]] or [current_price])
    atr = calc_atr(daily_bars)
    session = filter_bars_for_today_session(minute_bars)
    minute_highs = [safe_num(b.get('h')) for b in session[-15:]] or [current_price]
    minute_lows = [safe_num(b.get('l')) for b in session[-15:]] or [current_price]
    coil_high = max(minute_highs)
    coil_low = min(minute_lows)
    or_breakout = safe_num(or_stats.get('breakout_price')) or max(recent_high, coil_high)
    entry = max(recent_high, coil_high, or_breakout) + max(0.02, atr * 0.03)
    stop = max(recent_low, entry - max(0.05, atr * ATR_STOP_MULT))
    risk = max(0.01, entry - stop)
    target1 = entry + risk * 3
    target2 = entry + risk * 4
    rr2 = (target2 - entry) / risk if risk > 0 else 0.0
    distance = abs(current_price - entry) / entry if entry > 0 else 9.99
    contraction = (coil_high - coil_low) <= max(0.25, atr * 0.8)
    extended = current_price > entry * (1 + MAX_ENTRY_EXTENSION_PCT)
    breakout_confirmed = bool(or_stats.get('breakout_confirmed'))
    reclaim_ok = bool(vwap_meta.get('reclaimed_vwap'))
    pullback_ok = bool(pullback_meta.get('low_holds_vwap'))

    score = 1
    if rr2 >= 3 and distance <= 0.0075 and breakout_confirmed and reclaim_ok and pullback_ok and not extended:
        score = 5
    elif rr2 >= 3 and breakout_confirmed and reclaim_ok and not extended:
        score = 4
    elif rr2 >= 2.5 and reclaim_ok:
        score = 3
    elif rr2 >= 2:
        score = 2

    return score, {
        'entry_price': round(entry, 2),
        'stop_price': round(stop, 2),
        'target_1': round(target1, 2),
        'target_2': round(target2, 2),
        'risk_per_share': round(risk, 2),
        'rr_ratio_1': round((target1 - entry) / risk if risk > 0 else 0.0, 2),
        'rr_ratio_2': round(rr2, 2),
        'contraction_proxy': contraction,
        'extended': extended,
        'distance_from_entry_pct': round(distance * 100, 2),
        'breakout_confirmed': breakout_confirmed,
    }


def score_opening_range_confirmation(current_price: float, or_stats: Dict[str, Any], vwap_meta: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    if not or_stats.get('or_high'):
        return 1, {'reason': 'Opening range not formed yet.'}
    breakout_confirmed = bool(or_stats.get('breakout_confirmed'))
    above_mid = current_price >= safe_num(or_stats.get('or_mid'))
    above_breakout = current_price >= safe_num(or_stats.get('breakout_price'))
    holds_vwap = bool(vwap_meta.get('holds_last5', 0) >= 3)
    score = 1
    if breakout_confirmed and holds_vwap:
        score = 5
    elif above_breakout and holds_vwap:
        score = 4
    elif above_mid:
        score = 3
    elif current_price >= safe_num(or_stats.get('or_low')):
        score = 2
    return score, {
        'breakout_confirmed': breakout_confirmed,
        'above_breakout': above_breakout,
        'above_mid': above_mid,
        'bars_above_breakout': or_stats.get('bars_above_breakout', 0),
    }



def calculate_rvol(minute_bars: List[Dict[str, Any]], lookback_days: int = 3) -> float:
    if not minute_bars:
        return 0.0
    session = filter_bars_for_today_session(minute_bars)
    current_volume = sum(safe_num(b.get('v')) for b in session)
    if current_volume <= 0:
        return 0.0
    latest_dt = bar_dt_et(minute_bars[-1])
    if not latest_dt:
        return 0.0
    cutoff = latest_dt.hour * 60 + latest_dt.minute
    volumes_by_day: Dict[Any, float] = {}
    for b in minute_bars:
        dt = bar_dt_et(b)
        if not dt or dt.date() == latest_dt.date():
            continue
        mins = dt.hour * 60 + dt.minute
        if 9 * 60 + 30 <= mins <= cutoff:
            volumes_by_day[dt.date()] = volumes_by_day.get(dt.date(), 0.0) + safe_num(b.get('v'))
    hist = list(volumes_by_day.values())[-lookback_days:]
    avg = mean(hist) if hist else 0.0
    return (current_volume / avg) if avg > 0 else 0.0


def calculate_trend_efficiency(minute_bars: List[Dict[str, Any]], window: int = 30) -> float:
    session = filter_bars_for_today_session(minute_bars)
    closes = [safe_num(b.get('c')) for b in session[-window:] if safe_num(b.get('c')) > 0]
    if len(closes) < 3:
        return 0.0
    net_move = abs(closes[-1] - closes[0])
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    return (net_move / path) if path > 0 else 0.0


def calculate_halt_risk_probability(minute_bars: List[Dict[str, Any]], bars: int = 5) -> Dict[str, Any]:
    session = filter_bars_for_today_session(minute_bars)
    recent = session[-bars:]
    if not recent:
        return {'halt_risk': 'unknown', 'max_1m_range_pct': 0.0}
    max_range = 0.0
    for b in recent:
        h = safe_num(b.get('h'))
        l = safe_num(b.get('l'))
        if l > 0 and h >= l:
            max_range = max(max_range, (h - l) / l * 100.0)
    risk = 'high' if max_range > 8 else 'normal'
    return {'halt_risk': risk, 'max_1m_range_pct': round(max_range, 2)}


def build_model_scores(price_change_pct: float, rvol: float, float_shares: float, catalyst_weight: int, spread_pct: float, trend_efficiency: float, current_price: float, vwap: float, now_label: str) -> Dict[str, int]:
    gap_component = 100 if 8 <= price_change_pct <= 20 else (70 if price_change_pct > 5 else 40)
    rvol_component = min(100, int((rvol / max(0.1, MIN_RVOL)) * 100))
    float_component = 100 if 0 < float_shares <= 20_000_000 else (55 if float_shares <= MAX_FLOAT else 20)
    catalyst_component = catalyst_weight * 20
    opportunity = int(0.25 * catalyst_component + 0.20 * rvol_component + 0.15 * float_component + 0.15 * gap_component + 0.25 * 80)

    spread_component = 100 if spread_pct <= 0.003 else (70 if spread_pct <= 0.01 else 35)
    trend_component = min(100, int(trend_efficiency * 100))
    tradability = int(0.5 * spread_component + 0.5 * trend_component)

    extension = ((current_price - vwap) / vwap * 100.0) if vwap > 0 else 0.0
    extension_component = 100 if extension <= 1.5 else (70 if extension <= 3 else 30)
    tod_component = 95 if '09:' in now_label or '10:' in now_label else (45 if '12:' in now_label or '13:' in now_label else 70)
    entry_quality = int(0.6 * extension_component + 0.4 * tod_component)

    return {
        'opportunity': max(1, min(100, opportunity)),
        'tradability': max(1, min(100, tradability)),
        'entry_quality': max(1, min(100, entry_quality)),
    }


def get_trade_decision(model_scores: Dict[str, int], time_et: datetime, relative_strength_vs_spy: float) -> str:
    minutes = time_et.hour * 60 + time_et.minute
    if 9 * 60 + 30 <= minutes <= 10 * 60 + 30:
        if model_scores['opportunity'] > 80 and model_scores['tradability'] > 60:
            return 'BUY NOW'
    elif 11 * 60 <= minutes <= 14 * 60:
        if model_scores['opportunity'] > 95 and model_scores['entry_quality'] > 90:
            return 'BUY NOW'
        return 'WATCH FOR BREAKOUT'
    elif 15 * 60 <= minutes <= 16 * 60:
        if relative_strength_vs_spy > 2.0 and model_scores['tradability'] > 55:
            return 'BUY NOW'
    return 'WATCH FOR BREAKOUT'


def detect_entry_trigger_name(or_stats: Dict[str, Any], vwap_meta: Dict[str, Any], pullback_meta: Dict[str, Any], trend_efficiency: float) -> Tuple[str, str]:
    if bool(or_stats.get('breakout_confirmed')):
        return 'ORB_BREAKOUT', 'Opening range breakout confirmed with follow-through.'
    if bool(vwap_meta.get('reclaimed_vwap')):
        return 'VWAP_RECLAIM', 'Price reclaimed and held VWAP.'
    if bool(pullback_meta.get('low_holds_vwap')) and bool(vwap_meta.get('holds_last5', 0) >= 3):
        return 'VWAP_PULLBACK_BOUNCE', 'Pullback held near VWAP with bounce structure.'
    if trend_efficiency >= 0.55 and bool(vwap_meta.get('holds_last5', 0) >= 4):
        return 'MOMENTUM_CONTINUATION', 'Trend efficiency and VWAP hold support continuation.'
    return 'NO_TRIGGER', 'No clean ORB/VWAP/momentum trigger is confirmed.'


def calculate_position_size(
    entry_price: float,
    stop_price: float,
    target_price: float,
    p_success: float,
    vix_spike_active: bool,
) -> Dict[str, Any]:
    """Calculates position size using Fractional Kelly driven by ML win probability."""

    risk_per_share = entry_price - stop_price
    if risk_per_share <= 0:
        return {
            'qty': 0, 'capital_qty': 0, 'risk_qty': 0,
            'max_dollar_loss': 0.0, 'buying_power_used': 0.0,
            'dynamic_risk_limit': 0.0, 'kelly_fraction_used': 0.0,
            'reason': 'Invalid stop distance.',
        }
    reward_per_share = max(0.01, target_price - entry_price)
    reward_to_risk = reward_per_share / risk_per_share

    # 1. Full Kelly Formula: f = (P * R - (1 - P)) / R
    kelly_full = (p_success * reward_to_risk - (1.0 - p_success)) / reward_to_risk

    # If Kelly is <= 0, the mathematical expectancy is negative. Skip trade.
    if kelly_full <= 0:
        return {
            'qty': 0, 'capital_qty': 0, 'risk_qty': 0,
            'max_dollar_loss': 0.0, 'buying_power_used': 0.0,
            'dynamic_risk_limit': 0.0, 'kelly_fraction_used': 0.0,
            'reason': 'Negative mathematical expectancy.',
        }

    # 2. Fractional Kelly & Volatility Brakes
    current_k_fraction = KELLY_FRACTION
    if vix_spike_active:
        current_k_fraction *= VIX_PENALTY_MULTIPLIER  # Cut risk in volatile regimes

    # Calculate optimal risk percentage, capped by max portfolio heat
    fractional_kelly_pct = min(current_k_fraction * kelly_full, MAX_PORTFOLIO_HEAT)
    dynamic_dollar_risk = CURRENT_BANKROLL * fractional_kelly_pct
    configured_cap = min(MAX_DOLLAR_LOSS_PER_TRADE, CURRENT_BANKROLL * MAX_TRADE_RISK_PCT)
    # In active paper mode we allow the larger cap so tiny bankrolls still generate testable attempts.
    if ACTIVE_PAPER_TRADING_MODE:
        configured_cap = max(MAX_DOLLAR_LOSS_PER_TRADE, CURRENT_BANKROLL * MAX_TRADE_RISK_PCT)
    dynamic_dollar_risk = min(dynamic_dollar_risk, configured_cap)

    # 3. Share Quantity Calculation
    capital_qty = int(DEFAULT_RISK_CAPITAL // max(0.01, entry_price))
    risk_qty = int(dynamic_dollar_risk // risk_per_share)

    # We take the minimum of constraints to ensure capital limits are respected
    qty = max(0, min(MAX_BUY_SHARES, capital_qty, risk_qty))

    return {
        'qty': qty,
        'capital_qty': capital_qty,
        'risk_qty': risk_qty,
        'max_dollar_loss': round(qty * risk_per_share, 2),
        'buying_power_used': round(qty * entry_price, 2),
        'dynamic_risk_limit': round(dynamic_dollar_risk, 2),
        'kelly_fraction_used': round(fractional_kelly_pct, 4),
    }


def analyze_symbol(symbol: str, snapshot: Dict[str, Any], quote: Dict[str, Any], daily_bars: List[Dict[str, Any]], minute_bars: List[Dict[str, Any]], spy_change_pct: float, profile: Dict[str, Any], asset: Dict[str, Any], spy_minute_bars: List[Dict[str, Any]], sector_snapshots: Dict[str, Any], market_internals: Dict[str, Any]) -> Dict[str, Any]:
    daily_bar = snapshot.get('dailyBar', {})
    prev_daily = snapshot.get('prevDailyBar', {})
    minute_bar = snapshot.get('minuteBar', {})
    ask = safe_num(quote.get('ap'))
    bid = safe_num(quote.get('bp'))
    spread = max(0.0, ask - bid) if ask and bid else 0.0
    current_price = ask or safe_num(minute_bar.get('c')) or safe_num(daily_bar.get('c')) or safe_num(prev_daily.get('c'))
    prev_close = safe_num(prev_daily.get('c')) or safe_num(daily_bar.get('o')) or current_price
    day_volume = safe_num(daily_bar.get('v')) or safe_num(prev_daily.get('v'))
    price_change_pct = ((current_price - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0
    atr = calc_atr(daily_bars)
    premarket_notional = premarket_dollar_volume(minute_bars)
    premarket_gap_pct = price_change_pct
    required_premarket_notional = required_premarket_volume_for_gap(premarket_gap_pct)
    volume_poc = calc_daily_volume_poc(minute_bars, 0.01 if current_price >= 1 else 0.0001)
    va_metrics = calc_value_area(filter_bars_for_today_session(minute_bars), safe_num, VA_PERCENT)
    vah = safe_num(va_metrics.get('vah'))
    red_candle_trap = detect_heavy_red_candle_trap(minute_bars)
    mtf_aligned = has_positive_mtf_vwap_trend(minute_bars)
    vixy_change = get_vix_change()

    ml_features = store.get_symbol_features(symbol)
    p_success = float(ml_features.get('p_success', 0.0) or 0.0)
    sentiment = float(ml_features.get('finbert_sentiment', 0.0) or 0.0)
    catalyst_score = max(1, min(5, int(round(p_success * 5))))
    catalyst_meta = {
        'used_ai': True,
        'model': 'FinBERT + XGBoost',
        'sentiment_score': sentiment,
        'p_success': p_success,
        'headline_count': int(ml_features.get('headline_count', 0) or 0),
        'hard_pass': p_success < 0.20,
        'catalyst_category_weight': catalyst_score,
        'direction': 'bullish' if sentiment >= 0 else 'mixed',
        'confidence': 'medium',
        'reason': 'Loaded from pre-market feature store.',
    }
    liquidity_score, liquidity_meta = score_float_liquidity(profile, asset, premarket_notional, day_volume, spread, atr, current_price)
    daily_score, daily_meta = score_daily_alignment(current_price, daily_bars)
    sector_symbol = choose_sector_etf(profile, symbol)
    sector_snapshot = sector_snapshots.get(sector_symbol, {})
    sector_prev = safe_num(sector_snapshot.get('prevDailyBar', {}).get('c')) or 1
    sector_curr = safe_num(sector_snapshot.get('dailyBar', {}).get('c')) or safe_num(sector_snapshot.get('minuteBar', {}).get('c')) or sector_prev
    sector_change_pct = ((sector_curr - sector_prev) / sector_prev * 100.0) if sector_prev > 0 else 0.0
    sector_score, sector_meta = score_sector_sympathy(symbol, price_change_pct, sector_symbol, sector_change_pct, catalyst_meta)
    or_stats = get_opening_range_stats(minute_bars)
    orb_meta = detect_orb(minute_bars, OPENING_RANGE_START_ET, OPENING_RANGE_END_ET)
    open_rs_score, open_rs_meta = score_relative_strength_open(minute_bars, spy_minute_bars)
    vwap_score, vwap_meta = score_vwap_hold_reclaim(minute_bars)
    pullback_score, pullback_meta = score_first_pullback_quality(minute_bars, or_stats)
    entry_score, entry_meta = score_entry_quality(current_price, daily_bars, minute_bars, or_stats, vwap_meta, pullback_meta)
    confirm_score, confirm_meta = score_opening_range_confirmation(current_price, or_stats, vwap_meta)

    rvol = indicators_calc_rvol(minute_bars, filter_bars_for_today_session, bar_dt_et, safe_num)
    trend_efficiency = indicators_calc_trend_efficiency(minute_bars, filter_bars_for_today_session, safe_num)
    halt_risk = calculate_halt_risk_probability(minute_bars)
    rel_strength_vs_spy = open_rs_meta.get('edge', 0.0)
    # Strengthen morning-trade scoring with explicit day-trade components.
    if 2 <= premarket_gap_pct <= 20:
        gap_score = 5
    elif 1 <= premarket_gap_pct < 2 or 20 < premarket_gap_pct <= 35:
        gap_score = 3
    else:
        gap_score = 1
    if premarket_gap_pct > 25 and premarket_notional < required_premarket_notional:
        gap_score = max(1, gap_score - 2)

    vol_ratio = premarket_notional / max(1.0, MIN_PREMARKET_DOLLAR_VOL)
    premarket_dollar_vol_score = 5 if vol_ratio >= 4 else (4 if vol_ratio >= 2 else (3 if vol_ratio >= 1 else (2 if vol_ratio >= 0.7 else 1)))
    rvol_score = 5 if rvol >= 5 else (4 if rvol >= 3 else (3 if rvol >= MIN_RVOL else 2 if rvol >= MIN_RVOL * 0.75 else 1))
    opening_strength_score = 5 if rel_strength_vs_spy >= 3 else (4 if rel_strength_vs_spy >= 2 else (3 if rel_strength_vs_spy >= 1 else 2 if rel_strength_vs_spy >= 0 else 1))
    vwap_ext_pct = ((current_price - safe_num(vwap_meta.get('vwap'))) / max(0.01, safe_num(vwap_meta.get('vwap')))) * 100.0 if safe_num(vwap_meta.get('vwap')) else 0.0
    vwap_behavior_score = 5 if vwap_meta.get('reclaimed_vwap') else (4 if pullback_meta.get('low_holds_vwap') else (3 if vwap_meta.get('holds_last5', 0) >= 3 else 2))
    if vwap_ext_pct > (MAX_ENTRY_EXTENSION_PCT * 100.0 * 1.4):
        vwap_behavior_score = max(1, vwap_behavior_score - 2)
    opening_range_score = 5 if or_stats.get('breakout_confirmed') else (4 if confirm_meta.get('above_breakout') else (3 if confirm_meta.get('above_mid') else 2 if or_stats.get('or_complete') else 1))
    trend_score = 5 if trend_efficiency >= 0.65 else (4 if trend_efficiency >= 0.5 else (3 if trend_efficiency >= 0.4 else 2 if trend_efficiency >= 0.3 else 1))

    model_scores = build_model_scores(
        price_change_pct=premarket_gap_pct,
        rvol=rvol,
        float_shares=safe_num(liquidity_meta.get('float_shares')),
        catalyst_weight=int(catalyst_meta.get('catalyst_category_weight') or catalyst_score),
        spread_pct=safe_num(liquidity_meta.get('spread_pct')),
        trend_efficiency=trend_efficiency,
        current_price=current_price,
        vwap=safe_num(vwap_meta.get('vwap')),
        now_label=now_et().strftime('%H:%M'),
    )

    total = int(round(
        0.18 * (gap_score * 20)
        + 0.14 * (premarket_dollar_vol_score * 20)
        + 0.14 * (rvol_score * 20)
        + 0.12 * (opening_strength_score * 20)
        + 0.14 * (vwap_behavior_score * 20)
        + 0.10 * (opening_range_score * 20)
        + 0.08 * (liquidity_score * 20)
        + 0.10 * (trend_score * 20)
    ))
    buy_lower = entry_meta['entry_price']
    buy_upper = round(entry_meta['entry_price'] * (1 + MAX_ENTRY_EXTENSION_PCT), 2)
    p_success = catalyst_meta.get('p_success', 0.0)
    vix_spike_active = vixy_change >= VIX_CIRCUIT_BREAKER_PCT
    sizing = calculate_position_size(
        entry_price=entry_meta['entry_price'],
        stop_price=entry_meta['stop_price'],
        target_price=entry_meta['target_1'],
        p_success=p_success,
        vix_spike_active=vix_spike_active,
    )
    after_time_gate = buy_window_open()
    wait_state = not after_time_gate

    hard_reject_reasons, soft_warning_reasons = [], []
    if current_price < SCAN_MIN_PRICE: hard_reject_reasons.append('below_min_price')
    if current_price > SCAN_MAX_PRICE: hard_reject_reasons.append('above_max_price')
    if catalyst_score < MIN_CATALYST_SCORE or catalyst_meta.get('hard_pass'): hard_reject_reasons.append('missing_catalyst')
    if premarket_gap_pct < MIN_PREMARKET_GAP_PCT: soft_warning_reasons.append('weak_premarket_gap')
    if premarket_notional < required_premarket_notional: hard_reject_reasons.append('low_premarket_dollar_volume')
    if rvol < MIN_RVOL: soft_warning_reasons.append('low_rvol')
    if liquidity_meta.get('wide_spread_block'): hard_reject_reasons.append('spread_too_wide')
    if liquidity_meta.get('high_float_block'): soft_warning_reasons.append('high_float')
    if red_candle_trap.get('triggered'): hard_reject_reasons.append('heavy_red_candle_trap')
    if not mtf_aligned: soft_warning_reasons.append('choppy_trend')
    if entry_meta.get('extended'): hard_reject_reasons.append('extended_above_buy_zone')
    if wait_state: soft_warning_reasons.append('buy_window_closed')
    if after_time_gate and not or_stats.get('or_complete'): soft_warning_reasons.append('opening_range_not_complete')
    if market_internals.get('longs_blocked'): hard_reject_reasons.append('market_internals_block')
    if vixy_change >= VIX_CIRCUIT_BREAKER_PCT: hard_reject_reasons.append('vix_circuit_breaker')
    if sizing['qty'] < 1: hard_reject_reasons.append('risk_qty_below_1')

    entry_trigger, entry_trigger_reason = detect_entry_trigger_name(or_stats, vwap_meta, pullback_meta, trend_efficiency)
    if entry_trigger == 'NO_TRIGGER':
        soft_warning_reasons.extend(['no_orb_breakout', 'no_vwap_reclaim'])

    component_scores_detail = {
        'premarket_gap_score': gap_score,
        'premarket_dollar_volume_score': premarket_dollar_vol_score,
        'relative_volume_score': rvol_score,
        'opening_strength_score': opening_strength_score,
        'vwap_behavior_score': vwap_behavior_score,
        'opening_range_score': opening_range_score,
        'trend_score': trend_score,
    }
    in_buy_zone = current_price >= buy_lower * 0.995 and current_price <= buy_upper
    spread_safe = safe_num(liquidity_meta.get('spread_pct')) <= MAX_SPREAD_PCT
    setup_grade, grade_reason = classify_setup_grade(total, entry_trigger, hard_reject_reasons, component_scores_detail, catalyst_score, spread_safe, liquidity_score, sizing['qty'])
    decision = 'NO TRADE'
    if hard_reject_reasons or setup_grade == 'NO TRADE':
        decision = 'NO TRADE'
    elif setup_grade in {'A+', 'A'} and wait_state:
        decision = 'WAIT'
    elif setup_grade in {'A+', 'A'} and entry_trigger == 'NO_TRIGGER':
        decision = 'WAIT' if in_buy_zone else 'WATCH FOR BREAKOUT'
    elif setup_grade in {'A+', 'A'} and (not in_buy_zone or not spread_safe or sizing['qty'] < 1):
        decision = 'WAIT'
    elif setup_grade in {'A+', 'A'} and after_time_gate and in_buy_zone and spread_safe and sizing['qty'] >= 1 and entry_trigger != 'NO_TRIGGER':
        decision = 'BUY NOW'
    elif setup_grade == 'WATCH':
        decision = 'WATCH FOR BREAKOUT'

    notes = []
    if or_stats.get('or_high'):
        notes.append(f"OR {OPENING_RANGE_START_ET}-{OPENING_RANGE_END_ET}: {or_stats['or_low']} to {or_stats['or_high']}")
    if vwap_meta.get('vwap'):
        notes.append(f"VWAP {vwap_meta['vwap']}")
    if open_rs_meta.get('edge') is not None:
        notes.append(f"Open RS vs SPY: {open_rs_meta.get('edge', 0)}%")

    # 1. Build the typed Sub-components
    component_scores = ComponentScores(
        catalyst=catalyst_score,
        liquidity=liquidity_score,
        daily_chart_alignment=daily_score,
        sector_sympathy=sector_score,
        open_relative_strength=open_rs_score,
        vwap_hold_reclaim=vwap_score,
        first_pullback=pullback_score,
        entry_quality=entry_score,
        opening_range_confirmation=confirm_score
    )

    watch_panel = WatchPanelDef(
        label=f"{now_et().strftime('%A')}: Watch {symbol}",
        buy_after=f'{NO_BUY_BEFORE_ET} ET',
        buy_range=[round(buy_lower, 2), round(buy_upper, 2)],
        max_shares=sizing['qty'],
        stop=round(entry_meta['stop_price'], 2),
        take_profit_range=[round(entry_meta['target_1'], 2), round(entry_meta['target_2'], 2)],
        max_dollar_loss=sizing['max_dollar_loss'],
        opening_range=[or_stats.get('or_low'), or_stats.get('or_high')],
        vwap=vwap_meta.get('vwap'),
        status=decision,
        setup_grade=setup_grade
    )

    # 2. Build the main typed Result Object
    analysis_result = SymbolAnalysisResult(
        symbol=symbol,
        score_total=total,
        decision=decision,
        current_price=round(current_price, 2),
        buy_lower=round(buy_lower, 2),
        buy_upper=buy_upper,
        entry_price=round(entry_meta['entry_price'], 2),
        stop_price=round(entry_meta['stop_price'], 2),
        target_1=round(entry_meta['target_1'], 2),
        target_2=round(entry_meta['target_2'], 2),
        qty=sizing['qty'],
        risk_per_share=entry_meta['risk_per_share'],
        max_dollar_loss=sizing['max_dollar_loss'],
        buying_power_used=sizing['buying_power_used'],
        rr_ratio_1=entry_meta['rr_ratio_1'],
        rr_ratio_2=entry_meta['rr_ratio_2'],
        score_models=ScoreTriplet(**model_scores).to_dict(),
        scores=component_scores,
        details={
            'catalyst': catalyst_meta,
            'liquidity': liquidity_meta,
            'daily_chart_alignment': daily_meta,
            'sector_sympathy': sector_meta,
            'open_relative_strength': open_rs_meta,
            'vwap_hold_reclaim': vwap_meta,
            'first_pullback': pullback_meta,
            'entry_quality': entry_meta,
            'opening_range': or_stats,
            'orb_setup': orb_meta,
            'opening_range_confirmation': confirm_meta,
            'price_change_pct': round(price_change_pct, 2),
            'premarket_gap_pct': round(premarket_gap_pct, 2),
            'spy_day_change_pct': round(spy_change_pct, 2),
            'spread': round(spread, 4),
            'spread_pct': round((spread / current_price) if current_price > 0 else 0.0, 4),
            'volume_profile': {'daily_poc': round(volume_poc, 4) if volume_poc else None, 'price_above_poc': bool(current_price > volume_poc) if volume_poc else None},
            'value_area': va_metrics,
            'market_internals': market_internals,
            'rvol': round(rvol, 2),
            'trend_efficiency': round(trend_efficiency, 3),
            'halt_risk': halt_risk,
            'relative_strength_vs_spy': round(safe_num(rel_strength_vs_spy), 2),
            'red_candle_trap': red_candle_trap,
            'mtf_vwap_aligned': mtf_aligned,
            'vix_circuit_breaker': vixy_change >= VIX_CIRCUIT_BREAKER_PCT,
            'vixy_change_pct_1h': round(vixy_change, 3),
            'required_premarket_dollar_volume': round(required_premarket_notional, 2),
            'hard_reject_reasons': hard_reject_reasons,
            'soft_warning_reasons': soft_warning_reasons,
            'entry_trigger': entry_trigger,
            'entry_trigger_reason': entry_trigger_reason,
            'actionable_now': decision == 'BUY NOW',
            'why_not_buying': hard_reject_reasons + soft_warning_reasons if decision != 'BUY NOW' else [],
            'grade_reason': grade_reason,
            'component_scores': component_scores_detail,
            'entry_reason': f"Entry near breakout/VWAP confirmation at {entry_meta['entry_price']}.",
            'stop_reason': 'Stop anchored to structure and ATR guardrails.',
            'target_reason': 'Target1 is quick scalp objective; Target2 is runner.',
            'risk_reward_reason': f"R:R1 {entry_meta['rr_ratio_1']}, R:R2 {entry_meta['rr_ratio_2']}.",
            'skip_reasons': hard_reject_reasons + soft_warning_reasons,
            'sizing': sizing,
            'quick_notes': notes,
        },
        setup_grade=setup_grade,
        watch_panel=watch_panel,
        buy_window_open=after_time_gate,
        opening_range_complete=bool(or_stats.get('or_complete')),
        breakout_confirmed=bool(confirm_meta.get('breakout_confirmed'))
    )

    # 3. Return as a dict so it passes safely to Flask and SQLite
    return analysis_result.to_dict()


def _ensure_candidate_execution_fields(candidate: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(candidate or {})
    details = dict(out.get('details') or {})
    out['details'] = details
    out.setdefault('hard_reject_reasons', list(details.get('hard_reject_reasons') or []))
    out.setdefault('why_not_buying', list(details.get('why_not_buying') or []))
    reasons = []
    for k in ['symbol', 'setup_grade', 'decision', 'score_total', 'scores', 'current_price', 'entry_price', 'stop_price', 'target_1', 'target_2', 'buy_lower', 'buy_upper']:
        if out.get(k) in (None, ''):
            reasons.append(f'missing_{k}')
    if out.get('qty') is None:
        reasons.append('missing_qty')
    if details.get('spread_pct') is None:
        reasons.append('missing_details.spread_pct')
    if not (details.get('entry_trigger') or details.get('momentum_continuation')):
        reasons.append('missing_entry_trigger_or_momentum_continuation')
    try:
        current_price = float(out.get('current_price'))
        if current_price <= 0:
            reasons.append('invalid_current_price')
    except Exception:
        if out.get('current_price') not in (None, ''):
            reasons.append('invalid_current_price')
    try:
        entry_price = float(out.get('entry_price'))
        if entry_price <= 0:
            reasons.append('invalid_entry_price')
    except Exception:
        if out.get('entry_price') not in (None, ''):
            reasons.append('invalid_entry_price')
        entry_price = 0.0
    try:
        stop_price = float(out.get('stop_price'))
        if stop_price <= 0:
            reasons.append('invalid_stop_price')
    except Exception:
        if out.get('stop_price') not in (None, ''):
            reasons.append('invalid_stop_price')
        stop_price = 0.0
    try:
        buy_upper = float(out.get('buy_upper'))
        if buy_upper <= 0:
            reasons.append('invalid_buy_upper')
    except Exception:
        if out.get('buy_upper') not in (None, ''):
            reasons.append('invalid_buy_upper')
    if stop_price >= entry_price and entry_price > 0 and stop_price > 0:
        reasons.append('invalid_risk')
    try:
        target_1 = float(out.get('target_1'))
        target_2 = float(out.get('target_2'))
        if (entry_price > 0 and target_1 <= entry_price) or target_2 < target_1:
            reasons.append('invalid_targets')
    except Exception:
        pass
    if reasons:
        out['hard_reject_reasons'] = list(dict.fromkeys((out.get('hard_reject_reasons') or []) + reasons))
        out['why_not_buying'] = list(dict.fromkeys((out.get('why_not_buying') or []) + reasons))
    return out

def run_scan() -> Dict[str, Any]:
    symbols, rejected_candidates = get_refined_universe()
    if not symbols:
        raise ScanError('No symbols passed the refined universe gatekeeper.')
    last_diag = get_last_scan_diagnostics()
    diag = dict(_LAST_BROAD_SCAN_DIAGNOSTICS or {})
    ranked_pool = list(diag.get('ranked_candidate_pool') or [])
    candidate_pool = dedupe_preserve_order(symbols + ranked_pool + ['SPY'])
    snapshots = get_snapshots(candidate_pool)
    quotes = get_latest_quotes(candidate_pool)
    sector_symbols = ['SPY', 'SMH', 'XLK', 'XLF', 'XLV', 'XLY', 'XLC', 'XLI', 'XLE', 'XLU', 'XLRE', 'XLB', 'XBI', 'KBE']
    sector_snapshots = get_snapshots([s for s in sector_symbols if s not in symbols])
    sector_snapshots.update({k: v for k, v in snapshots.items() if k in sector_symbols})
    end = now_utc()
    daily_bars_map = get_bars(candidate_pool, '1Day', end - timedelta(days=400), end, 400)
    minute_bars_map = get_bars(candidate_pool, '1Min', end - timedelta(days=3), end, 1000)
    bars_diag_daily = dict(_LAST_BARS_FETCH_DIAGNOSTICS.get('1Day', {}))
    bars_diag_minute = dict(_LAST_BARS_FETCH_DIAGNOSTICS.get('1Min', {}))

    spy_snap = snapshots.get('SPY', {})
    spy_prev = safe_num(spy_snap.get('prevDailyBar', {}).get('c')) or 1
    spy_curr = safe_num(spy_snap.get('dailyBar', {}).get('c')) or safe_num(spy_snap.get('minuteBar', {}).get('c')) or spy_prev
    spy_change_pct = ((spy_curr - spy_prev) / spy_prev * 100.0) if spy_prev > 0 else 0.0
    spy_minute_bars = minute_bars_map.get('SPY', [])
    market_internals = get_market_internals_bias()

    ranked = []
    eligible_symbols = [s for s in candidate_pool if s != 'SPY' and snapshots.get(s) and daily_bars_map.get(s) and minute_bars_map.get(s)]
    deep_analysis_target = int(diag.get('deep_analysis_target') or DEEP_ANALYSIS_TOP_N)
    deep_analysis_requested_count = int(diag.get('deep_analysis_requested_count') or len([s for s in symbols if s != 'SPY']))
    symbols_evaluated_count = 0
    symbols_analyzed_count = 0
    symbols_missing_data = []
    symbols_missing_daily_bars_count = 0
    symbols_missing_minute_bars_count = 0
    symbols_skipped_reasons: Dict[str, str] = {}
    print(f"\n--- DEBUG: STARTING SCAN LOOP FOR {len(candidate_pool)} SYMBOLS (eligible={len(eligible_symbols)}, target analyzed={deep_analysis_target}) ---")
    for symbol in candidate_pool:
        if symbol == 'SPY':
            continue
        if symbols_analyzed_count >= deep_analysis_target:
            break
        symbols_evaluated_count += 1
        daily_bars = daily_bars_map.get(symbol, [])
        minute_bars = minute_bars_map.get(symbol, [])
        snapshot = snapshots.get(symbol, {})
        quote = quotes.get(symbol, {})
        ask = safe_num(quote.get('ap'))
        minute_close = safe_num(snapshot.get('minuteBar', {}).get('c'))
        daily_close = safe_num(snapshot.get('dailyBar', {}).get('c'))
        current_price = ask or minute_close or daily_close

        print(f"Evaluating {symbol}: Price=${current_price}, DailyBars={len(daily_bars)}, MinBars={len(minute_bars)}")

        # FIX 2: Allow up to $500.00
        if current_price and current_price >= 500.0:
            print(f" -> SKIP: {symbol} price too high.")
            symbols_skipped_reasons[symbol] = 'price_too_high'
            continue
        if not snapshot or not daily_bars or not minute_bars:
            print(f" -> SKIP: {symbol} missing Alpaca data.")
            symbols_missing_data.append(symbol)
            if not daily_bars:
                symbols_missing_daily_bars_count += 1
            if not minute_bars:
                symbols_missing_minute_bars_count += 1
            symbols_skipped_reasons[symbol] = 'missing_alpaca_data'
            continue

        # We removed the silent exception so we can see exact crashes
        try:
            profile = get_company_profile(symbol)
            asset = get_alpaca_asset(symbol)
            asset_name = str((asset or {}).get('name', '')).upper()
            if any(k in asset_name for k in ('WARRANT', 'RIGHT', 'UNIT', 'PREF', 'PREFERRED', 'ADR', 'DR', 'FUND', 'TRUST')):
                symbols_skipped_reasons[symbol] = 'non_common_equity_asset'
                rejected_candidates.append({'symbol': symbol, 'hard_reject_reasons': ['not_common_equity'], 'why_not_buying': ['not_common_equity_asset'], 'asset_name': asset.get('name')})
                continue
            ranked.append(analyze_symbol(symbol, snapshot, quote, daily_bars, minute_bars, spy_change_pct, profile, asset, spy_minute_bars, sector_snapshots, market_internals))
            symbols_analyzed_count += 1
            print(f" -> SUCCESS: Analyzed {symbol}")
        except Exception as e:
            import traceback
            print(f" -> CRASH on {symbol}: {e}")
            traceback.print_exc()
            symbols_skipped_reasons[symbol] = f'analysis_error:{e}'
            continue

    print("--- DEBUG: SCAN LOOP FINISHED ---\n")

    candidate_pool_exhausted = symbols_analyzed_count < deep_analysis_target and symbols_evaluated_count >= len([s for s in candidate_pool if s != 'SPY'])
    deep_backfill_attempts = max(0, symbols_evaluated_count - deep_analysis_requested_count)
    deep_backfill_used = deep_backfill_attempts > 0
    deep_backfill_chunks = deep_backfill_attempts

    if not ranked:
        raise ScanError('No tradeable candidates were found from the current market data.')

    grade_rank = {'A+': 4, 'A': 3, 'WATCH': 2, 'NO TRADE': 1}
    ranked.sort(
        key=lambda x: (
            grade_rank.get(x.get('setup_grade'), 0),
            x['decision'] == 'BUY NOW',
            x['decision'] == 'WATCH FOR BREAKOUT',
            x['scores']['catalyst'],
            x['scores'].get('sector_sympathy', 0),
            x['score_total'],
            x['details']['open_relative_strength'].get('edge', -999),
            -x['details']['liquidity']['spread'],
        ),
        reverse=True,
    )
    ranked = [_ensure_candidate_execution_fields(r) for r in ranked]
    best = ranked[0]
    chart_pack = get_stock_chart_pack(best['symbol'])
    valid_candidates = [r for r in ranked if r.get('setup_grade') in {'A+', 'A'}]
    market_call = 'NO TRADE TODAY'
    if valid_candidates:
        market_call = f"{valid_candidates[0]['setup_grade']} setup available"
    elif any(r.get('setup_grade') == 'WATCH' for r in ranked):
        market_call = 'WATCH ONLY'
    return {
        'generated_at': now_utc().isoformat(),
        'day_of_week': now_et().strftime('%A'),
        'market_bias_proxy': {'spy_change_pct': round(spy_change_pct, 2), 'market_internals': market_internals},
        'market_call': market_call,
        'best_pick': best,
        'watchlist': ranked[:WATCHLIST_SIZE],
        'ranked': ranked[:10],
        'rejected_candidates': rejected_candidates,
        'chart_pack': chart_pack,
        'scan_diagnostics': {
            'broad_universe_enabled': bool(BROAD_UNIVERSE_SCAN_ENABLED),
            'broad_universe_count': diag.get('broad_universe_count', diag.get('broad_pulled_count', len(symbols))),
            'broad_candidates_ranked': diag.get('broad_candidates_ranked', diag.get('broad_ranked_count')),
            'ranked_candidate_pool': diag.get('ranked_candidate_pool', []),
            'ranked_candidate_pool_count': diag.get('ranked_candidate_pool_count', 0),
            'deep_analysis_target': deep_analysis_target,
            'deep_analysis_requested_count': deep_analysis_requested_count,
            'symbols_evaluated_count': symbols_evaluated_count,
            'symbols_analyzed_count': symbols_analyzed_count,
            'symbols_missing_data_count': len(symbols_missing_data),
            'symbols_missing_daily_bars_count': symbols_missing_daily_bars_count,
            'symbols_missing_minute_bars_count': symbols_missing_minute_bars_count,
            'symbols_missing_data_sample': symbols_missing_data[:25],
            'deep_backfill_used': deep_backfill_used,
            'deep_backfill_chunks': deep_backfill_chunks,
            'candidate_pool_exhausted': candidate_pool_exhausted,
            'fallback_used': bool(diag.get('fallback_used', False)),
            'broad_scan_errors': list(diag.get('broad_scan_errors', [])),
            'symbols_missing_data': symbols_missing_data,
            'symbols_skipped_reasons': symbols_skipped_reasons,
            'eligible_symbol_count': len(eligible_symbols),
            'bars_fetch_diagnostics': {'1Day': bars_diag_daily, '1Min': bars_diag_minute},
            # Legacy compatibility fields
            'broad_ranked_count': diag.get('broad_ranked_count', diag.get('broad_candidates_ranked')),
            'deep_analysis_count': diag.get('deep_analysis_count', deep_analysis_requested_count),
        },
        'rules_applied': {
            'min_catalyst_score': MIN_CATALYST_SCORE,
            'no_buy_before_et': NO_BUY_BEFORE_ET,
            'opening_range_window_et': f'{OPENING_RANGE_START_ET}-{OPENING_RANGE_END_ET}',
            'max_spread_pct': MAX_SPREAD_PCT,
            'max_entry_extension_pct': MAX_ENTRY_EXTENSION_PCT,
            'current_bankroll': CURRENT_BANKROLL,
            'risk_pct_per_trade': 0.02,
            'dynamic_dollar_risk_limit': round(CURRENT_BANKROLL * 0.02, 2),
            'a_plus_score': A_PLUS_SCORE,
            'a_score': A_SCORE,
            'min_premarket_gap_pct': MIN_PREMARKET_GAP_PCT,
            'min_premarket_dollar_vol': MIN_PREMARKET_DOLLAR_VOL,
            'market_internals_block_enabled': MARKET_INTERNALS_BLOCK_ENABLED,
        },
    }
