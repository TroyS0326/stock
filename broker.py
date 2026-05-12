import time
import math
from typing import Any, Dict, List

import requests

from config import (
    ALPACA_API_KEY,
    ALPACA_API_SECRET,
    ALPACA_DATA_BASE,
    ALPACA_FEED,
    ALPACA_PAPER_BASE,
    ENTRY_ORDER_POLL_SECONDS,
    ENTRY_ORDER_TIMEOUT_SECONDS,
    ENTRY_LIMIT_PRICE_BUFFER_PCT,
    TARGET2_TRAILING_STOP_PCT,
    ENTRY_RETRY_ENABLED,
    ENTRY_RETRY_TIMEOUT_SECONDS,
    ENTRY_RETRY_LIMIT_BUFFER_PCT,
)

TIMEOUT = 20


class BrokerError(Exception):
    pass


def _headers() -> Dict[str, str]:
    if not ALPACA_API_KEY or not ALPACA_API_SECRET:
        raise BrokerError('Missing Alpaca paper-trading credentials in .env')
    return {
        'accept': 'application/json',
        'content-type': 'application/json',
        'APCA-API-KEY-ID': ALPACA_API_KEY,
        'APCA-API-SECRET-KEY': ALPACA_API_SECRET,
    }


def _get_json(url: str, params: Dict[str, Any] | None = None) -> Any:
    resp = requests.get(url, params=params or {}, headers=_headers(), timeout=TIMEOUT)
    if resp.status_code >= 400:
        raise BrokerError(resp.text)
    return resp.json()


def _post_json(url: str, payload: Dict[str, Any]) -> Any:
    resp = requests.post(url, json=payload, headers=_headers(), timeout=TIMEOUT)
    if resp.status_code >= 400:
        raise BrokerError(resp.text)
    return resp.json()


def _patch_json(url: str, payload: Dict[str, Any]) -> Any:
    resp = requests.patch(url, json=payload, headers=_headers(), timeout=TIMEOUT)
    if resp.status_code >= 400:
        raise BrokerError(resp.text)
    return resp.json()


def get_latest_quote(symbol: str) -> Dict[str, Any]:
    symbol = symbol.upper()
    data = _get_json(
        f'{ALPACA_DATA_BASE}/v2/stocks/quotes/latest',
        params={'symbols': symbol, 'feed': ALPACA_FEED},
    )
    return (data.get('quotes') or {}).get(symbol, {})


