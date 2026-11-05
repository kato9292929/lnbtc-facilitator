#!/usr/bin/env bash
# チャネルを開くための準備（root で流す）。限定パスワードで足りる操作だけを使う。
#   ssh -i ~/.ssh/lnbtc root@153.115.37.3 'bash -s' < deploy/vps/04-liquidity.sh            # 費用の見積もりと状態
#   ssh -i ~/.ssh/lnbtc root@153.115.37.3 'AMOUNT_SAT=30000 bash -s' < deploy/vps/04-liquidity.sh  # invoice も作る
#
# phoenixd は受け取りに容量が足りないと、auto-liquidity（既定 2m sat）でチャネルを開く。
# 費用（サービス手数料 1%＋マイニング手数料）は受け取った額から引かれる。チャネルを開くのに
# 足りない入金は返金できない fee credit になるので、見積もりを上回る額を 1 回で払う。
set -euo pipefail

pw=$(cat /etc/lnbtc/phoenixd-limited-password)
api() { printf 'user = ":%s"\n' "$pw" | curl -fsS -K - "$@"; }

echo "--- 2m sat のチャネルを開く費用の見積もり（sat）"
api "http://127.0.0.1:9740/estimateliquidityfees?amountSat=2000000"; echo
echo "--- 残高と fee credit（sat）"
api http://127.0.0.1:9740/getbalance; echo
echo "--- チャネル"
api http://127.0.0.1:9740/getinfo | python3 -I -c '
import json,sys
d=json.load(sys.stdin)
print("nodeId:", d["nodeId"], "chain:", d["chain"], "channels:", len(d["channels"]))
for c in d["channels"]:
    print(" ", c.get("state"), "capacity:", c.get("capacitySat"), "balance:", c.get("balanceSat"), "inbound:", c.get("inboundLiquiditySat"))'

if [[ -n "${AMOUNT_SAT:-}" ]]; then
  [[ "$AMOUNT_SAT" =~ ^[0-9]+$ ]] || { echo "AMOUNT_SAT は sat の整数で" >&2; exit 1; }
  echo "--- チャネルを開くための invoice（${AMOUNT_SAT} sat、有効 1 時間）"
  api -X POST http://127.0.0.1:9740/createinvoice \
    --data-urlencode "description=lnbtc-facilitator initial liquidity" \
    --data-urlencode "amountSat=${AMOUNT_SAT}" \
    --data-urlencode "expirySeconds=3600" \
    --data-urlencode "externalId=initial-liquidity" \
    | python3 -I -c 'import json,sys; d=json.load(sys.stdin); print("paymentHash:", d["paymentHash"]); print(d["serialized"])'
fi
unset pw
