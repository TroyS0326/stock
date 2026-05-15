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


def test_checklist_uses_durable_preflight_and_synthetic(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    execution.RUNTIME_STATE.clear()
    db.set_runtime_value('last_paper_readiness_preflight', {'ok': True, 'overall_status': 'PASS'})
    db.set_runtime_value('last_paper_readiness_preflight_at', {'value': '2026-05-15T00:00:00Z'})
    db.set_runtime_value('last_synthetic_rehearsal', {'would_attempt_trade': True})
    db.set_runtime_value('last_synthetic_rehearsal_at', {'value': '2026-05-15T00:00:00Z'})
    data = app.build_deployment_checklist()
    assert data['paper_readiness_preflight_recent'] is True
    assert data['synthetic_rehearsal_recent'] is True


def test_bot_status_includes_durable_protection_and_reconciliation(tmp_path, monkeypatch, client):
    _use_temp_db(tmp_path, monkeypatch)
    execution.RUNTIME_STATE.clear()
    db.set_runtime_value('last_position_protection_audit', {'protection_status': 'PASS', 'unsafe_protection_symbols': []})
    db.set_runtime_value('last_position_protection_audit_at', {'value': '2026-05-15T00:00:00Z'})
    db.set_runtime_value('last_paper_position_reconciliation', {'reconciliation_status': 'PASS', 'unsafe_protection_symbols': []})
    db.set_runtime_value('last_paper_position_reconciliation_at', {'value': '2026-05-15T00:00:00Z'})
    payload = client.get('/api/bot-status').get_json().get('data', {})
    debug = payload.get('readiness_debug', {})
    assert (debug.get('last_position_protection_audit') or {}).get('protection_status') == 'PASS'
    assert (payload.get('last_paper_position_reconciliation') or {}).get('reconciliation_status') == 'PASS'


def test_runtime_entry_and_entries_include_updated_at(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    db.set_runtime_value('k3', {'x': 1})
    entry = db.get_runtime_entry('k3')
    assert entry['key'] == 'k3'
    assert entry['value']['x'] == 1
    assert entry.get('updated_at')
    entries = db.get_runtime_entries(['k3', 'missing'])
    assert 'k3' in entries
    assert entries['k3']['value']['x'] == 1
    assert entries['k3'].get('updated_at')


def test_bot_status_prefers_newer_durable_over_stale_memory(tmp_path, monkeypatch, client):
    _use_temp_db(tmp_path, monkeypatch)
    execution.RUNTIME_STATE.clear()
    execution.RUNTIME_STATE['last_position_protection_audit'] = {'protection_status': 'FAIL', 'unsafe_protection_symbols': ['VNET'], 'next_action_hint': 'review'}
    execution.RUNTIME_STATE['last_position_protection_audit_at'] = '2026-05-14T00:00:00Z'
    db.set_runtime_value('last_position_protection_audit', {'protection_status': 'PASS', 'status': 'PASS', 'unsafe_protection_symbols': [], 'next_action_hint': 'no_positions'})
    db.set_runtime_value('last_position_protection_audit_at', {'value': '2026-05-15T00:00:00Z'})
    payload = client.get('/api/bot-status').get_json().get('data', {})
    debug = payload.get('readiness_debug', {})
    audit = debug.get('last_position_protection_audit') or {}
    assert audit.get('protection_status') == 'PASS'
    assert audit.get('next_action_hint') == 'no_positions'


def test_launch_gate_prefers_newer_durable_pipeline(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    execution.RUNTIME_STATE.clear()
    execution.RUNTIME_STATE['last_pre_market_readiness_pipeline'] = None
    execution.RUNTIME_STATE['last_pre_market_readiness_pipeline_at'] = '2026-05-14T00:00:00Z'
    db.set_runtime_value('last_pre_market_readiness_pipeline', {
        'overall_status': 'PASS',
        'safe_to_enable_auto_cycle': True,
        'safe_to_run_manual_auto_cycle': False,
        'go_no_go': 'WAIT_FOR_MARKET_OPEN',
        'next_required_action': 'wait_for_market_open',
        'blocking_reasons': ['market_closed'],
    })
    db.set_runtime_value('last_pre_market_readiness_pipeline_at', {'value': '2026-05-15T00:00:00Z'})
    gate = app.build_paper_market_launch_gate()
    assert 'missing_pre_market_pipeline' not in (gate.get('blocking_reasons') or [])


def test_launch_gate_prefers_newer_memory_pipeline_over_older_durable(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    execution.RUNTIME_STATE.clear()
    execution.RUNTIME_STATE['last_pre_market_readiness_pipeline'] = {'overall_status': 'PASS', 'safe_to_enable_auto_cycle': True}
    execution.RUNTIME_STATE['last_pre_market_readiness_pipeline_at'] = '2026-05-15T00:00:00Z'
    db.set_runtime_value('last_pre_market_readiness_pipeline', {'overall_status': 'FAIL', 'safe_to_enable_auto_cycle': False})
    db.set_runtime_value('last_pre_market_readiness_pipeline_at', {'value': '2026-05-14T00:00:00Z'})
    gate = app.build_paper_market_launch_gate()
    assert gate.get('launch_gate_status') != 'BLOCKED_READINESS'


def test_position_protection_audit_persists_unsafe_protection_symbols(tmp_path, monkeypatch, client):
    _use_temp_db(tmp_path, monkeypatch)
    execution.RUNTIME_STATE.clear()
    monkeypatch.setattr(app, 'build_position_protection_audit', lambda: {
        'generated_at': '2026-05-15T00:00:00Z',
        'status': 'FAIL',
        'protection_status': 'FAIL',
        'open_positions_count': 1,
        'open_orders_count': 1,
        'unprotected_position_detected': True,
        'unsafe_protection_symbols': ['VNET'],
        'unprotected_symbols': ['VNET'],
        'partial_symbols': [],
        'next_action_hint': 'add_protection',
        'summary_reason': 'unprotected_open_position',
        'ok': False,
    })
    resp = client.get('/api/position-protection-audit')
    assert resp.status_code == 200
    durable = db.get_runtime_value('last_position_protection_audit', default={})
    assert durable.get('unsafe_protection_symbols') == ['VNET']
