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
    assert 'api_key' not in rows[0]['compact_json']


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


def test_heartbeat_and_attempt_endpoints_no_order(monkeypatch, tmp_path):
    app, db = _reload(monkeypatch, tmp_path)
    db.insert_auto_cycle_attempt({'cycle_id': 'cycle_2', 'source': 'scheduled_auto_cycle', 'status': 'failed', 'execution_error': 'x'})
    called = {'n': 0}
    monkeypatch.setattr(app, 'execute_trade_candidate', lambda *_a, **_k: (_ for _ in ()).throw(AssertionError('should not call execute')))
    client = app.app.test_client()
    r1 = client.get('/api/market-session-heartbeat')
    assert r1.status_code == 200
    assert r1.get_json()['ok']
    r2 = client.get('/api/auto-cycle-attempts', query_string={'limit': 1})
    assert r2.status_code == 200
    assert len(r2.get_json()['data']['items']) == 1
