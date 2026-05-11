def test_flask_import_is_real_package():
    import flask

    assert hasattr(flask, "Flask")
    assert getattr(flask, "__file__", "")
    assert "site-packages/flask" in flask.__file__
