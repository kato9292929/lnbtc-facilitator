#!/usr/bin/env python3
"""XServer VPS クラウドの API で、VPS `lnbtc-facilitator` を外から触れる状態にする。

本人の Mac で流す。

    python3 deploy/xserver/open_vps.py            # 実行
    python3 deploy/xserver/open_vps.py --dry-run  # 読むだけ（作成・適用はしない）

- API キーは ~/.config/xserver/api_key から読む。キーは表示しない・ログに出さない
- 何度流しても同じ結果になる（同名のフィルターや同じルールがあれば作り直さない）
- 項目名は https://developer.xserver.ne.jp/api/vps-cloud/openapi.json（2026-11-05 取得）で確かめた
- 標準ライブラリだけを使う。送信元 IPv4 は `curl -4 ifconfig.me` で取る

やること:
1. GET /v1/me でキーの権限を表示する
2. GET /v1/vps-cloud/linux-vps?name=... で API 用の uuid を取る（パネルの UUID とは別物）
3. OS 再インストールの完了を確かめ、止まっていれば起動する
4. パケットフィルター lnbtc を作り、tcp 22（自分の IPv4/32 だけ）・80・443（全許可）を足す
5. メインの NIC（is_main=true）に適用する
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid as uuidlib

API = "https://api.xserver.ne.jp"
VPS = "/v1/vps-cloud"
KEY_PATH = os.path.expanduser("~/.config/xserver/api_key")
SERVER_NAME = "lnbtc-facilitator"
FILTER_NAME = "lnbtc"
# レート制限は 60 回/分。状態の照会は 10 秒以上あける（API の案内どおり）。
POLL_SECONDS = 12


class ApiError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(f"HTTP {status} {code}")
        self.status = status
        self.code = code


def load_key() -> str:
    try:
        with open(KEY_PATH, encoding="utf-8") as handle:
            key = handle.read().strip()
    except OSError:
        sys.exit(f"{KEY_PATH} を読めません")
    if not key:
        sys.exit(f"{KEY_PATH} が空です")
    return key


def request(key: str, method: str, path: str, body: dict | None = None, *, idempotent=False):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if idempotent:
        req.add_header("Idempotency-Key", uuidlib.uuid4().hex)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        try:
            code = json.loads(exc.read())["error"]["code"]
        except Exception:
            code = "unknown"
        raise ApiError(exc.code, code) from None


def my_ipv4() -> str:
    out = subprocess.run(
        ["curl", "-4", "-fsS", "--max-time", "10", "ifconfig.me"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    parts = out.split(".")
    if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        sys.exit(f"送信元 IPv4 を取れませんでした: {out!r}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    key = load_key()

    _, me = request(key, "GET", "/v1/me")
    vps_cloud = me["services"]["vps_cloud"]
    print("キーの権限:", vps_cloud["permission_type"])
    granted = vps_cloud.get("permissions", {})
    for name in ("vps_server_info", "vps_network", "vps_power", "packet_filter", "os_reinstall"):
        print(f"  {name}: {granted.get(name, vps_cloud['permission_type'])}")

    query = urllib.parse.urlencode({"name": SERVER_NAME})
    _, listing = request(key, "GET", f"{VPS}/linux-vps?{query}")
    matches = [v for v in listing["linux_vps"] if v["name"] == SERVER_NAME]
    if len(matches) != 1:
        sys.exit(f"名前が {SERVER_NAME} の VPS が 1 台に絞れません（{len(matches)} 台）")
    server = matches[0]
    vps_uuid = server["uuid"]
    print(f"VPS: {server['name']} status={server['status']} ipv4={server['primary_ipv4']}")

    _, reinstall = request(key, "GET", f"{VPS}/linux-vps/{vps_uuid}/os-reinstall")
    status = reinstall["os_reinstall"]["status"]
    print("OS 再インストール:", status, reinstall["os_reinstall"].get("image_code"))
    if status == "processing":
        sys.exit("OS 再インストールの途中です。終わってから流し直してください")
    if status == "error":
        sys.exit("OS 再インストールが失敗しています。サポートへの連絡が要ります")

    _, power = request(key, "GET", f"{VPS}/linux-vps/{vps_uuid}/power")
    print("電源:", power["power"]["status"])
    if power["power"]["status"] == "stopped" and not args.dry_run:
        request(key, "POST", f"{VPS}/linux-vps/{vps_uuid}/power/start", idempotent=True)
        for _ in range(20):
            time.sleep(POLL_SECONDS)
            _, power = request(key, "GET", f"{VPS}/linux-vps/{vps_uuid}/power")
            if power["power"]["status"] == "running":
                break
        print("電源:", power["power"]["status"])

    source = my_ipv4()
    print("SSH を許可する送信元:", f"{source}/32")
    wanted = [
        {"ethertype": "ipv4", "protocol": "tcp", "port_range_min": 22, "port_range_max": 22,
         "remote_ip_prefix": f"{source}/32"},
        {"ethertype": "ipv4", "protocol": "tcp", "port_range_min": 80, "port_range_max": 80,
         "remote_ip_prefix": None},
        {"ethertype": "ipv4", "protocol": "tcp", "port_range_min": 443, "port_range_max": 443,
         "remote_ip_prefix": None},
    ]
    if args.dry_run:
        print("--dry-run：フィルターの作成と適用はしません")
        return

    query = urllib.parse.urlencode({"name": FILTER_NAME})
    _, filters = request(key, "GET", f"{VPS}/packet-filters?{query}")
    existing = [f for f in filters["packet_filters"] if f["name"] == FILTER_NAME]
    if existing:
        filter_uuid = existing[0]["filter_uuid"]
        print("既存のフィルターを使います:", FILTER_NAME)
    else:
        _, created = request(key, "POST", f"{VPS}/packet-filters", {"name": FILTER_NAME},
                             idempotent=True)
        filter_uuid = created["filter_uuid"]
        print("フィルターを作りました:", FILTER_NAME)

    for rule in wanted:
        label = f"tcp {rule['port_range_min']} from {rule['remote_ip_prefix'] or 'any'}"
        try:
            request(key, "POST", f"{VPS}/packet-filters/{filter_uuid}/rules", rule, idempotent=True)
            print("ルールを足しました:", label)
        except ApiError as exc:
            if exc.status == 409:
                print("ルールは既にあります:", label)
            else:
                raise

    _, ports = request(key, "GET", f"{VPS}/linux-vps/{vps_uuid}/ports")
    mains = [p for p in ports["ports"] if p["is_main"]]
    if len(mains) != 1:
        sys.exit("メインの NIC が 1 つに絞れません")
    port = mains[0]
    attach = f"{VPS}/linux-vps/{vps_uuid}/ports/{port['port_uuid']}/packet-filters/{filter_uuid}"
    request(key, "PUT", attach, idempotent=True)
    print(f"メインの NIC（{port['global_ip_address']}）にフィルター {FILTER_NAME} を適用しました")

    _, ports = request(key, "GET", f"{VPS}/linux-vps/{vps_uuid}/ports")
    for p in ports["ports"]:
        if p["is_main"]:
            for f in p["packet_filters"]:
                for r in f["rules"]:
                    ports_text = f"{r['port_range_min']}-{r['port_range_max']}"
                    source_text = r["remote_ip_prefix"]
                    print(f"  {f['name']}: {r['protocol']} {ports_text} from {source_text}")
    print(f"次：ssh -i ~/.ssh/lnbtc root@{server['primary_ipv4']}")


if __name__ == "__main__":
    try:
        main()
    except ApiError as exc:
        sys.exit(f"XServer API がエラーを返しました: {exc}")
