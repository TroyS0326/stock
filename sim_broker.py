import time
import uuid
from typing import Any, Dict, List

import config
from db import get_conn, utc_now


STATUSES_OPEN = {'new', 'open'}


def _ensure_tables() -> None:
    with get_conn() as conn:
        conn.executescript(
            '''
            CREATE TABLE IF NOT EXISTS sim_account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cash REAL NOT NULL,
                equity REAL NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sim_positions (
                symbol TEXT PRIMARY KEY,
                qty REAL NOT NULL,
                avg_entry_price REAL NOT NULL,
                current_price REAL NOT NULL,
                market_value REAL NOT NULL,
                unrealized_pl REAL NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sim_orders (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                qty REAL NOT NULL,
                status TEXT NOT NULL,
                limit_price REAL,
                stop_price REAL,
                trail_percent REAL,
                filled_qty REAL DEFAULT 0,
                filled_avg_price REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            '''
        )
        row = conn.execute('SELECT id FROM sim_account WHERE id=1').fetchone()
        if not row:
            start_cash = float(config.SIMULATED_STARTING_CASH)
            conn.execute(
                'INSERT INTO sim_account (id, cash, equity, updated_at) VALUES (1, ?, ?, ?)',
                (start_cash, start_cash, utc_now()),
            )


def _new_order_id() -> str:
    return f"SIM-ORDER-{uuid.uuid4().hex[:12].upper()}"


def _fill_price(symbol: str, side: str, requested_price: float | None = None) -> float:
    px = float(requested_price or 10.0)
    spread = float(config.SIMULATED_DEFAULT_SPREAD_PCT)
    return round(px * (1 + spread if side == 'buy' else 1 - spread), 2)


