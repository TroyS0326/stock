import app


def test_operator_route_returns_200_or_redirect_when_auth_disabled(monkeypatch):
    monkeypatch.setattr(app.config, 'OPERATOR_AUTH_ENABLED', False)
    client = app.app.test_client()
    response = client.get('/operator', follow_redirects=False)
    assert response.status_code in (200, 301, 302, 307, 308)


def test_operator_uses_restored_dashboard_template():
    client = app.app.test_client()
    response = client.get('/operator', follow_redirects=True)
    assert response.status_code == 200
    html = open('templates/index.html', 'r', encoding='utf-8').read()
    assert 'Your Data-Driven Co-Pilot' in html
    assert 'Paper Validation' in html
    assert 'Run Morning Scan' in html
