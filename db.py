import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional
from zoneinfo import ZoneInfo

import config


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_et_prefix() -> str:
    return datetime.now(ZoneInfo(config.TIMEZONE_LABEL)).date().isoformat()


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            '''
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                market_day TEXT,
                best_symbol TEXT,
                best_decision TEXT,
                best_score INTEGER,
                payload_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                scan_id INTEGER,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                decision TEXT NOT NULL,
                score_total INTEGER,
                current_price REAL,
                entry_price REAL NOT NULL,
                buy_lower REAL,
                buy_upper REAL,
                stop_price REAL NOT NULL,
                target_1 REAL NOT NULL,
                target_2 REAL NOT NULL,
                qty INTEGER,
                risk_per_share REAL,
                reward_to_target_1 REAL,
                reward_to_target_2 REAL,
                rr_ratio_1 REAL,
                rr_ratio_2 REAL,
                order_id TEXT,
                order_status TEXT,
                filled_avg_price REAL,
                filled_qty REAL,
                outcome TEXT,
                notes TEXT,
                raw_json TEXT,
                FOREIGN KEY(scan_id) REFERENCES scans(id)
            );

            CREATE TABLE IF NOT EXISTS operator_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                action TEXT NOT NULL,
                reason TEXT,
                success INTEGER NOT NULL,
                details_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_scans_created_at ON scans(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_trades_created_at ON trades(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_trades_order_id ON trades(order_id);
            '''
        )


def insert_scan(payload: Dict[str, Any]) -> int:
    best = payload.get('best_pick', {})
    with get_conn() as conn:
        cur = conn.execute(
            '''
            INSERT INTO scans (created_at, market_day, best_symbol, best_decision, best_score, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ''',
            (
                utc_now(),
                payload.get('day_of_week'),
                best.get('symbol'),
                best.get('decision'),
                best.get('score_total'),
                json.dumps(payload),
            ),
        )
        return int(cur.lastrowid)


