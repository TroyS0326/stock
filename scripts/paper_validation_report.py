#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os, sys, urllib.error, urllib.parse, urllib.request

def parse_args(argv=None):
    p=argparse.ArgumentParser(description='Paper validation session report runner')
    p.add_argument('--base-url', default='http://127.0.0.1:5000')
    p.add_argument('--token')
    p.add_argument('--auth-header', default='X-Operator-Token')
    p.add_argument('--day')
    p.add_argument('--timeout', default=10, type=float)
    return p.parse_args(argv)

def fetch(args, token):
    path='/api/paper-validation-session-report'
    if args.day:
        path += '?' + urllib.parse.urlencode({'day': args.day})
    url=urllib.parse.urljoin(args.base_url.rstrip('/')+'/', path.lstrip('/'))
    headers={'Accept':'application/json'}
    if token:
        headers[args.auth_header]=token
    req=urllib.request.Request(url=url, method='GET', headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as r:
            raw=r.read().decode('utf-8')
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw=e.read().decode('utf-8') if e.fp else ''
        try:data=json.loads(raw) if raw else {}
        except Exception:data={'raw':raw}
        return e.code, data
    except Exception as e:
        return 0, {'ok':False,'error':str(e)}

def main(argv=None):
    args=parse_args(argv)
    token=args.token if args.token is not None else os.getenv('OPERATOR_AUTH_TOKEN')
    status, payload = fetch(args, token)
    data = payload.get('data') if isinstance(payload, dict) else {}
    print('Paper Validation Session Report')
    print(f'- http_status: {status}')
    print(f"- report_status: {(data or {}).get('report_status')}")
    print(f"- acceptance_pass: {bool((data or {}).get('acceptance_pass'))}")
    print(f"- market_day: {(data or {}).get('market_day')}")
    print(f"- required_actions: {(data or {}).get('required_actions', [])}")
    print(json.dumps(payload, sort_keys=True))
    if status == 401:
        return 1
    if status < 200 or status >= 300:
        return 1
    return 0 if bool((data or {}).get('acceptance_pass')) else 1

if __name__=='__main__':
    sys.exit(main())
