#!/usr/bin/env python3
"""JIN の有料エンドポイントに x402 exact/lnbtc で 1 回払い、往復の記録を残す（本人の Mac で流す）。

    python3 deploy/roundtrip.py [URL]   # 既定 https://jin.x402jp.com/api/jin/series

1. 支払いなしで GET → 402 の PAYMENT-REQUIRED から lnbtc の選択肢と invoice を取り出して表示する
2. 本人が手元のウォレットで invoice を払い、ウォレットに出る preimage（64 桁の 16 進）を貼る
   （入力は画面に出さない。記録にも残さない）
3. preimage を付けて再送 → 200 と PAYMENT-RESPONSE を確かめる
4. 同じ preimage でもう一度送る → 402（duplicate_settlement）になることを確かめる
5. 記録を ~/lnbtc-roundtrip-<日時>.json に保存する（リクエスト・402・invoice・200・再送の結果）

標準ライブラリだけを使う。
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://jin.x402jp.com/api/jin/series"


def b64json(value: str | None):
    if not value:
        return None
    return json.loads(base64.b64decode(value.strip()).decode())


def get(url: str, headers: dict[str, str] | None = None):
    req = urllib.request.Request(url, headers={"accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, dict(res.headers), res.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def header(headers: dict[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def line(ok: bool, text: str) -> None:
    print(("OK  " if ok else "NG  ") + text)


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    record: dict = {"url": url, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    out = Path.home() / f"lnbtc-roundtrip-{time.strftime('%Y%m%d-%H%M%S')}.json"

    status, headers, _ = get(url)
    required = b64json(header(headers, "payment-required"))
    record["challenge"] = {"status": status, "payment_required": required}
    accepts = (required or {}).get("accepts", [])
    offers = [a for a in accepts if str(a.get("network", "")).startswith("lnbtc:")]
    if status != 402 or not offers:
        line(False, f"402 に lnbtc がありません（status {status}）。"
                    "JIN の LNBTC_ENABLED と facilitator を確かめる")
        out.write_text(json.dumps(record, ensure_ascii=False, indent=2))
        print(f"記録：{out}")
        return 1
    accepted = offers[0]
    invoice = accepted["extra"]["invoice"]
    sats = int(accepted["amount"]) // 1000
    line(True, f"1/4 402 に lnbtc（{sats} sats、{accepted['network']}）")
    timeout = accepted["maxTimeoutSeconds"]
    print(f"\n次の invoice を手元のウォレットで払ってください（{sats} sats、有効 {timeout} 秒）。")
    print(f"\n{invoice}\n")

    preimage = ""
    while not re.fullmatch(r"[0-9a-f]{64}", preimage):
        prompt = "払ったら、ウォレットに出る preimage（64 桁の 16 進）を貼って Enter"
        prompt += "（画面には出ません）："
        preimage = getpass.getpass(prompt).strip().lower()
    payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
    record["invoice"] = invoice
    record["payment_hash"] = payment_hash

    payment = {
        "x402Version": 2,
        "resource": {"url": url},
        "accepted": accepted,
        "payload": {"preimage": preimage},
    }
    signature = base64.b64encode(json.dumps(payment).encode()).decode()
    status, headers, body = get(url, {"payment-signature": signature})
    response = b64json(header(headers, "payment-response"))
    record["paid"] = {
        "status": status,
        "payment_response": response,
        "body_bytes": len(body),
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "body_head": body[:200].decode(errors="replace"),
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    paid_ok = status == 200 and bool(response) and response.get("transaction") == payment_hash
    detail = f"（transaction {payment_hash[:16]}…）" if paid_ok else ""
    line(paid_ok, f"2/4 preimage を付けた再送が {status}{detail}")

    status, headers, _ = get(url, {"payment-signature": signature})
    again = b64json(header(headers, "payment-required")) or {}
    record["replay"] = {"status": status, "error": again.get("error")}
    replay_ok = status == 402 and again.get("error") == "duplicate_settlement"
    line(replay_ok, f"3/4 同じ preimage の再送は {status}（{again.get('error')}）")

    record["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    line(True, f"4/4 記録を保存（preimage は含めない）：{out}")
    print(f"PAYMENT_HASH={payment_hash}")
    return 0 if paid_ok and replay_ok else 1


if __name__ == "__main__":
    sys.exit(main())