def _insert_order(symbol: str, side: str, order_type: str, qty: float, price: float | None = None, stop_price: float | None = None, trail_percent: float | None = None) -> Dict[str, Any]:
    _ensure_tables()
    now = utc_now()
    oid = _new_order_id()
    status = 'open' if float(config.SIMULATED_ORDER_FILL_DELAY_SECONDS) > 0 else 'filled'
    filled_qty = qty if status == 'filled' else 0
    fill_px = _fill_price(symbol, side, price or stop_price) if status == 'filled' else None
    with get_conn() as conn:
        conn.execute(
            '''INSERT INTO sim_orders (id,symbol,side,order_type,qty,status,limit_price,stop_price,trail_percent,filled_qty,filled_avg_price,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (oid, symbol.upper(), side, order_type, float(qty), status, price, stop_price, trail_percent, filled_qty, fill_px, now, now),
        )
    if status == 'filled':
        _apply_fill(oid)
    else:
        _maybe_fill_due_orders()
    return get_order(oid)


def _apply_fill(order_id: str) -> None:
    with get_conn() as conn:
        order = conn.execute('SELECT * FROM sim_orders WHERE id=?', (order_id,)).fetchone()
        if not order:
            return
        o = dict(order)
        if o['status'] == 'filled':
            pass
        elif o['status'] in {'canceled', 'rejected'}:
            return
        qty = float(o['qty'])
        fill_px = float(o['filled_avg_price'] or _fill_price(o['symbol'], o['side'], o.get('limit_price') or o.get('stop_price')))
        conn.execute('UPDATE sim_orders SET status=?, filled_qty=?, filled_avg_price=?, updated_at=? WHERE id=?', ('filled', qty, fill_px, utc_now(), order_id))

        acct = dict(conn.execute('SELECT * FROM sim_account WHERE id=1').fetchone())
        cash = float(acct['cash'])
        pos_row = conn.execute('SELECT * FROM sim_positions WHERE symbol=?', (o['symbol'],)).fetchone()
        pos = dict(pos_row) if pos_row else None
        if o['side'] == 'buy':
            cost = qty * fill_px
            cash -= cost
            if pos:
                old_qty = float(pos['qty'])
                new_qty = old_qty + qty
                avg = ((old_qty * float(pos['avg_entry_price'])) + cost) / new_qty
                conn.execute('UPDATE sim_positions SET qty=?, avg_entry_price=?, current_price=?, market_value=?, unrealized_pl=?, updated_at=? WHERE symbol=?',
                             (new_qty, avg, fill_px, new_qty * fill_px, (fill_px-avg)*new_qty, utc_now(), o['symbol']))
            else:
                conn.execute('INSERT INTO sim_positions (symbol,qty,avg_entry_price,current_price,market_value,unrealized_pl,updated_at) VALUES (?,?,?,?,?,?,?)',
                             (o['symbol'], qty, fill_px, fill_px, qty*fill_px, 0.0, utc_now()))
        else:
            proceeds = qty * fill_px
            cash += proceeds
            if pos:
                old_qty = float(pos['qty'])
                new_qty = max(0.0, old_qty - qty)
                if new_qty <= 0:
                    conn.execute('DELETE FROM sim_positions WHERE symbol=?', (o['symbol'],))
                else:
                    avg = float(pos['avg_entry_price'])
                    conn.execute('UPDATE sim_positions SET qty=?, current_price=?, market_value=?, unrealized_pl=?, updated_at=? WHERE symbol=?',
                                 (new_qty, fill_px, new_qty*fill_px, (fill_px-avg)*new_qty, utc_now(), o['symbol']))
        equity = cash + sum([float(r['market_value']) for r in conn.execute('SELECT market_value FROM sim_positions').fetchall()])
        conn.execute('UPDATE sim_account SET cash=?, equity=?, updated_at=? WHERE id=1', (cash, equity, utc_now()))


def _maybe_fill_due_orders() -> None:
    delay = float(config.SIMULATED_ORDER_FILL_DELAY_SECONDS)
    if delay <= 0:
        return
    now = time.time()
    with get_conn() as conn:
        rows = conn.execute('SELECT id, created_at, status FROM sim_orders WHERE status IN ("new","open")').fetchall()
    for r in rows:
        created = time.mktime(time.strptime(dict(r)['created_at'][:19], '%Y-%m-%dT%H:%M:%S')) if 'T' in dict(r)['created_at'] else 0
        if created and (now - created) >= delay:
            _apply_fill(dict(r)['id'])


def place_managed_entry_order(symbol: str, qty: int, entry_price: float, stop_price: float, target_1_price: float, target_2_price: float, avg_1m_volume: float = 0.0) -> Dict[str, Any]:
    _ = target_2_price, avg_1m_volume
    entry = _insert_order(symbol, 'buy', 'limit', qty, price=entry_price)
    t1_qty = max(1, int(float(entry.get('filled_qty') or qty)) // 2)
    runner_qty = max(0, int(float(entry.get('filled_qty') or qty)) - t1_qty)
    t1 = _insert_order(symbol, 'sell', 'limit', t1_qty, price=target_1_price)
    runner = _insert_order(symbol, 'sell', 'stop', runner_qty, stop_price=stop_price) if runner_qty > 0 else None
    return {'id': entry['id'], 'status': entry['status'], 'symbol': symbol.upper(), 'filled_qty': entry.get('filled_qty'), 'filled_avg_price': entry.get('filled_avg_price'), 'strategy': 'target1_then_trailing_runner', 'entry_order': entry, 'target_1_order_id': t1['id'], 'runner_stop_order_id': (runner or {}).get('id'), 'runner_trailing_pct': config.TARGET2_TRAILING_STOP_PCT}


def submit_market_sell(symbol: str, qty: int) -> Dict[str, Any]: return _insert_order(symbol, 'sell', 'market', qty)
def submit_stop_sell(symbol: str, qty: int, stop_price: float) -> Dict[str, Any]: return _insert_order(symbol, 'sell', 'stop', qty, stop_price=stop_price)
def submit_trailing_stop_sell(symbol: str, qty: int, trail_percent: float) -> Dict[str, Any]: return _insert_order(symbol, 'sell', 'trailing_stop', qty, trail_percent=trail_percent)

def get_open_positions() -> List[Dict[str, Any]]:
    _maybe_fill_due_orders(); _ensure_tables()
    with get_conn() as conn: return [dict(r) for r in conn.execute('SELECT * FROM sim_positions').fetchall()]

def get_open_orders(symbol: str | None = None) -> List[Dict[str, Any]]:
    _maybe_fill_due_orders(); _ensure_tables()
    q = 'SELECT * FROM sim_orders WHERE status IN ("new","open")'
    params = []
    if symbol: q += ' AND symbol=?'; params.append(symbol.upper())
    with get_conn() as conn: return [dict(r) for r in conn.execute(q, tuple(params)).fetchall()]

def get_order(order_id: str) -> Dict[str, Any]:
    _maybe_fill_due_orders(); _ensure_tables()
    with get_conn() as conn:
        row = conn.execute('SELECT * FROM sim_orders WHERE id=?', (order_id,)).fetchone()
    return dict(row) if row else {}

def cancel_open_orders_for_symbol(symbol: str, side: str | None = None) -> List[str]:
    _ensure_tables(); canceled=[]
    with get_conn() as conn:
        rows = conn.execute('SELECT id, side FROM sim_orders WHERE symbol=? AND status IN ("new","open")', (symbol.upper(),)).fetchall()
        for r in rows:
            o=dict(r)
            if side and o['side'] != side: continue
            conn.execute('UPDATE sim_orders SET status=?, updated_at=? WHERE id=?', ('canceled', utc_now(), o['id']))
            canceled.append(o['id'])
    return canceled

def close_position(symbol: str) -> Dict[str, Any]:
    pos = next((p for p in get_open_positions() if p.get('symbol') == symbol.upper()), None)
    if not pos: return {}
    return submit_market_sell(symbol, int(float(pos.get('qty') or 0)))

def get_account() -> Dict[str, Any]:
    _ensure_tables()
    with get_conn() as conn: return dict(conn.execute('SELECT * FROM sim_account WHERE id=1').fetchone())
