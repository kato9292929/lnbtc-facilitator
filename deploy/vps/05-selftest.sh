#!/usr/bin/env bash
# facilitator の自己テスト（root で流す）。HMAC 鍵は設定ファイルから読み、表示しない。
#   ssh ... 'bash -s' < deploy/vps/05-selftest.sh
#
# 1. /healthz（ノードの疎通・mainnet の有効化）
# 2. 署名なしの /invoice が 401
# 3. 12,000 msat の /invoice が invoice を返す（mainnet が有効なときだけ。作った invoice は払わない）
# 4. 1,000 sats を超える /invoice が amount_exceeds_call_cap
set -euo pipefail
set -a; . /etc/lnbtc/facilitator.env; set +a
/opt/lnbtc/venv/bin/python -I - <<'PY'
import json, os, time, urllib.error, urllib.request
from hashlib import sha256
import sys
sys.path.insert(0, "/opt/lnbtc/app")
from facilitator.auth import build_header

base = f"http://127.0.0.1:{os.environ.get('LNBTC_PORT', '8000')}"
key_id, _, secret = os.environ["LNBTC_HMAC_KEYS"].split(",")[0].partition(":")
network = "lnbtc:000000000019d6689c085ae165831e93"
failed = False

def call(path, body=None, sign=True):
    raw = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=raw, method="POST" if raw else "GET")
    if raw:
        req.add_header("content-type", "application/json")
        if sign:
            req.add_header("x-jin-signature", build_header(key_id, secret, timestamp=int(time.time()), method="POST", path=path, body=raw))
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

def check(label, ok, detail=""):
    global failed
    failed |= not ok
    print(("OK  " if ok else "NG  ") + label + (f"（{detail}）" if detail else ""))

status, health = call("/healthz")
check("healthz", status == 200 and health.get("node") == "ok", f"node={health.get('node')} networks={len(health.get('networks', []))}")
armed = network in health.get("networks", [])
print(f"--  mainnet の受付: {'有効' if armed else '無効（LNBTC_ALLOW_MAINNET=false）'}")
digest = sha256(b"lnbtc-facilitator selftest").hexdigest()
req = {"amount": "12000", "descriptionHash": digest, "expirySeconds": 60, "network": network}
status, body = call("/invoice", req, sign=False)
check("署名なしの /invoice は 401", status == 401, str(status))
if armed:
    status, body = call("/invoice", req)
    check("12,000 msat の invoice を発行", status == 200 and str(body.get("invoice", "")).startswith("lnbc"), str(status) if status != 200 else "")
    status, body = call("/invoice", dict(req, amount="1000001"))
    check("1,000 sats を超える額は amount_exceeds_call_cap", body.get("errorReason") == "amount_exceeds_call_cap", str(body.get("errorReason")))
else:
    status, body = call("/invoice", req)
    check("mainnet が無効のあいだは unsupported_network", body.get("errorReason") == "unsupported_network", str(body.get("errorReason")))
sys.exit(1 if failed else 0)
PY
