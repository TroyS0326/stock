from pathlib import Path
import app


def test_get_root_route_200():
    client = app.app.test_client()
    assert client.get('/').status_code == 200


def test_dashboard_contains_restored_sections_and_controls():
    html = Path('templates/index.html').read_text(encoding='utf-8')
    for marker in [
        'Your Data-Driven Co-Pilot',
        'Run Morning Scan',
        'The Trade Plan',
        'The Engine Room',
        'Live Charts',
        'Top Market Candidates',
        'Paper Validation',
        'cleanStatusText',
        '/api/paper-market-launch-gate',
        '/api/market-open-command-center',
        '/api/paper-validation-session-report',
        '/api/scan',
        '/ws/watchlist',
        'lightweight-charts.standalone.production.js',
    ]:
        assert marker in html


def test_dashboard_excludes_clutter_and_forbidden_controls_endpoints():
    html = Path('templates/index.html').read_text(encoding='utf-8')
    forbidden = [
        'Trade Readiness', 'Why No Motion?', 'Last Auto Cycle', 'Current Candidate',
        'Recent Paper Trades', 'Run Auto Cycle', 'Emergency Close', 'Pause Bot',
        'Resume Bot', 'Clear Emergency', '/api/auto-cycle', '/api/run-auto-cycle',
        '/api/control/emergency-stop', '/api/control/clear-emergency-stop',
        '/api/control/pause-auto-trading', '/api/control/resume-auto-trading',
        '/api/order', '/api/orders', '/api/position/close', '/api/positions/close'
    ]
    for marker in forbidden:
        assert marker not in html


def test_clean_status_maps_broker_401_text():
    html = Path('templates/index.html').read_text(encoding='utf-8')
    assert '401 authorization required' in html.lower()
    assert 'Alpaca auth failed. Check paper API key, secret, and ALPACA_PAPER_BASE.' in html
