import sqlite3

import app
import db
import execution


def _use_temp_db(tmp_path, monkeypatch):
    path = tmp_path / 'test.db'
    monkeypatch.setattr(app.config, 'DB_PATH', str(path))
    monkeypatch.setattr(db.config, 'DB_PATH', str(path))
    db.init_db()
    return path


def test_runtime_kv_create_and_roundtrip(tmp_path, monkeypatch):
    path = _use_temp_db(tmp_path, monkeypatch)
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='runtime_kv'").fetchone()
    assert row is not None
    db.set_runtime_value('k1', {'ok': True, 'api_key': 'abc'})
    got = db.get_runtime_value('k1', default={})
    assert got.get('ok') is True
    assert got.get('api_key') == '[redacted]'
    assert db.get_runtime_value('missing', default={'x': 1}) == {'x': 1}
    db.set_runtime_value('k2', {'a': 1})
    multi = db.get_runtime_values(['k1', 'k2', 'missing'])
    assert set(multi.keys()) == {'k1', 'k2'}


def test_durable_pipeline_used_when_runtime_empty(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    execution.RUNTIME_STATE.clear()
    db.set_runtime_value('last_pre_market_readiness_pipeline', {
        'overall_status': 'PASS',
        'safe_to_enable_auto_cycle': True,
        'safe_to_run_manual_auto_cycle': False,
        'go_no_go': 'WAIT_FOR_MARKET_OPEN',
        'next_required_action': 'wait_for_market_open',
        'blocking_reasons': ['market_closed'],
    })
    gate = app.build_paper_market_launch_gate()
    assert gate.get('launch_gate_status') != 'BLOCKED_READINESS'
    assert 'missing_pre_market_pipeline' not in (gate.get('blocking_reasons') or [])


def test_bot_status_readiness_debug_uses_durable(tmp_path, monkeypatch, client):
    _use_temp_db(tmp_path, monkeypatch)
    execution.RUNTIME_STATE.clear()
    db.set_runtime_value('last_pre_market_readiness_pipeline', {'overall_status': 'PASS', 'safe_to_enable_auto_cycle': True})
    resp = client.get('/api/bot-status')
    payload = resp.get_json().get('data', {})
    debug = payload.get('readiness_debug', {})
    assert (debug.get('last_pre_market_readiness_pipeline') or {}).get('overall_status') == 'PASS'
