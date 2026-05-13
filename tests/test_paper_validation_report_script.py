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

def test_script_401(monkeypatch):
    monkeypatch.setattr(s, 'fetch', lambda args, token: (401, {'ok':False}))
    assert s.main([])==1
