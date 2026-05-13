import importlib


def _reload(monkeypatch, tmp_path):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'test.db'))
    import config, db, app
    importlib.reload(config)
    importlib.reload(db)
    importlib.reload(app)
    app.config.DB_PATH = str(tmp_path / 'test.db')
    db.config.DB_PATH = str(tmp_path / 'test.db')
    return app, db


def test_db_auto_cycle_attempt_roundtrip_and_sanitize(monkeypatch, tmp_path):
    app, db = _reload(monkeypatch, tmp_path)
    row_id = db.insert_auto_cycle_attempt({'cycle_id': 'cycle_1', 'source': 'auto_cycle_plan', 'status': 'planned', 'compact_json': {'api_key': 'secret', 'symbol': 'TEST'}})
    assert row_id > 0
    rows = list(db.get_recent_auto_cycle_attempts(1))
    assert rows[0]['cycle_id'] == 'cycle_1'
    assert rows[0]['compact_json']['api_key'] == '[redacted]'


def test_db_auto_cycle_attempt_redacts_sensitive_text_fields(monkeypatch, tmp_path):
    app, db = _reload(monkeypatch, tmp_path)
    sensitive_key = app.config.ALPACA_API_KEY
    sensitive_secret = app.config.ALPACA_API_SECRET
    db.insert_auto_cycle_attempt({
        'cycle_id': 'cycle_2',
        'source': 'scheduled_auto_cycle',
        'status': 'failed',
        'compact_json': {'authorization_header': f'Bearer {sensitive_key}'},
        'execution_error': f'failed with key={sensitive_key} secret={sensitive_secret}',
        'skip_reasons': [f'token={sensitive_key}', {'password': sensitive_secret}],
        'top_blockers': {'api_secret': sensitive_secret, 'reason': f'bearer {sensitive_key}'},
    })
    row = list(db.get_recent_auto_cycle_attempts(1))[0]
    assert sensitive_key not in row['execution_error']
    assert sensitive_secret not in row['execution_error']
    assert row['top_blockers_json']['api_secret'] == '[redacted]'
    assert '[redacted]' in row['skip_reasons_json'][1]['password']
    assert sensitive_key not in str(row)


def test_run_scan_market_closed_records_skipped(monkeypatch, tmp_path):
    app, db = _reload(monkeypatch, tmp_path)
    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (False, 'market_closed'))
    app.run_scan_and_maybe_auto_trade()
    rows = list(db.get_recent_auto_cycle_attempts(1))
    assert rows[0]['status'] == 'skipped'


def test_auto_cycle_executed_records_fields(monkeypatch, tmp_path):
    app, db = _reload(monkeypatch, tmp_path)
    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (True, 'market_open'))
    monkeypatch.setattr(app, 'run_scan', lambda: {'best_pick': {'symbol': 'ABC'}, 'watchlist': []})
    monkeypatch.setattr(app, 'insert_scan', lambda *_: 1)
    monkeypatch.setattr(app.watchlist_manager, 'set_items', lambda *_: None)
    monkeypatch.setattr(app, 'build_auto_trade_candidate_plan', lambda *_args, **_kwargs: {'candidate_count': 1, 'executable_count': 1, 'top_blockers': {}, 'attempt_plan': [{'candidate': {'symbol': 'ABC', 'qty': 3}, 'verdict': {'ok': True, 'probe_trade': True, 'first_trade_governor_applied': True, 'first_trade_final_qty': 2, 'first_trade_risk_dollars': 4.5, 'skip_reasons': []}}]})
    monkeypatch.setattr(app, 'execute_trade_candidate', lambda *_a, **_k: {'order': {'id': 'o1', 'status': 'accepted', 'filled_qty': 2}})
    app.run_scan_and_maybe_auto_trade()
    row = list(db.get_recent_auto_cycle_attempts(1))[0]
    assert row['status'] == 'executed'
    assert row['attempted_symbol'] == 'ABC'
    assert row['attempted_qty'] == 2


def test_heartbeat_classification_for_executed_attempts(monkeypatch, tmp_path):
    app, db = _reload(monkeypatch, tmp_path)
    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (True, 'market_open'))
    db.insert_auto_cycle_attempt({'cycle_id': 'c1', 'source': 'scheduled_auto_cycle', 'status': 'executed'})

    monkeypatch.setattr(app, 'build_position_protection_audit', lambda: {'status': 'PASS', 'open_positions_count': 0})
    hb = app.build_market_session_heartbeat()
    assert hb['next_action_hint'] == 'ready_for_next_auto_cycle'

    monkeypatch.setattr(app, 'build_position_protection_audit', lambda: {'status': 'PASS', 'open_positions_count': 1})
    hb = app.build_market_session_heartbeat()
    assert hb['heartbeat_status'] == 'POSITION_OPEN_PROTECTED'
    assert hb['next_action_hint'] == 'monitor_open_trade'

    monkeypatch.setattr(app, 'build_position_protection_audit', lambda: {'status': 'FAIL', 'open_positions_count': 1})
    hb = app.build_market_session_heartbeat()
    assert hb['heartbeat_status'] == 'POSITION_OPEN_UNPROTECTED'
    assert hb['next_action_hint'] == 'review_unprotected_position'


def test_heartbeat_silence_detection_market_open_vs_closed(monkeypatch, tmp_path):
    app, _db = _reload(monkeypatch, tmp_path)
    monkeypatch.setattr(app, 'get_runtime_state', lambda: {'scheduler_running': True, 'auto_scan_job_registered': True})
    monkeypatch.setattr(app, 'get_recent_auto_cycle_attempts', lambda limit=10: [])
    monkeypatch.setattr(app.config, 'AUTO_CYCLE_REQUIRE_MARKET_OPEN', True)

    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (False, 'market_closed'))
    hb_closed = app.build_market_session_heartbeat()
    assert hb_closed['silence_detection']['likely_silent_failure'] is False

    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (True, 'market_open'))
    hb_open = app.build_market_session_heartbeat()
    assert hb_open['silence_detection']['likely_silent_failure'] is True


def test_heartbeat_and_attempt_endpoints_no_order(monkeypatch, tmp_path):
    app, db = _reload(monkeypatch, tmp_path)
    db.insert_auto_cycle_attempt({'cycle_id': 'cycle_2', 'source': 'scheduled_auto_cycle', 'status': 'failed', 'execution_error': 'x'})

    def _raise(*_a, **_k):
        raise AssertionError('should not call trading/scanning functions')

    monkeypatch.setattr(app, 'execute_trade_candidate', _raise)
    monkeypatch.setattr(app, 'place_managed_entry_order', _raise, raising=False)
    monkeypatch.setattr(app, 'submit_order', _raise, raising=False)
    monkeypatch.setattr(app, 'cancel_order', _raise, raising=False)
    monkeypatch.setattr(app, 'close_position', _raise, raising=False)
    monkeypatch.setattr(app, 'submit_market_sell', _raise, raising=False)
    monkeypatch.setattr(app, 'run_scan', _raise)

    client = app.app.test_client()
    r1 = client.get('/api/market-session-heartbeat')
    assert r1.status_code == 200
    assert r1.get_json()['ok']
    r2 = client.get('/api/auto-cycle-attempts', query_string={'limit': 1})
    assert r2.status_code == 200
    assert len(r2.get_json()['data']['items']) == 1
