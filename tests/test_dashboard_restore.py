from pathlib import Path

import app


def test_root_dashboard_contains_restored_sections_when_auth_disabled(monkeypatch):
    monkeypatch.setattr(app.config, 'OPERATOR_AUTH_ENABLED', False)
    client = app.app.test_client()
    resp = client.get('/')
    assert resp.status_code == 200
    html = Path('templates/index.html').read_text(encoding='utf-8')
    for marker in [
        'Your Data-Driven Co-Pilot',
        'Run Morning Scan',
        'The Trade Plan',
        'The Engine Room',
        'Live Charts',
        'Top Market Candidates',
        'Paper Validation',
    ]:
        assert marker in html


def test_operator_route_is_redirect_or_ok_when_auth_disabled(monkeypatch):
    monkeypatch.setattr(app.config, 'OPERATOR_AUTH_ENABLED', False)
    client = app.app.test_client()
    resp = client.get('/operator', follow_redirects=False)
    assert resp.status_code in (200, 301, 302, 307, 308)


def test_dashboard_excludes_cluttered_controls_and_sections():
    html = Path('templates/index.html').read_text(encoding='utf-8')
    forbidden = [
        'Trade Readiness',
        'Why No Motion?',
        'Last Auto Cycle',
        'Current Candidate',
        'Recent Paper Trades',
        'Run Auto Cycle',
        'Emergency Close',
        'Pause Bot',
        'Resume Bot',
        'Clear Emergency',
    ]
    for marker in forbidden:
        assert marker not in html


def test_dashboard_has_readonly_paper_validation_endpoints_and_no_mutation_endpoints():
    html = Path('templates/index.html').read_text(encoding='utf-8')
    for endpoint in [
        '/api/paper-market-launch-gate',
        '/api/market-open-command-center',
        '/api/paper-validation-session-report',
    ]:
        assert endpoint in html
    for forbidden in [
        '/api/auto-cycle',
        '/api/run-auto-cycle',
        '/api/control/emergency-stop',
        '/api/control/clear-emergency-stop',
        '/api/control/pause-auto-trading',
        '/api/control/resume-auto-trading',
        '/api/order',
        '/api/orders',
        '/api/position/close',
        '/api/positions/close',
    ]:
        assert forbidden not in html


def test_dashboard_includes_original_scan_and_socket_markers():
    html = Path('templates/index.html').read_text(encoding='utf-8')
    assert "fetch('/api/scan'" in html
    assert '/ws/watchlist' in html
    assert 'lightweight-charts' in html


def test_clean_status_text_contains_401_cleanup_logic():
    html = Path('templates/index.html').read_text(encoding='utf-8')
    assert 'function cleanStatusText(value)' in html
    assert "lower.includes('401 authorization required')" in html
    assert "lower.includes('unauthorized')" in html
    assert 'Alpaca auth failed. Check paper API key, secret, and ALPACA_PAPER_BASE.' in html


def test_no_order_execution_on_dashboard_load(monkeypatch):
    called = {}

    def _mark(name):
        def _inner(*args, **kwargs):
            called[name] = called.get(name, 0) + 1
            raise AssertionError(f'{name} should not be called on dashboard load')

        return _inner

    for fn in [
        'run_scan',
        'run_scan_and_maybe_auto_trade',
        'execute_trade_candidate',
        'place_managed_entry_order',
        'submit_order',
        'cancel_order',
        'close_position',
        'submit_market_sell',
    ]:
        if hasattr(app, fn):
            monkeypatch.setattr(app, fn, _mark(fn), raising=False)

    client = app.app.test_client()
    resp = client.get('/')
    assert resp.status_code == 200
    assert called == {}
