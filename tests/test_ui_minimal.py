from pathlib import Path
import app


def test_get_root_route_200():
    if hasattr(app.app, 'test_client'):
        c = app.app.test_client()
        r = c.get('/')
        assert r.status_code == 200
    else:
        assert app.index() is not None


def test_minimal_ui_contains_required_markers():
    html = Path('templates/index.html').read_text(encoding='utf-8')
    for marker in [
        'Paper Day Flipper',
        'Trade Readiness',
        'Why No Motion?',
        'Last Auto Cycle',
        'Current Candidate',
        'Recent Paper Trades',
        'Advanced / Diagnostics',
        'Run Auto Cycle',
        'Emergency Close',
        'Scan Only — No Trade',
        'Market Reason',
        'market_clock_unavailable',
        'ALPACA_PAPER_BASE should be https://paper-api.alpaca.markets without /v2',
        'Run Preflight',
    ]:
        assert marker in html


def test_minimal_ui_has_emergency_confirm_and_helpers():
    html = Path('templates/index.html').read_text(encoding='utf-8')
    assert 'confirm(' in html
    assert 'normalizeBestTrade' in html
    assert 'explainNoMotion' in html
    assert 'renderLastAutoCycle' in html
    assert 'NOT PAPER' in html
    assert 'Mode: Not Paper / Blocked' in html


def test_minimal_ui_excludes_removed_markers():
    html = Path('templates/index.html').read_text(encoding='utf-8')
    for marker in [
        'LightweightCharts',
        'Rejected Candidates',
        'Engine Room',
        'Live Charts',
        'Top Market Candidates',
        'chart-box',
    ]:
        assert marker not in html