def submit_order(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _post_json(f'{ALPACA_PAPER_BASE}/v2/orders', payload)


def replace_order(order_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return _patch_json(f'{ALPACA_PAPER_BASE}/v2/orders/{order_id}', payload)


def cancel_order(order_id: str) -> None:
    resp = requests.delete(f'{ALPACA_PAPER_BASE}/v2/orders/{order_id}', headers=_headers(), timeout=TIMEOUT)
    if resp.status_code not in {200, 204, 404, 422}:
        raise BrokerError(resp.text)


def get_order(order_id: str) -> Dict[str, Any]:
    resp = requests.get(
        f'{ALPACA_PAPER_BASE}/v2/orders/{order_id}',
        params={'nested': 'true'},
        headers=_headers(),
        timeout=TIMEOUT,
    )
    if resp.status_code >= 400:
        raise BrokerError(resp.text)
    return resp.json()


def get_orders(order_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for oid in order_ids:
        if not oid:
            continue
        try:
            out[oid] = get_order(oid)
        except BrokerError:
            continue
    return out






def get_account() -> Dict[str, Any]:
    return _get_json(f'{ALPACA_PAPER_BASE}/v2/account')


def get_clock() -> Dict[str, Any]:
    return _get_json(f'{ALPACA_PAPER_BASE}/v2/clock')

def get_open_positions() -> List[Dict[str, Any]]:
    data = _get_json(f'{ALPACA_PAPER_BASE}/v2/positions')
    return data if isinstance(data, list) else []


def get_position(symbol: str) -> Dict[str, Any]:
    symbol = symbol.upper()
    resp = requests.get(f'{ALPACA_PAPER_BASE}/v2/positions/{symbol}', headers=_headers(), timeout=TIMEOUT)
    if resp.status_code == 404:
        return {}
    if resp.status_code >= 400:
        raise BrokerError(resp.text)
    return resp.json()


def close_position(symbol: str, qty: int | None = None) -> Dict[str, Any]:
    payload = {'qty': str(qty)} if qty else {}
    resp = requests.delete(f'{ALPACA_PAPER_BASE}/v2/positions/{symbol.upper()}', headers=_headers(), params=payload, timeout=TIMEOUT)
    if resp.status_code >= 400 and resp.status_code not in {404}:
        raise BrokerError(resp.text)
    return resp.json() if resp.text else {}


def submit_market_sell(symbol: str, qty: int) -> Dict[str, Any]:
    return submit_order({'symbol': symbol.upper(), 'qty': str(qty), 'side': 'sell', 'type': 'market', 'time_in_force': 'day'})


def submit_stop_sell(symbol: str, qty: int, stop_price: float) -> Dict[str, Any]:
    return submit_order(
        {
            'symbol': symbol.upper(),
            'qty': str(qty),
            'side': 'sell',
            'type': 'stop',
            'time_in_force': 'day',
            'stop_price': round_sell_limit(stop_price),
        }
    )


def submit_trailing_stop_sell(symbol: str, qty: int, trail_percent: float) -> Dict[str, Any]:
    return submit_order(
        {
            'symbol': symbol.upper(),
            'qty': str(qty),
            'side': 'sell',
            'type': 'trailing_stop',
            'time_in_force': 'day',
            'trail_percent': round(float(trail_percent), 4),
        }
    )


def get_open_orders(symbol: str | None = None) -> List[Dict[str, Any]]:
    params = {'status': 'open', 'nested': 'true'}
    if symbol:
        params['symbols'] = symbol.upper()
    data = _get_json(f'{ALPACA_PAPER_BASE}/v2/orders', params=params)
    return data if isinstance(data, list) else []


def cancel_open_orders_for_symbol(symbol: str, side: str | None = None) -> List[str]:
    canceled_ids: List[str] = []
    for order in get_open_orders(symbol):
        order_side = (order.get('side') or '').lower()
        if side and order_side != side.lower():
            continue
        order_id = order.get('id')
        if not order_id:
            continue
        cancel_order(order_id)
        canceled_ids.append(order_id)
    return canceled_ids


def replace_order_qty(order_id: str, qty: int) -> Dict[str, Any]:
    if qty < 1:
        raise BrokerError('Replacement quantity must be at least 1 share.')
    return replace_order(order_id, {'qty': str(int(qty))})
def _poll_for_fill(order_id: str, timeout_seconds: float) -> Dict[str, Any]:
    started = time.time()
    while True:
        order = get_order(order_id)
        status = (order.get('status') or '').lower()
        if status == 'filled':
            return order
        if status in {'canceled', 'expired', 'rejected', 'done_for_day'}:
            raise BrokerError(f'Entry order {order_id} ended as {status}.')
        if time.time() - started >= timeout_seconds:
            cancel_order(order_id)
            raise BrokerError(
                f'Entry order was not filled in {int(timeout_seconds)} seconds and was canceled to avoid slippage.'
            )
        time.sleep(max(0.25, ENTRY_ORDER_POLL_SECONDS))


def _pegged_limit_entry(symbol: str, qty: int, side: str = 'buy', buffer_pct: float = ENTRY_LIMIT_PRICE_BUFFER_PCT, max_limit_price: float | None = None) -> Dict[str, Any]:
    quote = get_latest_quote(symbol)
    ask = float(quote.get('ap') or 0)
    bid = float(quote.get('bp') or 0)
    if side == 'buy':
        ref = ask or bid
        peg_price = ref * (1 + buffer_pct)
    else:
        ref = bid or ask
        peg_price = ref * (1 - buffer_pct)
    if peg_price <= 0:
        raise BrokerError(f'No valid quote available to peg entry order for {symbol}.')
    if max_limit_price and side == 'buy':
        peg_price = min(peg_price, max_limit_price)
    order = submit_order(
        {
            'symbol': symbol.upper(),
            'qty': str(qty),
            'side': side,
            'type': 'limit',
            'time_in_force': 'day',
            'limit_price': round_buy_limit(peg_price) if side == 'buy' else round_sell_limit(peg_price),
        }
    )
    order['quote'] = {'bid': bid, 'ask': ask}
    order['peg_buffer_pct'] = buffer_pct
    order['peg_price'] = round_buy_limit(peg_price) if side == 'buy' else round_sell_limit(peg_price)
    return order


def place_managed_entry_order(
    symbol: str,
    qty: int,
    entry_price: float,
    stop_price: float,
    target_1_price: float,
    target_2_price: float,
    avg_1m_volume: float = 0.0,
    max_entry_price: float | None = None,
) -> Dict[str, Any]:
    # Microstructure liquidity cap (max 5% of 1-minute volume).
    if avg_1m_volume > 0:
        max_safe_qty = int(0.05 * avg_1m_volume)
        if qty > max_safe_qty:
            qty = max(1, max_safe_qty)

    _ = entry_price, target_2_price  # reserved for external broker adapters and journaling.
    max_limit_price = max_entry_price
    if (max_limit_price is None) and entry_price > 0:
        max_limit_price = entry_price * (1 + ENTRY_RETRY_LIMIT_BUFFER_PCT)
    entry = _pegged_limit_entry(symbol=symbol, qty=qty, side='buy', buffer_pct=ENTRY_LIMIT_PRICE_BUFFER_PCT, max_limit_price=max_limit_price)
    entry_id = entry.get('id')
    if not entry_id:
        raise BrokerError('Broker did not return an order id for entry.')
    try:
        filled_entry = _poll_for_fill(entry_id, ENTRY_ORDER_TIMEOUT_SECONDS)
    except BrokerError as exc:
        retry_allowed = bool(ENTRY_RETRY_ENABLED) and ('paper-api.alpaca.markets' in str(ALPACA_PAPER_BASE))
        if not retry_allowed:
            raise
        retry_entry = _pegged_limit_entry(symbol=symbol, qty=qty, side='buy', buffer_pct=ENTRY_RETRY_LIMIT_BUFFER_PCT, max_limit_price=max_limit_price)
        retry_id = retry_entry.get('id')
        if not retry_id:
            raise BrokerError(f'entry_timeout_no_retry_order_id:{exc}')
        try:
            filled_entry = _poll_for_fill(retry_id, ENTRY_RETRY_TIMEOUT_SECONDS)
            entry = retry_entry
            entry_id = retry_id
        except BrokerError as retry_exc:
            raise BrokerError(f'entry_timeout_after_retry:first={exc};retry={retry_exc}')
    filled_qty = int(float(filled_entry.get('filled_qty') or qty))
    if filled_qty < 1:
        raise BrokerError('Entry order reported no shares filled.')

    stop_full_order = None
    try:
        stop_full_order = submit_stop_sell(symbol, filled_qty, stop_price)
    except BrokerError as exc:
        submit_market_sell(symbol, filled_qty)
        raise BrokerError(f'Failed to place protective stop after fill; flattened immediately: {exc}')

    return {
        'id': entry_id,
        'status': 'filled',
        'symbol': symbol.upper(),
        'filled_qty': str(filled_qty),
        'filled_avg_price': filled_entry.get('filled_avg_price'),
        'strategy': 'target1_then_trailing_runner',
        'entry_order': filled_entry,
        'target_1_order_id': None,
        'runner_stop_order_id': (stop_full_order or {}).get('id'),
        'runner_trailing_pct': TARGET2_TRAILING_STOP_PCT,
        'protection_mode': 'full_stop_only',
    }


def maybe_activate_runner_trailing(raw_trade_payload: Dict[str, Any], breakeven_price: float) -> Dict[str, Any]:
    if (raw_trade_payload or {}).get('strategy') != 'target1_then_trailing_runner':
        return raw_trade_payload
    if raw_trade_payload.get('runner_trailing_activated'):
        return raw_trade_payload

    target_1_id = raw_trade_payload.get('target_1_order_id')
    runner_stop_id = raw_trade_payload.get('runner_stop_order_id')
    if not target_1_id or not runner_stop_id:
        return raw_trade_payload

    target_1 = get_order(target_1_id)
    if (target_1.get('status') or '').lower() != 'filled':
        return raw_trade_payload

    # Lock in a "base hit": move stop to breakeven first, then convert to trailing.
    replace_order(runner_stop_id, {'stop_price': round(breakeven_price, 2)})
    cancel_order(runner_stop_id)
    runner_qty = int(float(target_1.get('qty') or 0))
    remaining_qty = int(float(raw_trade_payload.get('filled_qty') or 0)) - runner_qty
    if remaining_qty < 1:
        raw_trade_payload['runner_trailing_activated'] = True
        return raw_trade_payload

    trailing = submit_order(
        {
            'symbol': raw_trade_payload.get('symbol'),
            'qty': str(remaining_qty),
            'side': 'sell',
            'type': 'trailing_stop',
            'time_in_force': 'day',
            'trail_percent': str(round(TARGET2_TRAILING_STOP_PCT, 4)),
        }
    )
    raw_trade_payload['runner_trailing_activated'] = True
    raw_trade_payload['runner_trailing_order_id'] = trailing.get('id')
    raw_trade_payload['runner_breakeven_price'] = round(breakeven_price, 2)
    return raw_trade_payload
def _tick_size(price: float) -> float:
    return 0.01 if price >= 1.0 else 0.0001


def round_buy_limit(price: float) -> float:
    tick = _tick_size(price)
    out = math.ceil(max(price, tick) / tick) * tick
    return max(tick, round(out, 4 if tick < 0.01 else 2))


def round_sell_limit(price: float) -> float:
    tick = _tick_size(price)
    out = math.floor(max(price, tick) / tick) * tick
    return max(tick, round(out, 4 if tick < 0.01 else 2))
