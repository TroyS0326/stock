import app
import db
import config


def _ins(symbol, outcome='open', order_status='filled', notes='n'):
    db.insert_trade({'symbol': symbol, 'qty': 1, 'entry_price': 1, 'stop_price': 0.9, 'target_1': 1.1, 'target_2': 1.2, 'order_id': f'o-{symbol}', 'order_status': order_status, 'filled_avg_price': 1, 'filled_qty': 1, 'outcome': outcome, 'notes': notes, 'raw_json': {}})


def test_cleanup_plan_variants(monkeypatch, tmp_path):
    p = tmp_path / 'cleanup.sqlite'
    monkeypatch.setattr(config, 'DB_PATH', str(p), raising=False)
    monkeypatch.setattr(db, 'DB_PATH', str(p), raising=False)
    monkeypatch.setattr(app.db, 'DB_PATH', str(p), raising=False)
    db.init_db()
    monkeypatch.setattr(app, 'get_open_positions', lambda: [])
    monkeypatch.setattr(app, 'get_open_orders', lambda: [])
    plan = app.build_stale_db_trade_cleanup_plan()
    assert plan['stale_count'] == 0
    assert plan['next_action_hint'] == 'no_stale_db_trades'

    _ins('AGBK', outcome='open')
    _ins('BEKE', outcome='working_or_filled')
    _ins('CCCC', outcome='partial_win')
    _ins('NIO', outcome='breakeven_or_small_win')
    _ins('SAFE', outcome='loss')
    plan = app.build_stale_db_trade_cleanup_plan()
    assert plan['stale_count'] == 4
    assert plan['stale_symbols'] == ['AGBK', 'BEKE', 'CCCC', 'NIO']


def test_cleanup_plan_excludes_broker_matched(monkeypatch, tmp_path):
    p = tmp_path / 'cleanup2.sqlite'
    monkeypatch.setattr(config, 'DB_PATH', str(p), raising=False)
    monkeypatch.setattr(db, 'DB_PATH', str(p), raising=False)
    monkeypatch.setattr(app.db, 'DB_PATH', str(p), raising=False)
    db.init_db()
    _ins('VNET', outcome='open')
    monkeypatch.setattr(app, 'get_open_positions', lambda: [{'symbol': 'VNET', 'qty': '1', 'side': 'long'}])
    monkeypatch.setattr(app, 'get_open_orders', lambda: [])
    assert app.build_stale_db_trade_cleanup_plan()['stale_count'] == 0


def test_apply_endpoint_and_no_broker_mutation(monkeypatch, tmp_path):
    p = tmp_path / 'cleanup3.sqlite'
    monkeypatch.setattr(config, 'DB_PATH', str(p), raising=False)
    monkeypatch.setattr(db, 'DB_PATH', str(p), raising=False)
    monkeypatch.setattr(app.db, 'DB_PATH', str(p), raising=False)
    db.init_db()
    _ins('AGBK', outcome='open', notes='hello')
    _ins('VNET', outcome='open', notes='keep')
    monkeypatch.setattr(app, 'get_open_positions', lambda: [{'symbol': 'VNET', 'qty': '1', 'side': 'long'}])
    monkeypatch.setattr(app, 'get_open_orders', lambda: [])

    forbidden = ['execute_trade_candidate', 'place_managed_entry_order', 'submit_order', 'cancel_order', 'close_position', 'submit_market_sell']
    for name in forbidden:
        if hasattr(app, name):
            monkeypatch.setattr(app, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError(name)))

    c = app.app.test_client()
    assert c.post('/api/stale-db-trade-cleanup-apply', json={}).status_code == 400
    assert c.post('/api/stale-db-trade-cleanup-apply', json={'confirm': 'BAD'}).status_code == 400

    r = c.get('/api/stale-db-trade-cleanup-plan')
    assert r.status_code == 200
    payload = r.get_json()['data']
    assert payload['stale_symbols'] == ['AGBK']

    r = c.post('/api/stale-db-trade-cleanup-apply', json={'confirm': 'MARK_STALE_DB_TRADES'})
    assert r.status_code == 200
    data = r.get_json()['data']
    assert data['updated_count'] == 1
    assert data['updated_symbols'] == ['AGBK']

    with db.get_conn() as conn:
        rows = conn.execute("SELECT symbol, outcome, notes FROM trades ORDER BY symbol").fetchall()
    row_map = {r['symbol']: dict(r) for r in rows}
    assert row_map['AGBK']['outcome'] == 'broker_position_missing'
    assert 'Marked stale by broker/DB reconciliation cleanup.' in row_map['AGBK']['notes']
    assert row_map['VNET']['outcome'] == 'open'


def test_reconciliation_exposes_cleanup_hints(monkeypatch, tmp_path):
    p = tmp_path / 'cleanup4.sqlite'
    monkeypatch.setattr(config, 'DB_PATH', str(p), raising=False)
    monkeypatch.setattr(db, 'DB_PATH', str(p), raising=False)
    monkeypatch.setattr(app.db, 'DB_PATH', str(p), raising=False)
    db.init_db()
    _ins('NIO', outcome='open')
    monkeypatch.setattr(app, 'get_open_positions', lambda: [])
    monkeypatch.setattr(app, 'get_open_orders', lambda: [])
    rec = app.build_paper_position_reconciliation()
    assert rec['stale_db_cleanup_available'] is True
    assert rec['stale_db_cleanup_plan_count'] == 1
    assert rec['next_action_hint'] == 'run_stale_db_trade_cleanup_plan'
