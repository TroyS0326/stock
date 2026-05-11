import types

import app
import broker_facade
import config
import execution
import sim_broker
from db import get_conn


def test_sim_mode_routes_managed_entry_to_sim(monkeypatch):
    monkeypatch.setattr(config, 'SIMULATION_MODE', True)
    called = {}
    monkeypatch.setattr(sim_broker, 'place_managed_entry_order', lambda *a, **k: called.setdefault('ok', True) or {'id': 'SIM'})
    broker_facade.place_managed_entry_order('AAPL', 10, 10, 9, 11, 12)
    assert called['ok'] is True


def test_managed_entry_delayed_activates_exits_after_fill(monkeypatch):
    monkeypatch.setattr(config, 'SIMULATED_ORDER_FILL_DELAY_SECONDS', 999.0)
    order = sim_broker.place_managed_entry_order('MSFT', 10, 10, 9, 11, 12)
    assert order['pending_exit_activation'] is True
    assert order['target_1_order_id'] is None
    assert all(o.get('side') != 'sell' for o in sim_broker.get_open_orders('MSFT'))

    monkeypatch.setattr(config, 'SIMULATED_ORDER_FILL_DELAY_SECONDS', 0.0)
    sim_broker._apply_fill(order['id'])
    with get_conn() as conn:
        sells = [dict(r) for r in conn.execute('SELECT * FROM sim_orders WHERE parent_order_id=?', (order['id'],)).fetchall()]
    assert sells and all(o.get('status') == 'open' for o in sells)


def test_immediate_managed_entry_has_open_exits_not_auto_filled(monkeypatch):
    monkeypatch.setattr(config, 'SIMULATED_ORDER_FILL_DELAY_SECONDS', 0.0)
    order = sim_broker.place_managed_entry_order('NVDA', 10, 10, 9, 11, 12)
    assert order['pending_exit_activation'] is False
    assert order['target_1_order_id'] is not None
    assert sim_broker.get_order(order['target_1_order_id'])['status'] == 'open'


def test_sell_limits_do_not_autofill_from_delay(monkeypatch):
    monkeypatch.setattr(config, 'SIMULATED_ORDER_FILL_DELAY_SECONDS', 0.0)
    sim_broker.place_managed_entry_order('AMD', 10, 10, 9, 11, 12)
    monkeypatch.setattr(config, 'SIMULATED_ORDER_FILL_DELAY_SECONDS', 999.0)
    before = sim_broker.get_open_orders('AMD')
    sim_broker._maybe_fill_due_orders()
    after = sim_broker.get_open_orders('AMD')
    assert len(before) == len(after)


def test_market_sell_and_cancel_orders(monkeypatch):
    monkeypatch.setattr(config, 'SIMULATED_ORDER_FILL_DELAY_SECONDS', 0.0)
    sim_broker.place_managed_entry_order('TSLA', 10, 10, 9, 11, 12)
    out = sim_broker.submit_market_sell('TSLA', 10)
    assert out['status'] == 'filled'
    sim_broker.submit_stop_sell('TSLA', 2, 5)
    ids = sim_broker.cancel_open_orders_for_symbol('TSLA')
    assert isinstance(ids, list)


def test_flatten_book_sim_does_not_call_alpaca(monkeypatch):
    monkeypatch.setattr(config, 'SIMULATION_MODE', True)
    called = {'delete': 0}
    monkeypatch.setattr(execution.requests, 'delete', lambda *a, **k: called.__setitem__('delete', called['delete'] + 1))
    execution.flatten_book()
    assert called['delete'] == 0


def test_start_engine_sim_skips_trade_stream(monkeypatch):
    monkeypatch.setattr(config, 'SIMULATION_MODE', True)
    state = execution.start_execution_engine()
    assert state['trade_stream_required'] is False
    assert state['trade_stream_skipped_reason'] == 'simulation_mode'


def test_status_and_dashboard_markers(monkeypatch):
    monkeypatch.setattr(config, 'SIMULATION_MODE', True)
    monkeypatch.setattr(app, 'get_runtime_state', lambda: {'trade_stream_required': False, 'trade_stream_skipped_reason': 'simulation_mode'})
    monkeypatch.setattr(app, 'get_recent_scans', lambda: [])
    monkeypatch.setattr(app, 'get_recent_trades', lambda: [])
    monkeypatch.setattr(app, 'get_recent_operator_actions', lambda: [])
    monkeypatch.setattr(app, 'get_open_orders', lambda: [])
    monkeypatch.setattr(app, 'get_open_positions', lambda: [])
    monkeypatch.setattr(app, 'get_account', lambda: {'cash': 1, 'equity': 1})
    assert 'trade_stream_skipped_reason' in app.api_bot_status().json['data']
    html = open('templates/index.html', 'r', encoding='utf-8').read()
    assert 'Trade Stream:' in html
