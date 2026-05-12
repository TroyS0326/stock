import preflight
import app


def _map(result):
    return {c['name']: c for c in result['checks']}


def base_state():
    return {'scheduler_running': True, 'auto_scan_job_registered': True, 'last_auto_cycle_plan': {'candidate_count': 1, 'executable_count': 1}}


def test_sim_mode_passes_guard_without_credentials(monkeypatch):
    monkeypatch.setattr(preflight.config, 'SIMULATION_MODE', True)
    monkeypatch.setattr(preflight.config, 'ALPACA_API_KEY', '')
    monkeypatch.setattr(preflight.config, 'ALPACA_API_SECRET', '')
    monkeypatch.setattr(preflight, 'get_runtime_state', base_state)
    res = preflight.run_paper_trade_readiness_preflight('SPY')
    m = _map(res)
    assert m['paper_or_sim_guard']['status'] == 'PASS'
    assert m['credentials_present']['status'] == 'PASS'


def test_missing_creds_and_live_base_fail(monkeypatch):
    monkeypatch.setattr(preflight.config, 'SIMULATION_MODE', False)
    monkeypatch.setattr(preflight.config, 'PAPER_TRADING_DETECTED', False)
    monkeypatch.setattr(preflight.config, 'LIVE_TRADING_OVERRIDE', False)
    monkeypatch.setattr(preflight.config, 'ALPACA_PAPER_BASE', 'https://api.alpaca.markets')
    monkeypatch.setattr(preflight.config, 'ALPACA_API_KEY', '')
    monkeypatch.setattr(preflight.config, 'ALPACA_API_SECRET', '')
    monkeypatch.setattr(preflight, 'get_runtime_state', base_state)
    res = preflight.run_paper_trade_readiness_preflight('SPY')
    m = _map(res)
    assert m['paper_base_url']['status'] == 'FAIL'
    assert m['credentials_present']['status'] == 'FAIL'


def test_account_error_sanitized(monkeypatch):
    monkeypatch.setattr(preflight.config, 'SIMULATION_MODE', False)
    monkeypatch.setattr(preflight.config, 'PAPER_TRADING_DETECTED', True)
    monkeypatch.setattr(preflight.config, 'ALPACA_API_KEY', 'secretkey')
    monkeypatch.setattr(preflight.config, 'ALPACA_API_SECRET', 'secretvalue')
    monkeypatch.setattr(preflight, 'get_runtime_state', base_state)
    monkeypatch.setattr(preflight, 'get_account', lambda: (_ for _ in ()).throw(Exception('bad secretkey secretvalue')))
    res = preflight.run_paper_trade_readiness_preflight('SPY')
    msg = _map(res)['account_accessible']['message']
    assert 'secretkey' not in msg and 'secretvalue' not in msg


def test_blocked_account_wide_spread_quote_failures(monkeypatch):
    monkeypatch.setattr(preflight.config, 'SIMULATION_MODE', False)
    monkeypatch.setattr(preflight.config, 'PAPER_TRADING_DETECTED', True)
    monkeypatch.setattr(preflight.config, 'ALPACA_API_KEY', 'k')
    monkeypatch.setattr(preflight.config, 'ALPACA_API_SECRET', 's')
    monkeypatch.setattr(preflight, 'get_account', lambda: {'status': 'ACTIVE', 'trading_blocked': True, 'buying_power': '1'})
    monkeypatch.setattr(preflight, 'get_latest_quote', lambda s: {'bp': 1, 'ap': 2})
    monkeypatch.setattr(preflight, 'get_clock', lambda: {'is_open': False})
    monkeypatch.setattr(preflight, 'get_asset', lambda s: {'tradable': False})
    monkeypatch.setattr(preflight, 'get_runtime_state', lambda: {'scheduler_running': False, 'auto_scan_job_registered': False, 'last_auto_cycle_plan': {}})
    res = preflight.run_paper_trade_readiness_preflight('SPY')
    m = _map(res)
    assert m['account_tradeable']['status'] == 'FAIL'
    assert m['buying_power_probe_capacity']['status'] == 'FAIL'
    assert m['spread_reasonable_for_probe']['status'] == 'FAIL'
    assert m['symbol_tradability']['status'] == 'FAIL'
    assert res['next_action_hint'] in {'start_scheduler', 'set_paper_credentials', 'set_paper_base_url', 'run_auto_cycle_plan'}


def test_get_asset_routes(monkeypatch):
    import broker_facade
    monkeypatch.setattr(preflight.config, 'SIMULATION_MODE', True)
    a = broker_facade.get_asset('SPY')
    assert a.get('tradable') is True
