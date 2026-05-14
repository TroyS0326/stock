import app
import db
import config
import execution_service


def test_checker_exception_fails_closed(monkeypatch):
    execution_service.set_unprotected_position_checker(lambda: (_ for _ in ()).throw(RuntimeError('x')))
    blocked, symbols, compact = execution_service.has_unprotected_open_position()
    assert blocked is True
    assert symbols == ['UNKNOWN']
    assert compact.get('error') == 'unprotected_position_check_failed'


def test_validate_auto_blocks_on_checker_exception(monkeypatch):
    execution_service.set_unprotected_position_checker(lambda: (_ for _ in ()).throw(RuntimeError('x')))
    monkeypatch.setattr(execution_service, 'count_trades_today', lambda **kwargs: 0)
    monkeypatch.setattr(execution_service, 'estimated_daily_loss_risk_used_today', lambda: 0)
    monkeypatch.setattr(execution_service, 'get_failed_trades_today', lambda: 0)
    monkeypatch.setattr(execution_service, 'get_trade_by_symbol_today', lambda symbol: None)
    monkeypatch.setattr(execution_service, 'has_active_user_symbol_trade', lambda u,s: False)
    monkeypatch.setattr(execution_service, 'has_active_symbol_exposure', lambda s: False)
    monkeypatch.setattr(execution_service, 'buy_window_open', lambda: True)
    monkeypatch.setattr(execution_service, 'within_auto_scan_window', lambda: True)
    monkeypatch.setattr(execution_service, 'get_runtime_trade_blocks', lambda: [])
    c = {"symbol":"XYZ","setup_grade":"A","decision":"BUY NOW","score_total":35,"scores":{"catalyst":3},"details":{"spread_pct":0.002,"entry_trigger":"ORB_BREAKOUT","momentum_continuation":True},"current_price":10,"entry_price":10,"stop_price":9,"target_1":10.5,"target_2":11,"buy_lower":9.8,"buy_upper":10.2,"qty":2,"hard_reject_reasons":[],"why_not_buying":[]}
    v = execution_service.validate_trade_candidate(c, auto=True)
    assert not v['ok']
    assert 'unprotected_open_position' in v['skip_reasons']
    assert 'unprotected_position_check_failed' in v['skip_reasons']


def test_reconciliation_counts_filled_open_outcome(monkeypatch, tmp_path):
    p = tmp_path / 'recon.sqlite'
    monkeypatch.setattr(config, 'DB_PATH', str(p), raising=False)
    monkeypatch.setattr(db, 'DB_PATH', str(p), raising=False)
    db.init_db()
    db.insert_trade({'symbol':'VNET','qty':1,'entry_price':10,'stop_price':9,'target_1':11,'target_2':12,'order_id':'o1','order_status':'filled','filled_avg_price':10,'filled_qty':1,'outcome':'open','notes':'n','raw_json':{}})
    monkeypatch.setattr(app, 'get_open_positions', lambda: [{'symbol':'VNET','qty':'1','side':'long'}])
    monkeypatch.setattr(app, 'get_open_orders', lambda: [])
    rec = app.build_paper_position_reconciliation()
    assert rec['db_open_trades_count'] == 1
    assert rec['reconciliation_status'] == 'FAIL_UNPROTECTED_POSITION'
    assert 'VNET' in rec['unsafe_protection_symbols']


def test_position_protection_target_only_is_partial(monkeypatch):
    monkeypatch.setattr(app, 'get_open_positions', lambda: [{'symbol': 'VNET', 'qty': '1', 'side': 'long'}])
    monkeypatch.setattr(app, 'get_open_orders', lambda: [{'symbol':'VNET','side':'sell','type':'limit','status':'open','qty':'1'}])
    audit = app.build_position_protection_audit()
    assert audit['positions'][0]['protection_status'] == 'PARTIAL'
    assert audit['unprotected_position_detected'] is True
    assert 'VNET' in audit['unsafe_protection_symbols']
    assert 'stop_or_trailing' in audit['positions'][0]['missing_protection']


