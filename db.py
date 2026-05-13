import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional
from zoneinfo import ZoneInfo

import config



SENSITIVE_KEYWORDS = ('api_key', 'secret', 'token', 'password', 'authorization', 'bearer', 'account')
MAX_REDACTED_TEXT_LEN = 500


def _truncate_text(value: str, max_len: int = MAX_REDACTED_TEXT_LEN) -> str:
    text = value if isinstance(value, str) else str(value)
    return text[:max_len]


def _redact_sensitive_string(value: str) -> str:
    redacted = str(value)
    secrets = [getattr(config, 'ALPACA_API_KEY', None), getattr(config, 'ALPACA_API_SECRET', None)]
    for secret in secrets:
        if secret:
            redacted = redacted.replace(str(secret), '[redacted]')
    return _truncate_text(redacted)


def _is_sensitive_key(key: Any) -> bool:
    lk = str(key).lower()
    return any(word in lk for word in SENSITIVE_KEYWORDS)


def _redact_sensitive_text(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if _is_sensitive_key(key):
                out[key] = '[redacted]'
            else:
                out[key] = _redact_sensitive_text(item)
        return out
    if isinstance(value, list):
        return [_redact_sensitive_text(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact_sensitive_text(v) for v in value)
    if isinstance(value, str):
        return _redact_sensitive_string(value)
    return value

AUTO_CYCLE_ATTEMPT_COLUMNS = (
    'created_at', 'cycle_id', 'source', 'status', 'market_reason', 'candidate_count', 'executable_count',
    'attempted_symbol', 'attempted_qty', 'probe_trade', 'first_trade_governor_applied', 'first_trade_final_qty',
    'first_trade_risk_dollars', 'top_blockers_json', 'skip_reasons_json', 'execution_error', 'compact_json',
)


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
            CREATE TABLE IF NOT EXISTS auto_cycle_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                cycle_id TEXT NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                market_reason TEXT,
                candidate_count INTEGER,
                executable_count INTEGER,
                attempted_symbol TEXT,
                attempted_qty INTEGER,
                probe_trade INTEGER,
                first_trade_governor_applied INTEGER,
                first_trade_final_qty INTEGER,
                first_trade_risk_dollars REAL,
                top_blockers_json TEXT,
                skip_reasons_json TEXT,
                execution_error TEXT,
                compact_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_scans_created_at ON scans(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_trades_created_at ON trades(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_trades_order_id ON trades(order_id);
            CREATE INDEX IF NOT EXISTS idx_auto_cycle_attempts_created_at ON auto_cycle_attempts(created_at DESC);
            '''
        )


def _ensure_auto_cycle_attempts_table() -> None:
    with get_conn() as conn:
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS auto_cycle_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                cycle_id TEXT NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                market_reason TEXT,
                candidate_count INTEGER,
                executable_count INTEGER,
                attempted_symbol TEXT,
                attempted_qty INTEGER,
                probe_trade INTEGER,
                first_trade_governor_applied INTEGER,
                first_trade_final_qty INTEGER,
                first_trade_risk_dollars REAL,
                top_blockers_json TEXT,
                skip_reasons_json TEXT,
                execution_error TEXT,
                compact_json TEXT
            )
            '''
        )


def _sanitize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw = dict(payload or {})
    compact = _redact_sensitive_text(raw.get('compact_json') or {})
    top_blockers = _redact_sensitive_text(raw.get('top_blockers_json') or raw.get('top_blockers') or {})
    skip_reasons = _redact_sensitive_text(raw.get('skip_reasons_json') or raw.get('skip_reasons') or [])
    execution_error = _redact_sensitive_text(str(raw.get('execution_error') or ''))
    market_reason = _redact_sensitive_text(raw.get('market_reason'))
    return {
        'created_at': utc_now(),
        'cycle_id': str(raw.get('cycle_id') or ''),
        'source': str(raw.get('source') or ''),
        'status': str(raw.get('status') or ''),
        'market_reason': market_reason,
        'candidate_count': int(raw.get('candidate_count') or 0),
        'executable_count': int(raw.get('executable_count') or 0),
        'attempted_symbol': (raw.get('attempted_symbol') or None),
        'attempted_qty': int(raw.get('attempted_qty') or 0) if raw.get('attempted_qty') is not None else None,
        'probe_trade': 1 if bool(raw.get('probe_trade')) else 0,
        'first_trade_governor_applied': 1 if bool(raw.get('first_trade_governor_applied')) else 0,
        'first_trade_final_qty': int(raw.get('first_trade_final_qty') or 0) if raw.get('first_trade_final_qty') is not None else None,
        'first_trade_risk_dollars': float(raw.get('first_trade_risk_dollars') or 0.0) if raw.get('first_trade_risk_dollars') is not None else None,
        'top_blockers_json': json.dumps(top_blockers),
        'skip_reasons_json': json.dumps(skip_reasons),
        'execution_error': execution_error,
        'compact_json': json.dumps(compact),
    }


def insert_auto_cycle_attempt(payload: Dict[str, Any]) -> int:
    _ensure_auto_cycle_attempts_table()
    clean = _sanitize_payload(payload)
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO auto_cycle_attempts ({', '.join(AUTO_CYCLE_ATTEMPT_COLUMNS)}) VALUES ({', '.join(['?']*len(AUTO_CYCLE_ATTEMPT_COLUMNS))})",
            tuple(clean[c] for c in AUTO_CYCLE_ATTEMPT_COLUMNS),
        )
        return int(cur.lastrowid)


def get_recent_auto_cycle_attempts(limit: int = 20) -> Iterable[Dict[str, Any]]:
    _ensure_auto_cycle_attempts_table()
    lim = max(1, min(int(limit or 20), 100))
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM auto_cycle_attempts ORDER BY id DESC LIMIT ?", (lim,)).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        for k in ['top_blockers_json', 'skip_reasons_json', 'compact_json']:
            try:
                item[k] = json.loads(item.get(k) or ('{}' if 'blockers' in k or 'compact' in k else '[]'))
            except Exception:
                item[k] = {} if 'blockers' in k or 'compact' in k else []
        out.append(item)
    return out


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



def has_active_user_symbol_trade(user_id: int | None, symbol: str) -> bool:
    sym = (symbol or '').upper().strip()
    if not sym:
        return False
    try:
        uid = int(user_id)
    except Exception:
        return False

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT raw_json FROM trades
            WHERE symbol = ?
              AND (outcome IS NULL OR outcome IN ('open', 'working_or_filled', 'partial_win', 'breakeven_or_small_win'))
            ORDER BY id DESC LIMIT 500
            """,
            (sym,),
        ).fetchall()

    for row in rows:
        raw = row['raw_json'] if isinstance(row, sqlite3.Row) else row[0]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = {}
        raw = raw or {}
        req = raw.get('execution_request') if isinstance(raw, dict) else {}
        req = req if isinstance(req, dict) else {}
        raw_uid = req.get('user_id', raw.get('user_id') if isinstance(raw, dict) else None)
        try:
            if raw_uid is not None and int(raw_uid) == uid:
                return True
        except Exception:
            continue
    return False

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
    try:
        with get_conn() as conn:
            rows = conn.execute('SELECT created_at, symbol, raw_json FROM trades ORDER BY id DESC LIMIT 1000').fetchall()
    except sqlite3.OperationalError:
        return 0
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
    try:
        with get_conn() as conn:
            rows = conn.execute('SELECT * FROM trades WHERE symbol = ? ORDER BY id DESC LIMIT 200', (symbol,)).fetchall()
    except sqlite3.OperationalError:
        return None
    day = today_et_prefix()
    for row in rows:
        rec = dict(row)
        if is_trade_on_et_date(rec['created_at'], day):
            return rec
    return None


def get_failed_trades_today() -> int:
    try:
        with get_conn() as conn:
            rows = conn.execute(
            """
            SELECT created_at, outcome
            FROM trades
            WHERE outcome IN ('loss', 'stopped_out', 'rejected', 'failed')
            ORDER BY id DESC LIMIT 1000
            """
            ).fetchall()
    except sqlite3.OperationalError:
        return 0
    day = today_et_prefix()
    return sum(1 for row in rows if is_trade_on_et_date(row['created_at'], day))


def estimated_daily_loss_risk_used_today() -> float:
    try:
        with get_conn() as conn:
            rows = conn.execute(
            """
            SELECT created_at, outcome, qty, entry_price, stop_price
            FROM trades
            WHERE outcome IN ('loss', 'stopped_out', 'failed')
            ORDER BY id DESC LIMIT 1000
            """
            ).fetchall()
    except sqlite3.OperationalError:
        return 0.0
    day = today_et_prefix()
    total = 0.0
    for row in rows:
        if not is_trade_on_et_date(row['created_at'], day):
            continue
        qty = max(0, int(row['qty'] or 0))
        total += max(0.0, (float(row['entry_price'] or 0) - float(row['stop_price'] or 0)) * qty)
    return round(total, 2)
