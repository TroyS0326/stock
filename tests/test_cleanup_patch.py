import types, sys
from datetime import datetime, timedelta, timezone

sys.modules.setdefault('dotenv', types.SimpleNamespace(load_dotenv=lambda *a, **k: None))
sys.modules.setdefault('requests', types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None, patch=lambda *a, **k: None, delete=lambda *a, **k: None))

import broker, config, execution, execution_service, app

import pytest

@pytest.fixture(autouse=True)
def _force_not_hard_exit(monkeypatch):
    monkeypatch.setattr(execution.config, "HARD_EXIT_TIME_ET", "23:59")



def test_env_example_has_active_defaults():
    text = open('.env.example', encoding='utf-8').read()
    assert 'AUTO_SCAN_END_ET=15:15' in text
    assert 'ACTIVE_PAPER_TRADING_MODE=1' in text
    assert 'MAX_DOLLAR_LOSS_PER_TRADE=10' in text
    assert 'MIN_CATALYST_SCORE=2' in text


def test_gitignore_protects_env_and_env_example_present():
    text = open('.gitignore', encoding='utf-8').read()
    assert '.env' in text
    assert '*.env' in text
    assert '!.env.example' in text
    assert open('.env.example', encoding='utf-8').read()


def test_auto_scan_end_default_not_from_morning(monkeypatch):
    monkeypatch.setenv('MORNING_SCAN_END_ET', '11:00')
    monkeypatch.delenv('AUTO_SCAN_END_ET', raising=False)
    assert config.AUTO_SCAN_END_ET == '15:15'


def _cand(**kw):
    c={'symbol':'ABC','setup_grade':'A','score_total':40,'decision':'BUY NOW','current_price':1.01,'buy_upper':1.02,'qty':10,'entry_price':1.01,'stop_price':0.99,'target_1':1.03,'target_2':1.04,'details':{'spread_pct':0.001,'momentum_continuation':True}}
    c.update(kw)
    return c


def test_validate_blocks_invalid_risk_for_strict_and_watch(monkeypatch):
    monkeypatch.setattr(execution_service, 'get_failed_trades_today', lambda: 0)
    monkeypatch.setattr(execution_service, 'count_trades_today', lambda **kwargs: 0)
    monkeypatch.setattr(execution_service, 'get_trade_by_symbol_today', lambda symbol: None)
    monkeypatch.setattr(execution_service, 'buy_window_open', lambda: True)
    monkeypatch.setattr(execution_service, 'within_auto_scan_window', lambda: True)
    v1=execution_service.validate_trade_candidate(_cand(stop_price=1.05), auto=True)
    assert 'invalid_risk' in v1['skip_reasons']
    v2=execution_service.validate_trade_candidate(_cand(setup_grade='WATCH', decision='WATCH FOR BREAKOUT', stop_price=1.05), auto=True)
    assert 'invalid_risk' in v2['skip_reasons']


def test_min_auto_grade_blocks_watch(monkeypatch):
    monkeypatch.setattr(execution_service.config, 'MIN_AUTO_SETUP_GRADE', 'A')
    monkeypatch.setattr(execution_service, 'get_failed_trades_today', lambda: 0)
    monkeypatch.setattr(execution_service, 'count_trades_today', lambda **kwargs: 0)
    monkeypatch.setattr(execution_service, 'get_trade_by_symbol_today', lambda symbol: None)
    monkeypatch.setattr(execution_service, 'buy_window_open', lambda: True)
    monkeypatch.setattr(execution_service, 'within_auto_scan_window', lambda: True)
    monkeypatch.setattr(execution_service.config, 'PAPER_PROBE_TRADES_ENABLED', False)
    v=execution_service.validate_trade_candidate(_cand(setup_grade='WATCH', decision='WATCH FOR BREAKOUT'), auto=True)
    assert 'setup_grade_below_min_auto_grade' in v['skip_reasons']


