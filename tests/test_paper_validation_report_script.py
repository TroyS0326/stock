import scripts.paper_validation_report as s

def test_script_no_token_printed(monkeypatch, capsys):
    token='secret-token'
    monkeypatch.setattr(s, 'fetch', lambda args, token: (200, {'data': {'acceptance_pass': True, 'report_status':'ACCEPTED_PAPER_VALIDATION'}}))
    rc=s.main(['--token', token])
    out=capsys.readouterr().out
    assert rc==0
    assert token not in out

def test_script_exit_codes(monkeypatch):
    monkeypatch.setattr(s, 'fetch', lambda args, token: (200, {'data': {'acceptance_pass': False, 'report_status':'REVIEW_REQUIRED'}}))
    assert s.main([])==1

def test_script_500_fails_even_if_acceptance_true(monkeypatch):
    monkeypatch.setattr(s, 'fetch', lambda args, token: (500, {'data': {'acceptance_pass': True}}))
    assert s.main([]) == 1

def test_script_404_fails(monkeypatch):
    monkeypatch.setattr(s, 'fetch', lambda args, token: (404, {'ok': False}))
    assert s.main([]) == 1

def test_script_200_acceptance_true(monkeypatch):
    monkeypatch.setattr(s, 'fetch', lambda args, token: (200, {'data': {'acceptance_pass': True}}))
    assert s.main([]) == 0

def test_script_401(monkeypatch):
    monkeypatch.setattr(s, 'fetch', lambda args, token: (401, {'ok':False}))
    assert s.main([])==1
