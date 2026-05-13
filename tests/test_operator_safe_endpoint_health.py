from pathlib import Path
from unittest.mock import patch

import app


REQUIRED_PATHS = {
    '/api/market-open-command-center',
    '/api/paper-market-launch-gate',
    '/api/paper-readiness-preflight',
    '/api/synthetic-auto-cycle-rehearsal',
    '/api/pre-market-readiness-pipeline',
    '/api/market-open-rehearsal',
    '/api/auto-cycle-plan',
    '/api/first-trade-observer',
    '/api/position-protection-audit',
    '/api/market-session-heartbeat',
    '/api/paper-validation-session-report',
    '/api/auto-cycle-attempts?limit=10',
    '/api/deployment-checklist',
    '/api/operator-runbook',
}


def test_operator_safe_endpoint_health_returns_200():
    client = app.app.test_client()
    response = client.get('/api/operator-safe-endpoint-health')
    assert response.status_code == 200
    payload = response.get_json()['data']
    assert payload['endpoint_count'] == len(payload['endpoints'])


def test_helper_returns_required_allowed_endpoints_and_non_mutating():
    payload = app.build_operator_safe_endpoint_health()
    endpoint_paths = {item['path'] for item in payload['endpoints']}
    assert REQUIRED_PATHS.issubset(endpoint_paths)
    assert all(item['mutates_orders'] is False for item in payload['endpoints'])
    assert payload['missing_expected_endpoints'] == []


def test_helper_includes_forbidden_endpoint_list():
    payload = app.build_operator_safe_endpoint_health()
    forbidden = payload['forbidden_endpoints']
    assert 'POST /api/auto-cycle' in forbidden
    assert 'POST /api/run-auto-cycle' in forbidden
    assert any('emergency-stop' in item for item in forbidden)


def test_helper_does_not_call_broker_scanner_or_order_functions():
    with (
        patch('app.run_scan', side_effect=AssertionError('run_scan called')),
        patch('app.run_scan_and_maybe_auto_trade', side_effect=AssertionError('run_scan_and_maybe_auto_trade called'), create=True),
        patch('app.execute_trade_candidate', side_effect=AssertionError('execute_trade_candidate called')),
        patch('app.place_managed_entry_order', side_effect=AssertionError('place_managed_entry_order called'), create=True),
        patch('app.submit_order', side_effect=AssertionError('submit_order called'), create=True),
        patch('app.cancel_order', side_effect=AssertionError('cancel_order called'), create=True),
        patch('app.close_position', side_effect=AssertionError('close_position called'), create=True),
        patch('app.submit_market_sell', side_effect=AssertionError('submit_market_sell called'), create=True),
        patch('app.get_account', side_effect=AssertionError('get_account called')),
        patch('app.get_latest_quote', side_effect=AssertionError('get_latest_quote called'), create=True),
        patch('app.get_clock', side_effect=AssertionError('get_clock called')),
        patch('app.get_asset', side_effect=AssertionError('get_asset called'), create=True),
    ):
        payload = app.build_operator_safe_endpoint_health()
        assert payload['ok'] is True


def test_operator_template_includes_health_endpoint_and_excludes_auto_cycle_execution_buttons():
    html = Path('templates/operator_readiness.html').read_text(encoding='utf-8')
    assert '/api/operator-safe-endpoint-health' in html
    assert 'data-endpoint="/api/auto-cycle"' not in html
    assert 'data-endpoint="/api/run-auto-cycle"' not in html


def test_static_safe_endpoints_are_present_in_operator_page_or_backend_only():
    html = Path('templates/operator_readiness.html').read_text(encoding='utf-8')
    backend_only = set(app.OPERATOR_SAFE_BACKEND_ONLY_ENDPOINTS)
    for endpoint in app.OPERATOR_SAFE_ENDPOINTS:
        path = endpoint['path']
        assert path in html or path in backend_only


def test_operator_page_data_endpoints_are_declared_safe_or_allowed_page_local():
    html = Path('templates/operator_readiness.html').read_text(encoding='utf-8')
    safe_paths = {endpoint['path'] for endpoint in app.OPERATOR_SAFE_ENDPOINTS}
    backend_only = set(app.OPERATOR_SAFE_BACKEND_ONLY_ENDPOINTS)
    explicitly_allowed_page_local = {'/api/operator-safe-endpoint-health'}
    data_endpoints = set()
    for marker in html.split('data-endpoint="')[1:]:
        data_endpoints.add(marker.split('"', 1)[0])

    for path in data_endpoints:
        assert path in safe_paths or path in explicitly_allowed_page_local
        assert path not in backend_only
