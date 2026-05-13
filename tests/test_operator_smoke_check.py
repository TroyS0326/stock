import json

import scripts.operator_smoke_check as smoke


def test_planned_endpoint_list_contains_only_allowed_no_order_endpoints():
    plan = smoke.build_plan(False)
    paths = {path for _m, path, _p, _k in plan}
    assert paths == {
        '/api/operator-safe-endpoint-health',
        '/api/operator-runbook',
        '/api/paper-market-launch-gate',
        '/api/market-open-command-center',
        '/api/market-session-heartbeat',
        '/api/auto-cycle-attempts?limit=5',
    }


def test_forbidden_endpoints_are_not_in_default_planned_calls():
    ok, forbidden = smoke.assert_plan_safe(smoke.build_plan(False))
    assert ok is True
    assert forbidden == []


def test_script_does_not_print_token(monkeypatch, capsys):
    token = 'super-secret-token'

    def fake_fetch(*_a, **_k):
        return 200, {'data': {}}

    monkeypatch.setattr(smoke, 'fetch_json', fake_fetch)
    rc = smoke.main(['--token', token])
    out = capsys.readouterr().out
    assert rc == 1
    assert token not in out


def test_script_returns_fail_on_401(monkeypatch, capsys):
    def fake_fetch(_base, _m, path, *_args):
        if path == '/api/operator-safe-endpoint-health':
            return 401, {}
        return 200, {'data': {}}

    monkeypatch.setattr(smoke, 'fetch_json', fake_fetch)
    rc = smoke.main([])
    out = capsys.readouterr().out
    assert rc == 1
    assert 'auth failure' in out


def test_launch_gate_go_for_paper_validation_passes(monkeypatch):
    payloads = {
        '/api/operator-safe-endpoint-health': {'data': {'ok': True, 'missing_expected_endpoints': [], 'unexpected_forbidden_present': []}},
        '/api/paper-market-launch-gate': {'data': {'launch_gate_status': 'GO_FOR_PAPER_MARKET_VALIDATION', 'go_for_paper_validation': True, 'required_actions': []}},
        '/api/market-open-command-center': {'data': {}},
        '/api/market-session-heartbeat': {'data': {}},
        '/api/operator-runbook': {'data': {}},
        '/api/auto-cycle-attempts?limit=5': {'data': {'attempts': []}},
    }
    monkeypatch.setattr(smoke, 'fetch_json', lambda _b, _m, p, *_a: (200, payloads[p]))
    assert smoke.main([]) == 0


def test_wait_for_market_open_with_go_true_is_warn_exit_zero(monkeypatch):
    payloads = {
        '/api/operator-safe-endpoint-health': {'data': {'ok': True, 'missing_expected_endpoints': [], 'unexpected_forbidden_present': []}},
        '/api/paper-market-launch-gate': {'data': {'launch_gate_status': 'WAIT_FOR_MARKET_OPEN', 'go_for_paper_validation': True, 'required_actions': []}},
        '/api/market-open-command-center': {'data': {}},
        '/api/market-session-heartbeat': {'data': {}},
        '/api/operator-runbook': {'data': {}},
        '/api/auto-cycle-attempts?limit=5': {'data': {'attempts': []}},
    }
    monkeypatch.setattr(smoke, 'fetch_json', lambda _b, _m, p, *_a: (200, payloads[p]))
    assert smoke.main([]) == 0


def test_blocked_launch_gate_fails(monkeypatch):
    payloads = {
        '/api/operator-safe-endpoint-health': {'data': {'ok': True, 'missing_expected_endpoints': [], 'unexpected_forbidden_present': []}},
        '/api/paper-market-launch-gate': {'data': {'launch_gate_status': 'BLOCKED_SAFETY', 'go_for_paper_validation': False, 'required_actions': ['x']}},
        '/api/market-open-command-center': {'data': {}},
        '/api/market-session-heartbeat': {'data': {}},
        '/api/operator-runbook': {'data': {}},
        '/api/auto-cycle-attempts?limit=5': {'data': {'attempts': []}},
    }
    monkeypatch.setattr(smoke, 'fetch_json', lambda _b, _m, p, *_a: (200, payloads[p]))
    assert smoke.main([]) == 1


def test_missing_optional_endpoints_graceful_when_optional_mode_disabled(monkeypatch):
    seen = []

    def fake_fetch(_b, _m, p, *_a):
        seen.append(p)
        return 200, {'data': {'launch_gate_status': 'GO_FOR_PAPER_MARKET_VALIDATION', 'go_for_paper_validation': True} if p == '/api/paper-market-launch-gate' else {}}

    monkeypatch.setattr(smoke, 'fetch_json', fake_fetch)
    rc = smoke.main([])
    assert rc == 0
    assert '/api/pre-market-readiness-pipeline' not in seen


def test_optional_mode_includes_only_safe_no_order_optional_endpoints():
    plan = smoke.build_plan(True)
    optional = {path for _m, path, _p, kind in plan if kind == 'optional_no_order_diagnostic'}
    assert optional == {
        '/api/pre-market-readiness-pipeline',
        '/api/synthetic-auto-cycle-rehearsal',
        '/api/market-open-rehearsal',
        '/api/auto-cycle-plan',
    }
    ok, forbidden = smoke.assert_plan_safe(plan)
    assert ok is True
    assert forbidden == []