def insert_trade(trade: Dict[str, Any]) -> int:
    now = utc_now()
    with get_conn() as conn:
        cur = conn.execute(
            '''
            INSERT INTO trades (
                created_at, updated_at, scan_id, symbol, side, decision, score_total, current_price,
                entry_price, buy_lower, buy_upper, stop_price, target_1, target_2, qty,
                risk_per_share, reward_to_target_1, reward_to_target_2, rr_ratio_1, rr_ratio_2,
                order_id, order_status, filled_avg_price, filled_qty, outcome, notes, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                now,
                now,
                trade.get('scan_id'),
                trade['symbol'],
                trade.get('side', 'buy'),
                trade.get('decision', 'BUY NOW'),
                trade.get('score_total'),
                trade.get('current_price'),
                trade['entry_price'],
                trade.get('buy_lower'),
                trade.get('buy_upper'),
                trade['stop_price'],
                trade['target_1'],
                trade['target_2'],
                trade.get('qty'),
                trade.get('risk_per_share'),
                trade.get('reward_to_target_1'),
                trade.get('reward_to_target_2'),
                trade.get('rr_ratio_1'),
                trade.get('rr_ratio_2'),
                trade.get('order_id'),
                trade.get('order_status'),
                trade.get('filled_avg_price'),
                trade.get('filled_qty'),
                trade.get('outcome'),
                trade.get('notes'),
                json.dumps(trade.get('raw_json', {})),
            ),
        )
        return int(cur.lastrowid)


def update_trade_status(order_id: str, updates: Dict[str, Any]) -> None:
    fields = []
    values = []
    allowed = {
        'order_status', 'filled_avg_price', 'filled_qty', 'outcome', 'notes', 'raw_json',
        'current_price', 'entry_price', 'stop_price', 'target_1', 'target_2', 'qty'
    }
    for key, value in updates.items():
        if key not in allowed:
            continue
        if key == 'raw_json':
            value = json.dumps(value)
        fields.append(f"{key} = ?")
        values.append(value)
    if not fields:
        return
    fields.append('updated_at = ?')
    values.append(utc_now())
    values.append(order_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE trades SET {', '.join(fields)} WHERE order_id = ?", values)


def get_recent_scans(limit: int = 10) -> Iterable[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            'SELECT id, created_at, market_day, best_symbol, best_decision, best_score FROM scans ORDER BY id DESC LIMIT ?',
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_recent_trades(limit: int = 20) -> Iterable[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            '''
            SELECT id, created_at, symbol, decision, score_total, qty, entry_price, stop_price,
                   target_1, target_2, order_id, order_status, filled_avg_price, filled_qty, outcome
            FROM trades ORDER BY id DESC LIMIT ?
            ''',
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_trade_by_order_id(order_id: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute('SELECT * FROM trades WHERE order_id = ? ORDER BY id DESC LIMIT 1', (order_id,)).fetchone()
        return dict(row) if row else None



def insert_operator_action(action: str, reason: str | None = None, success: bool = True, details: Dict[str, Any] | None = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO operator_actions (created_at, action, reason, success, details_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (utc_now(), action, reason, 1 if success else 0, json.dumps(details or {})),
        )
        return int(cur.lastrowid)


def get_recent_operator_actions(limit: int = 50) -> Iterable[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, created_at, action, reason, success, details_json
            FROM operator_actions
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    actions = []
    for row in rows:
        item = dict(row)
        raw = item.get('details_json')
        if isinstance(raw, str):
            try:
                item['details'] = json.loads(raw)
            except json.JSONDecodeError:
                item['details'] = {}
        else:
            item['details'] = raw or {}
        item['success'] = bool(item.get('success'))
        actions.append(item)
    return actions


def get_trade_by_target1_id(target_1_id: str) -> Optional[Dict[str, Any]]:
    """Finds a trade based on its Target 1 order ID stored in raw_json."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM trades
            WHERE json_extract(raw_json, '$.order_bundle.target_1_order_id') = ?
            ORDER BY id DESC LIMIT 1
            """,
            (target_1_id,),
        ).fetchone()
        return dict(row) if row else None


def _created_at_to_et_date(created_at: str) -> str:
    dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(config.TIMEZONE_LABEL)).date().isoformat()


def is_trade_on_et_date(created_at: str, et_date: str) -> bool:
    return _created_at_to_et_date(created_at) == et_date


def get_active_trades(limit: int = 100) -> Iterable[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM trades
            WHERE outcome IS NULL OR outcome IN ('open', 'working_or_filled', 'partial_win', 'breakeven_or_small_win')
            ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def count_trades_today(symbol: str | None = None, source: str | None = None) -> int:
    with get_conn() as conn:
        rows = conn.execute('SELECT created_at, symbol, raw_json FROM trades ORDER BY id DESC LIMIT 1000').fetchall()
    day = today_et_prefix()
    count = 0
    for row in rows:
        trade = dict(row)
        if not is_trade_on_et_date(trade['created_at'], day):
            continue
        if symbol and trade.get('symbol') != symbol:
            continue
        if source:
            raw = trade.get('raw_json') or '{}'
            raw = json.loads(raw) if isinstance(raw, str) else raw
            if (raw.get('source') or raw.get('execution_request', {}).get('source')) != source:
                continue
        count += 1
    return count


def get_trade_by_symbol_today(symbol: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute('SELECT * FROM trades WHERE symbol = ? ORDER BY id DESC LIMIT 200', (symbol,)).fetchall()
    day = today_et_prefix()
    for row in rows:
        rec = dict(row)
        if is_trade_on_et_date(rec['created_at'], day):
            return rec
    return None


def get_failed_trades_today() -> int:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT created_at, outcome
            FROM trades
            WHERE outcome IN ('loss', 'stopped_out', 'rejected', 'failed')
            ORDER BY id DESC LIMIT 1000
            """
        ).fetchall()
    day = today_et_prefix()
    return sum(1 for row in rows if is_trade_on_et_date(row['created_at'], day))
