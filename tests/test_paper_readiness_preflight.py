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
    assert res['next_action_hint'] == 'fix_account_restriction'


def test_get_asset_routes(monkeypatch):
    import broker_facade
    monkeypatch.setattr(preflight.config, 'SIMULATION_MODE', True)
    a = broker_facade.get_asset('SPY')
    assert a.get('tradable') is True


import app


def _map(result):
    return {c['name']: c for c in result['checks']}


def base_state():
    return {'scheduler_running': True, 'auto_scan_job_registered': True, 'last_auto_cycle_plan': {'candidate_count': 1, 'executable_count': 1}}


def _common_ready_monkeypatch(monkeypatch):
    monkeypatch.setattr(preflight.config, 'SIMULATION_MODE', False)
    monkeypatch.setattr(preflight.config, 'PAPER_TRADING_DETECTED', True)
    monkeypatch.setattr(preflight.config, 'LIVE_TRADING_OVERRIDE', False)
    monkeypatch.setattr(preflight.config, 'ALPACA_PAPER_BASE', 'https://paper-api.alpaca.markets')
    monkeypatch.setattr(preflight.config, 'ALPACA_API_KEY', 'k')
    monkeypatch.setattr(preflight.config, 'ALPACA_API_SECRET', 's')
    monkeypatch.setattr(preflight.config, 'PREFLIGHT_REQUIRE_ASSET_TRADABLE', True)
    monkeypatch.setattr(preflight.config, 'FIRST_TRADE_GOVERNOR_ENABLED', True)
    monkeypatch.setattr(preflight.config, 'FIRST_TRADE_MAX_QTY', 1)
    monkeypatch.setattr(preflight.config, 'FIRST_TRADE_MAX_DOLLAR_RISK', 10.0)
    monkeypatch.setattr(preflight, 'get_account', lambda: {'status': 'ACTIVE', 'buying_power': '1000'})
    monkeypatch.setattr(preflight, 'get_latest_quote', lambda s: {'bp': 100, 'ap': 100.01})
    monkeypatch.setattr(preflight, 'get_clock', lambda: {'is_open': True})
    monkeypatch.setattr(preflight, 'get_asset', lambda s: {'tradable': True, 'status': 'active'})
    monkeypatch.setattr(preflight, 'get_runtime_state', base_state)


def test_next_action_hint_mapping_for_blocking_failures(monkeypatch):
    _common_ready_monkeypatch(monkeypatch)
    scenarios = [
        ('account_accessible', lambda: (_ for _ in ()).throw(Exception('down')), 'fix_paper_account_access'),
        ('account_tradeable', {'status': 'ACTIVE', 'trading_blocked': True, 'buying_power': '1000'}, 'fix_account_restriction'),
        ('quote_accessible', {'bp': 0, 'ap': 0}, 'fix_market_data_feed'),
        ('spread_reasonable_for_probe', {'bp': 100, 'ap': 103}, 'wait_for_tighter_spread_or_change_symbol'),
        ('buying_power_probe_capacity', {'status': 'ACTIVE', 'buying_power': '1'}, 'fund_paper_account_or_reduce_symbol_price'),
        ('symbol_tradability', {'tradable': False, 'status': 'inactive'}, 'change_preflight_symbol_or_asset_status'),
    ]

    for name, payload, expected in scenarios:
        _common_ready_monkeypatch(monkeypatch)
        if name == 'account_accessible':
            monkeypatch.setattr(preflight, 'get_account', payload)
        elif name in {'account_tradeable', 'buying_power_probe_capacity'}:
            monkeypatch.setattr(preflight, 'get_account', lambda payload=payload: payload)
        elif name in {'quote_accessible', 'spread_reasonable_for_probe'}:
            monkeypatch.setattr(preflight, 'get_latest_quote', lambda _s, payload=payload: payload)
        elif name == 'symbol_tradability':
            monkeypatch.setattr(preflight, 'get_asset', lambda _s, payload=payload: payload)
        res = preflight.run_paper_trade_readiness_preflight('SPY')
        assert res['overall_status'] == 'FAIL'
        assert res['next_action_hint'] == expected
        assert res['next_action_hint'] != 'ready_for_open'


def test_first_trade_governor_fail_maps_hint(monkeypatch):
    _common_ready_monkeypatch(monkeypatch)
    monkeypatch.setattr(preflight.config, 'FIRST_TRADE_MAX_QTY', 0)
    res = preflight.run_paper_trade_readiness_preflight('SPY')
    assert res['next_action_hint'] == 'fix_first_trade_governor_config'


def test_candidate_plan_zero_executable_maps_to_scan_diagnostics(monkeypatch):
    _common_ready_monkeypatch(monkeypatch)
    monkeypatch.setattr(preflight, 'get_runtime_state', lambda: {'scheduler_running': True, 'auto_scan_job_registered': True, 'last_auto_cycle_plan': {'candidate_count': 3, 'executable_count': 0}})
    res = preflight.run_paper_trade_readiness_preflight('SPY')
    assert res['overall_status'] == 'FAIL'
    assert res['next_action_hint'] == 'review_scan_diagnostics'


def test_candidate_plan_missing_warning_maps_to_run_auto_cycle_plan(monkeypatch):
    _common_ready_monkeypatch(monkeypatch)
    monkeypatch.setattr(preflight, 'get_runtime_state', lambda: {'scheduler_running': True, 'auto_scan_job_registered': True, 'last_auto_cycle_plan': {}})
    res = preflight.run_paper_trade_readiness_preflight('SPY')
    assert res['overall_status'] == 'WARN'
    assert res['next_action_hint'] == 'run_auto_cycle_plan'


def test_all_checks_pass_ready_for_open(monkeypatch):
    _common_ready_monkeypatch(monkeypatch)
    res = preflight.run_paper_trade_readiness_preflight('SPY')
    assert res['overall_status'] == 'PASS'
    assert res['next_action_hint'] == 'ready_for_open'
