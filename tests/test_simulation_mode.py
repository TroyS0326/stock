import types

import broker_facade
import config
import execution_service
import app
import sim_broker


def test_sim_mode_routes_to_sim(monkeypatch):
    monkeypatch.setattr(config, 'SIMULATION_MODE', True)
    called = {}
    monkeypatch.setattr(sim_broker, 'submit_market_sell', lambda symbol, qty: called.setdefault('sim', (symbol, qty)) or {'id': 'SIM'})
    broker_facade.submit_market_sell('AAPL', 1)
    assert called['sim'] == ('AAPL', 1)


def test_managed_entry_and_sell_lifecycle(monkeypatch):
    monkeypatch.setattr(config, 'SIMULATION_MODE', True)
    monkeypatch.setattr(config, 'SIMULATED_ORDER_FILL_DELAY_SECONDS', 999.0)
    order = sim_broker.place_managed_entry_order('MSFT', 10, 10, 9, 11, 12)
    assert str(order['id']).startswith('SIM-ORDER-')
    assert len(sim_broker.get_open_orders('MSFT')) >= 1
    monkeypatch.setattr(config, 'SIMULATED_ORDER_FILL_DELAY_SECONDS', 0.0)
    sim_broker.submit_market_sell('MSFT', 10)
    assert isinstance(sim_broker.get_open_positions(), list)


def test_cancel_open_orders(monkeypatch):
    monkeypatch.setattr(config, 'SIMULATED_ORDER_FILL_DELAY_SECONDS', 999.0)
    sim_broker.submit_stop_sell('TSLA', 2, 100)
    ids = sim_broker.cancel_open_orders_for_symbol('TSLA')
    assert ids


def test_emergency_stop_simulation(monkeypatch):
    monkeypatch.setattr(config, 'SIMULATION_MODE', True)
    monkeypatch.setattr(config, 'PAPER_TRADING_DETECTED', False)
    app.request = types.SimpleNamespace(headers={}, get_json=lambda silent=True: {'close_positions': True})
    monkeypatch.setattr(app, 'insert_operator_action', lambda *a, **k: 1)
    monkeypatch.setattr(app, 'get_runtime_state', lambda: {})
    monkeypatch.setattr(app, 'emergency_cancel_and_flatten', lambda close_positions=False, reason=None: {'ok': True, 'errors': [], 'canceled_symbols': [], 'closed_positions': []})
    resp, status = app.api_control_emergency_stop()
    assert status == 200 and resp.json['ok'] is True


def test_preflight_and_status_fields(monkeypatch):
    monkeypatch.setattr(config, 'SIMULATION_MODE', True)
    monkeypatch.setattr(app, 'get_runtime_state', lambda: {})
    monkeypatch.setattr(app, 'get_recent_scans', lambda: [])
    monkeypatch.setattr(app, 'get_recent_trades', lambda: [])
    monkeypatch.setattr(app, 'get_recent_operator_actions', lambda: [])
    monkeypatch.setattr(app, 'get_open_orders', lambda: [])
    monkeypatch.setattr(app, 'get_open_positions', lambda: [])
    monkeypatch.setattr(app, 'get_account', lambda: {'cash': 1, 'equity': 1})
    assert 'simulation_mode' in app.api_bot_status().json['data']
    assert app.api_control_state().json['data']['broker_backend'] == 'simulation'


def test_dashboard_markers():
    html = open('templates/index.html', 'r', encoding='utf-8').read()
    assert 'Simulation Mode is ON — no Alpaca orders will be placed.' in html
    assert 'Broker backend' in html
