import os
import sys
import types

os.environ.setdefault("DISABLE_AUTO_START_FOR_TESTS", "1")
os.environ.setdefault("AUTO_START_EXECUTION_ENGINE", "0")
os.environ.setdefault("AUTO_TRADE_ENABLED", "1")
os.environ.setdefault("ACTIVE_PAPER_TRADING_MODE", "1")
os.environ.setdefault("SIMULATION_MODE", "0")
os.environ.setdefault("AUTO_CYCLE_REQUIRE_MARKET_OPEN", "0")
os.environ.setdefault("AGGRESSIVE_DAY_FLIPPER_MODE", "1")
os.environ.setdefault("PAPER_PROBE_TRADES_ENABLED", "1")
os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_API_SECRET", "test-secret")
os.environ.setdefault("ALPACA_PAPER_BASE", "https://paper-api.alpaca.markets")

import pytest


def _install_flask_stub() -> None:
    flask_stub = types.ModuleType("flask")
    flask_stub.__file__ = "/usr/lib/python3/site-packages/flask/__init__.py"

    class _DummyRequest:
        def __init__(self):
            self.path = "/"
            self.headers = {}
            self.args = {}
            self.json = {}

        def get_json(self, silent=True):
            return self.json or {}

    request_obj = _DummyRequest()

    class _DummyResponse:
        def __init__(self, payload=None, status_code=200):
            self.json = payload if payload is not None else {}
            self.status_code = status_code
            self.data = str(self.json).encode("utf-8")

        def get_json(self):
            return self.json

    def jsonify(*args, **kwargs):
        if args and kwargs:
            payload = {"args": args, **kwargs}
        elif len(args) == 1:
            payload = args[0]
        elif args:
            payload = list(args)
        else:
            payload = kwargs
        return _DummyResponse(payload, status_code=200)

    class _DummyFlask:
        def __init__(self, *a, **k):
            self.testing = False
            self.config = {}
            self._routes = {}
            self._errorhandlers = {}
            self._before_request_funcs = []

        def route(self, rule, methods=None, **kwargs):
            methods = tuple((methods or ["GET"]))

            def deco(fn):
                for m in methods:
                    self._routes[(rule, m.upper())] = fn
                return fn

            return deco

        def errorhandler(self, exc_type):
            def deco(fn):
                self._errorhandlers[exc_type] = fn
                return fn

            return deco

        def before_request(self, fn):
            self._before_request_funcs.append(fn)
            return fn

        def test_client(self):
            app = self

            class _C:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    pass

                def _dispatch(self, path, method, json=None, headers=None, query_string=None):
                    request_obj.path = path
                    request_obj.json = json or {}
                    request_obj.headers = headers or {}
                    request_obj.args = query_string or {}
                    fn = app._routes.get((path, method.upper()))
                    if fn is None:
                        return _DummyResponse({"ok": False, "error": "not_found"}, 404)
                    try:
                        for before_fn in app._before_request_funcs:
                            maybe_response = before_fn()
                            if maybe_response is not None:
                                res = maybe_response
                                break
                        else:
                            res = fn()
                    except Exception as exc:
                        for k, handler in app._errorhandlers.items():
                            if isinstance(exc, k):
                                res = handler(exc)
                                break
                        else:
                            raise
                    status = 200
                    if isinstance(res, tuple):
                        obj, status = res
                    else:
                        obj = res
                    if isinstance(obj, _DummyResponse):
                        obj.status_code = status
                        return obj
                    return _DummyResponse(obj if obj is not None else {}, status)

                def get(self, path, **kwargs):
                    return self._dispatch(path, "GET", headers=kwargs.get("headers"), query_string=kwargs.get("query_string"))

                def post(self, path, json=None, **kwargs):
                    return self._dispatch(path, "POST", json=json, headers=kwargs.get("headers"), query_string=kwargs.get("query_string"))

            return _C()

        def app_context(self):
            class _Ctx:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    pass

            return _Ctx()

    flask_stub.Flask = _DummyFlask
    flask_stub.jsonify = jsonify
    flask_stub.render_template = lambda *a, **k: ""
    flask_stub.request = request_obj
    sys.modules["flask"] = flask_stub


try:
    import flask  # type: ignore
except Exception:
    _install_flask_stub()

if "werkzeug.exceptions" not in sys.modules:
    wz = types.ModuleType("werkzeug")
    wz_exc = types.ModuleType("werkzeug.exceptions")

    class HTTPException(Exception):
        code = 500
        description = "HTTP error"

    wz_exc.HTTPException = HTTPException
    sys.modules["werkzeug"] = wz
    sys.modules["werkzeug.exceptions"] = wz_exc
if "flask_sock" not in sys.modules:
    fs = types.ModuleType("flask_sock")

    class Sock:
        def __init__(self, *a, **k):
            pass

        def route(self, *a, **k):
            def deco(fn):
                return fn

            return deco

        def errorhandler(self, *a, **k):
            def deco(fn):
                return fn

            return deco

    fs.Sock = Sock
    sys.modules["flask_sock"] = fs
if "dotenv" not in sys.modules:
    dm = types.ModuleType("dotenv")
    dm.load_dotenv = lambda *a, **k: None
    sys.modules["dotenv"] = dm
if "requests" not in sys.modules:
    rm = types.ModuleType("requests")

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {}

    rm.get = rm.post = rm.patch = rm.delete = lambda *a, **k: _Resp()
    sys.modules["requests"] = rm
if "websockets" not in sys.modules:
    wm = types.ModuleType("websockets")
    wm.connect = lambda *a, **k: None
    sys.modules["websockets"] = wm
if "apscheduler" not in sys.modules:
    aps_mod = types.ModuleType("apscheduler")
    sched_mod = types.ModuleType("apscheduler.schedulers")
    bg_mod = types.ModuleType("apscheduler.schedulers.background")

    class _DummyScheduler:
        def __init__(self, *a, **k):
            self.running = False

        def add_job(self, *a, **k):
            pass

        def start(self):
            self.running = True

        def get_jobs(self):
            return []

    bg_mod.BackgroundScheduler = _DummyScheduler
    sys.modules["apscheduler"] = aps_mod
    sys.modules["apscheduler.schedulers"] = sched_mod
    sys.modules["apscheduler.schedulers.background"] = bg_mod

import app as app_module


def _ensure_global_test_client() -> None:
    flask_app = getattr(app_module, "app", None)
    if flask_app is None:
        raise RuntimeError("app module is missing Flask app instance")
    flask_app.testing = True
    test_client = getattr(flask_app, "test_client", None)
    if not callable(test_client):
        raise RuntimeError("Flask app instance does not expose a callable test_client")


_ensure_global_test_client()


@pytest.fixture
def flask_app():
    _ensure_global_test_client()
    return app_module.app


@pytest.fixture
def client(flask_app):
    with flask_app.test_client() as c:
        yield c


@pytest.fixture
def app_context(flask_app):
    with flask_app.app_context():
        yield


@pytest.fixture(autouse=True)
def reset_runtime_state():
    try:
        import execution

        execution.RUNTIME_STATE.clear()
    except Exception:
        pass
    try:
        import app

        app.RUNTIME_STATE.clear()
    except Exception:
        pass
    yield