def test_directional_rounding():
    assert broker.round_buy_limit(1.001) == 1.01
    assert broker.round_buy_limit(1.0116) >= 1.02
    assert broker.round_sell_limit(1.009) == 1.0
    assert broker.round_buy_limit(0.12341) == 0.1235


def test_protective_order_qty1_and_failure_flatten(monkeypatch):
    monkeypatch.setattr(broker, '_pegged_limit_entry', lambda **k: {'id': 'e1'})
    monkeypatch.setattr(broker, '_poll_for_fill', lambda *a, **k: {'filled_qty': '1', 'filled_avg_price': '1.0'})
    placed = []
    monkeypatch.setattr(broker, 'submit_stop_sell', lambda s, q, p: placed.append((s, q, p)) or {'id': 'st1'})
    out = broker.place_managed_entry_order('ABC', 1, 1.0, 0.9, 1.1, 1.2)
    assert out['runner_stop_order_id'] == 'st1'
    monkeypatch.setattr(broker, 'submit_stop_sell', lambda *a, **k: (_ for _ in ()).throw(broker.BrokerError('stop fail')))
    flattened = []
    monkeypatch.setattr(broker, 'submit_market_sell', lambda s, q: flattened.append((s, q)) or {'id': 'm1'})
    try:
        broker.place_managed_entry_order('ABC', 1, 1.0, 0.9, 1.1, 1.2)
    except broker.BrokerError:
        pass
    assert flattened == [('ABC', 1)]


def test_auto_exec_error_recorded(monkeypatch):
    monkeypatch.setattr(app, 'within_auto_scan_window', lambda: True)
    monkeypatch.setattr(app, 'market_open_for_auto_cycle', lambda: (True, 'market_open'))
    monkeypatch.setattr(app, 'run_scan', lambda: {'best_pick': _cand(), 'watchlist': []})
    monkeypatch.setattr(app, 'insert_scan', lambda result: 1)
    monkeypatch.setattr(app, 'validate_trade_candidate', lambda c, auto=True: {'ok': True, 'skip_reasons': []})
    monkeypatch.setattr(app, 'execute_trade_candidate', lambda *a, **k: (_ for _ in ()).throw(Exception('boom')))
    app.run_scan_and_maybe_auto_trade()
    assert app.RUNTIME_STATE['last_auto_trade_error'] == 'boom'
    assert app.RUNTIME_STATE['last_auto_trade_skip_reasons'] == ['execution_failed']


def test_stale_position_exit(monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(minutes=200)).isoformat()
    monkeypatch.setattr(execution, 'get_open_positions', lambda:[{'symbol':'ABC','qty':'2','avg_entry_price':'1','current_price':'1'}])
    monkeypatch.setattr(execution, 'get_active_trades', lambda limit:[{'symbol':'ABC','order_id':'o1','created_at':old,'raw_json':{},'entry_price':1}])
    monkeypatch.setattr(execution, 'get_latest_quote', lambda s:{'ap':1})
    calls=[]
    monkeypatch.setattr(execution, 'cancel_open_orders_for_symbol', lambda s: calls.append('cancel'))
    monkeypatch.setattr(execution, 'submit_market_sell', lambda s,q: calls.append(('sell',q)) or {'id':'x'})
    monkeypatch.setattr(execution, 'update_trade_status', lambda *a, **k: None)
    execution.monitor_positions_job()
    assert ('sell',2) in calls


