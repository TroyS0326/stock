import pytest
import execution_service
import broker
import app
import broker_facade
import sim_broker


def _cand(**kw):
    c = {
        'symbol':'AAA','setup_grade':'A','decision':'BUY NOW','score_total':90,
        'scores':{'catalyst':5},'details':{'spread_pct':0.001,'entry_trigger':'BREAKOUT'},
        'current_price':10.0,'entry_price':10.0,'stop_price':9.5,'target_1':10.5,'target_2':11.0,
        'buy_lower':9.9,'buy_upper':10.2,'qty':1,
    }
    c.update(kw)
    return c


def test_auto_buy_window_gate_bypassed_in_sim(monkeypatch):
    monkeypatch.setattr(execution_service.config, 'SIMULATION_MODE', True)
    monkeypatch.setattr(execution_service.config, 'AUTO_CYCLE_REQUIRE_MARKET_OPEN', True)
    monkeypatch.setattr(execution_service, 'buy_window_open', lambda: False)
    monkeypatch.setattr(execution_service, 'within_auto_scan_window', lambda: False)
    monkeypatch.setattr(execution_service, 'validate_price_risk_fields', lambda c: (True, [], 0.5))
    monkeypatch.setattr(execution_service, 'detect_entry_trigger', lambda c: 'BREAKOUT')
    monkeypatch.setattr(execution_service, 'classify_hard_reject_reasons', lambda c: ([], []))
    monkeypatch.setattr(execution_service, 'has_active_symbol_exposure', lambda s: False)
    monkeypatch.setattr(execution_service, 'has_active_user_symbol_trade', lambda u, s: False)
    monkeypatch.setattr(execution_service, 'get_runtime_trade_blocks', lambda: [])
    monkeypatch.setattr(execution_service, 'count_trades_today', lambda source='auto': 0)
    monkeypatch.setattr(execution_service, 'estimated_daily_loss_risk_used_today', lambda: 0)
    monkeypatch.setattr(execution_service, 'get_failed_trades_today', lambda: 0)
    v = execution_service.validate_trade_candidate(_cand(), auto=True)
    assert 'buy_window_closed' not in v['skip_reasons']


def test_retry_disabled_for_non_paper_endpoint(monkeypatch):
    monkeypatch.setattr(broker, 'ENTRY_RETRY_ENABLED', True)
    monkeypatch.setattr(broker, 'ALPACA_PAPER_BASE', 'https://api.alpaca.markets')
    monkeypatch.setattr(broker, '_pegged_limit_entry', lambda **k: {'id': 'o1'})
    calls = {'n': 0}
    def poll(order_id, timeout):
        calls['n'] += 1
        raise broker.BrokerError('timeout')
    monkeypatch.setattr(broker, '_poll_for_fill', poll)
    with pytest.raises(broker.BrokerError):
        broker.place_managed_entry_order('AAA',1,10,9,11,12)
    assert calls['n'] == 1


def test_run_scan_continues_after_execution_failure(monkeypatch):
    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (True, 'ok'))
    monkeypatch.setattr(app, 'run_scan', lambda: {'best_pick': {'symbol': 'AAA'}, 'watchlist': [{'symbol': 'BBB'}]})
    monkeypatch.setattr(app, 'insert_scan', lambda _r: 1)
    monkeypatch.setattr(app.watchlist_manager, 'set_items', lambda *_: None)
    monkeypatch.setattr(app, 'validate_trade_candidate', lambda c, auto=True: {'ok': True, 'skip_reasons': [], 'entry_trigger': 'BREAKOUT'})
    calls = {'n': 0}
    def exec_cand(c, source='auto'):
        calls['n'] += 1
        if calls['n'] == 1:
            raise app.BrokerError('entry_timeout_after_retry')
        return {'trade_id': 2, 'order': {'id': 'o2', 'status': 'filled'}}
    monkeypatch.setattr(app, 'execute_trade_candidate', exec_cand)
    app.run_scan_and_maybe_auto_trade()
    assert calls['n'] == 2
    assert app.RUNTIME_STATE['last_auto_trade_error'] is None


def test_sim_quote_comes_from_sim_backend(monkeypatch):
    monkeypatch.setattr(broker_facade.config, 'SIMULATION_MODE', True)
    monkeypatch.setattr(sim_broker, 'get_open_positions', lambda: [{'symbol': 'AAA', 'current_price': 50.0, 'avg_entry_price': 49.0}])
    q = broker_facade.get_latest_quote('AAA')
    assert q['ap'] > q['bp'] > 0
