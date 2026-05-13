import app



def test_bot_status_runtime_fields(client, monkeypatch):
    monkeypatch.setattr(app, 'get_runtime_state', lambda: {
        'engine_started': True,
        'scheduler_running': True,
        'trade_stream_thread_alive': True,
        'last_scan_at': '2026-01-01T12:00:00',
        'last_auto_trade_skip_reasons': [],
    })
    monkeypatch.setattr(app, 'get_recent_scans', lambda: [{'id': 1}])
    monkeypatch.setattr(app, 'get_recent_trades', lambda: [{'id': 2}])
    payload = client.get('/api/bot-status').get_json()
    assert payload['ok'] is True
    data = payload['data']
    assert 'engine_started' in data
    assert 'scheduled_jobs' in data or 'scheduler_running' in data
    assert 'recent_scans' in data
    assert 'recent_trades' in data
    for key in ['AUTO_TRADE_ENABLED', 'AUTO_SCAN_INTERVAL_SECONDS', 'POSITION_MONITOR_INTERVAL_SECONDS', 'MORNING_SCAN_START_ET', 'MORNING_SCAN_END_ET', 'NO_BUY_BEFORE_ET', 'MAX_AUTO_TRADES_PER_DAY', 'MAX_FAILED_TRADES_PER_DAY', 'SCAN_MIN_PRICE', 'SCAN_MAX_PRICE', 'QUICK_PROFIT_TAKE_PCT', 'BREAKEVEN_TRIGGER_PCT', 'ACTIVE_PAPER_TRADING_MODE', 'AUTO_SCAN_END_ET', 'MIN_AUTO_SETUP_GRADE', 'ALLOW_WATCH_GRADE_AUTO_TRADES', 'MIN_MOMENTUM_SCORE_TO_AUTOTRADE', 'FALLBACK_ENTRY_ENABLED', 'FALLBACK_ENTRY_MAX_SPREAD_PCT', 'MAX_DOLLAR_LOSS_PER_TRADE', 'MAX_TRADE_RISK_PCT']:
        assert key in data['config_summary']


def test_template_has_runtime_markers():
    html = open('templates/index.html', 'r', encoding='utf-8').read()
    assert '{{ app_title }}' in html
    assert 'The Trade Plan' in html
    assert 'Paper Validation' in html
    assert 'Run Morning Scan' in html


def test_template_js_handles_missing_diagnostics_markers():
    html = open('templates/index.html', 'r', encoding='utf-8').read()
    assert 'function cleanStatusText(value)' in html
    assert "if (lower.includes('401 authorization required') || lower.includes('market_clock_unavailable'))" in html
    assert 'if (out.length > 160)' in html


def test_api_preflight_returns_inner_ok(client, monkeypatch):
    monkeypatch.setattr(app, 'run_preflight', lambda: {
        'ok': False,
        'overall_status': 'BLOCKED',
        'checks': [{'name': 'x', 'status': 'fail'}],
        'auto_trade_readiness': {'ready': False},
    })
    payload = client.get('/api/preflight').get_json()
    assert payload['ok'] is True
    assert payload['data']['ok'] is False
    assert payload['data']['overall_status'] == 'BLOCKED'
    assert isinstance(payload['data']['checks'], list)
    assert isinstance(payload['data']['auto_trade_readiness'], dict)



def test_api_preflight_handles_unexpected_exception(client, monkeypatch):
    def _boom():
        raise RuntimeError('boom')

    monkeypatch.setattr(app, 'run_preflight', _boom)
    payload = client.get('/api/preflight').get_json()
    assert payload['ok'] is True
    data = payload['data']
    assert data['ok'] is False
    assert data['overall_status'] == 'BLOCKED'
    assert data['checks'][0]['name'] == 'preflight_exception'
    assert data['checks'][0]['status'] == 'FAIL'
    assert data['checks'][0]['message'].startswith('Preflight crashed: boom')
    assert data['auto_trade_readiness']['can_auto_trade_now'] is False
    assert data['auto_trade_readiness']['blocking_reasons'] == ['preflight_exception']
    assert data['auto_trade_readiness']['warning_reasons'] == []

def test_template_has_preflight_markers():
    html = open('templates/index.html', 'r', encoding='utf-8').read()
    assert 'Refresh Paper Gate' in html
    assert "fetch('/api/paper-market-launch-gate')" in html
    assert "fetch('/api/market-open-command-center')" in html
    assert "fetch('/api/paper-validation-session-report')" in html