def _setup_quick_profit_trade(monkeypatch):
    monkeypatch.setattr(execution.config, 'QUICK_PROFIT_TAKE_PCT', 1.0)
    monkeypatch.setattr(execution.config, 'BREAKEVEN_TRIGGER_PCT', 1000.0)
    monkeypatch.setattr(execution, 'get_open_positions', lambda: [{'symbol': 'ABC', 'qty': '10', 'avg_entry_price': '1', 'current_price': '1.02'}])
    monkeypatch.setattr(execution, 'get_active_trades', lambda limit: [{
        'symbol': 'ABC',
        'order_id': 'o1',
        'entry_price': 1.0,
        'stop_price': 0.95,
        'raw_json': {'order_bundle': {}},
    }])
    monkeypatch.setattr(execution, 'get_latest_quote', lambda s: {'ap': 1.02})
    monkeypatch.setattr(execution, 'get_open_orders', lambda s: [{'id': 'sell1', 'side': 'sell'}])
    canceled = []
    monkeypatch.setattr(execution, 'cancel_open_orders_for_symbol', lambda s, side='sell': canceled.append((s, side)) or ['sell1'])
    updates = []
    monkeypatch.setattr(execution, 'update_trade_status', lambda order_id, payload: updates.append(payload))
    return canceled, updates


def test_quick_profit_partial_sell_fail_reprotect_success(monkeypatch):
    canceled, updates = _setup_quick_profit_trade(monkeypatch)
    calls = []
    def _sell(symbol, qty):
        calls.append(('sell', qty))
        raise execution.BrokerError('partial sell fail')
    monkeypatch.setattr(execution, 'submit_market_sell', _sell)
    monkeypatch.setattr(execution, 'submit_stop_sell', lambda s, q, p: calls.append(('stop', q, p)) or {'id': 'rest1'})
    execution.RUNTIME_STATE['last_position_monitor_error'] = None
    execution.monitor_positions_job()
    raw = updates[-1]['raw_json']
    assert canceled == [('ABC', 'sell')]
    assert calls[0] == ('sell', 5)
    assert calls[1][0] == 'stop' and calls[1][1] == 10
    assert raw['quick_profit_reprotected_after_partial_sell_failure'] is True
    assert raw['quick_profit_protection_type'] == 'stop_restore'
    assert execution.RUNTIME_STATE['last_position_monitor_error'] == 'partial sell fail'


def test_quick_profit_partial_sell_fail_reprotect_fail_forced_flatten(monkeypatch):
    _, updates = _setup_quick_profit_trade(monkeypatch)
    calls = []
    def _sell(symbol, qty):
        calls.append(('sell', qty))
        if len(calls) == 1:
            raise execution.BrokerError('partial sell fail')
        return {'id': 'flat1'}
    monkeypatch.setattr(execution, 'submit_market_sell', _sell)
    monkeypatch.setattr(execution, 'submit_stop_sell', lambda *a, **k: (_ for _ in ()).throw(execution.BrokerError('restore fail')))
    execution.monitor_positions_job()
    raw = updates[-1]['raw_json']
    assert calls[0] == ('sell', 5)
    assert calls[1] == ('sell', 10)
    assert raw['quick_profit_protection_type'] == 'forced_flatten'
    assert raw['quick_profit_forced_flatten_reason'] == 'partial_sell_failed_and_reprotect_failed'
    assert raw['quick_profit_forced_flatten_order_id'] == 'flat1'


def test_quick_profit_partial_sell_fail_reprotect_fail_flatten_fail(monkeypatch):
    _, updates = _setup_quick_profit_trade(monkeypatch)
    monkeypatch.setattr(execution, 'submit_market_sell', lambda *a, **k: (_ for _ in ()).throw(execution.BrokerError('sell fail')))
    monkeypatch.setattr(execution, 'submit_stop_sell', lambda *a, **k: (_ for _ in ()).throw(execution.BrokerError('restore fail')))
    execution.RUNTIME_STATE['last_position_monitor_error'] = None
    execution.monitor_positions_job()
    raw = updates[-1]['raw_json']
    assert raw['quick_profit_partial_sell_failed_reason'] == 'sell fail'
    assert raw['quick_profit_reprotect_failed_reason'] == 'restore fail'
    assert raw['quick_profit_forced_flatten_failed_reason'] == 'sell fail'
    assert raw['quick_profit_protection_type'] == 'failed'
    assert execution.RUNTIME_STATE['last_position_monitor_error'] == 'sell fail'
