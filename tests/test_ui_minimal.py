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
        'Trading Bot',
        'Run Scan',
        'Run Preflight',
        'Emergency: Cancel + Close',
        'Current Best Trade',
        'Auto Attempts',
        'Recent Trades',
        'Preflight',
    ]:
        assert marker in html


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
