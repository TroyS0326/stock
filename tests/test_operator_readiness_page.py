from pathlib import Path

import app


SAFE_ENDPOINTS = [
    '/api/market-open-command-center',
    '/api/paper-readiness-preflight',
    '/api/synthetic-auto-cycle-rehearsal',
    '/api/pre-market-readiness-pipeline',
    '/api/market-open-rehearsal',
    '/api/auto-cycle-plan',
    '/api/first-trade-observer',
    '/api/position-protection-audit',
    '/api/market-session-heartbeat',
    '/api/auto-cycle-attempts?limit=10',
    '/api/operator-safe-endpoint-health',
]


def test_operator_route_returns_200():
    client = app.app.test_client()
    response = client.get('/operator')
    assert response.status_code == 200


def test_operator_template_contains_safe_diagnostics_only_markers():
    html = Path('templates/operator_readiness.html').read_text(encoding='utf-8')
    for endpoint in SAFE_ENDPOINTS:
        assert endpoint in html

    forbidden_markers = [
        'data-endpoint="/api/auto-cycle"',
        'data-endpoint="/api/run-auto-cycle"',
        'LIVE_TRADING_OVERRIDE',
        'APCA_API_KEY_ID',
        'APCA_API_SECRET_KEY',
        'submit_order',
        'cancel_order',
        'close_position',
        'place_managed_entry_order',
    ]
    for marker in forbidden_markers:
        assert marker not in html


def test_operator_template_contains_required_warnings_and_fetch_targets():
    html = Path('templates/operator_readiness.html').read_text(encoding='utf-8')

    assert 'Paper/sim only.' in html
    assert 'This page does not place trades.' in html
    assert 'Do not enable live trading.' in html
    assert 'Do not increase first-trade qty until after paper review.' in html
    assert 'Synthetic rehearsal does not prove broker/account readiness.' in html

    allowed_fetch_targets = {
        '/api/market-open-command-center',
        '/api/auto-cycle-attempts?limit=10',
    }
    fetch_targets = set()
    for line in html.splitlines():
        if "fetch('" in line:
            fetch_targets.add(line.split("fetch('", 1)[1].split("'", 1)[0])

    assert fetch_targets.issubset(allowed_fetch_targets)


def test_operator_template_uses_expected_api_response_shape():
    html = Path('templates/operator_readiness.html').read_text(encoding='utf-8')

    assert 'center.data.data' in html
    assert 'ledger.data.data' in html
    assert 'payload.market_status' in html
    assert 'payload.scheduler_summary' in html
    assert 'payload.readiness_cards' in html
    assert 'center.data.payload' not in html
    assert 'ledger.data.payload' not in html


def test_operator_template_reads_readiness_cards_from_payload_readiness_cards():
    html = Path('templates/operator_readiness.html').read_text(encoding='utf-8')

    assert "const SAFE_CARD_KEYS = ['paper_readiness', 'pipeline', 'scheduler', 'market', 'latest_attempt', 'protection', 'heartbeat'];" in html
    assert 'const readinessCards = payload.readiness_cards || {};' in html
    assert 'JSON.stringify(readinessCards[key] || {}, null, 2)' in html
    assert 'JSON.stringify(payload[key] || {}, null, 2)' not in html