def test_position_protection_close_pending_unprotected(monkeypatch):
    monkeypatch.setattr(app, 'get_open_positions', lambda: [{'symbol': 'VNET', 'qty': '1', 'side': 'long'}])
    monkeypatch.setattr(app, 'get_open_orders', lambda: [{'id': 'o1', 'symbol': 'VNET', 'side': 'sell', 'type': 'market', 'status': 'accepted', 'qty': '1'}])
    audit = app.build_position_protection_audit()
    p = audit['positions'][0]
    assert p['protection_status'] == 'CLOSE_PENDING_UNPROTECTED'
    assert p['has_close_order_pending'] is True
    assert 'VNET' in audit['close_pending_symbols']
    assert audit['next_action_hint'] == 'wait_for_close_order_fill'


def test_position_protection_stop_is_protected(monkeypatch):
    monkeypatch.setattr(app, 'get_open_positions', lambda: [{'symbol': 'ABC', 'qty': '2', 'side': 'long'}])
    monkeypatch.setattr(app, 'get_open_orders', lambda: [{'symbol': 'ABC', 'side': 'sell', 'type': 'stop', 'status': 'open', 'qty': '2'}])
    assert app.build_position_protection_audit()['positions'][0]['protection_status'] == 'PROTECTED'


def test_reconciliation_includes_stale_and_close_pending(monkeypatch, tmp_path):
    p = tmp_path / 'recon2.sqlite'
    monkeypatch.setattr(config, 'DB_PATH', str(p), raising=False)
    monkeypatch.setattr(db, 'DB_PATH', str(p), raising=False)
    db.init_db()
    db.insert_trade({'symbol': 'AGBK', 'qty': 1, 'entry_price': 1, 'stop_price': 0.9, 'target_1': 1.1, 'target_2': 1.2, 'order_id': 'a1', 'order_status': 'filled', 'filled_avg_price': 1, 'filled_qty': 1, 'outcome': 'open', 'notes': 'n', 'raw_json': {}})
    monkeypatch.setattr(app, 'get_open_positions', lambda: [{'symbol': 'VNET', 'qty': '1', 'side': 'long'}])
    monkeypatch.setattr(app, 'get_open_orders', lambda: [{'id': 'o1', 'symbol': 'VNET', 'side': 'sell', 'type': 'market', 'status': 'accepted', 'qty': '1'}])
    rec = app.build_paper_position_reconciliation()
    assert rec['close_pending_symbols'] == ['VNET']
    assert 'VNET' in rec['unsafe_protection_symbols']
    assert 'AGBK' in rec['stale_open_db_trades']


def test_stale_cleanup_plan_read_only(monkeypatch, tmp_path):
    p = tmp_path / 'recon3.sqlite'
    monkeypatch.setattr(config, 'DB_PATH', str(p), raising=False)
    monkeypatch.setattr(db, 'DB_PATH', str(p), raising=False)
    monkeypatch.setattr(app.db, 'DB_PATH', str(p), raising=False)
    db.init_db()
    db.insert_trade({'symbol': 'NIO', 'qty': 1, 'entry_price': 1, 'stop_price': 0.9, 'target_1': 1.1, 'target_2': 1.2, 'order_id': 'a1', 'order_status': 'filled', 'filled_avg_price': 1, 'filled_qty': 1, 'outcome': 'open', 'notes': 'n', 'raw_json': {}})
    monkeypatch.setattr(app, 'get_open_positions', lambda: [])
    monkeypatch.setattr(app, 'get_open_orders', lambda: [])
    plan = app.build_stale_db_trade_cleanup_plan()
    assert plan['stale_count'] >= 1
    assert 'NIO' in [x['symbol'] for x in plan['stale_trades']]
    assert plan['recommended_updates'][0]['recommended_outcome'] == 'broker_position_missing'
