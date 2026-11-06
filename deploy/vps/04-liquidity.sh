#!/usr/bin/env bash
# チャネルを開くための準備と状態の確認（root で流す）。限定パスワードで足りる操作だけを使う。
#   ssh ... 'bash -s' < deploy/vps/04-liquidity.sh                              # 見積もりと状態（人向け）
#   ssh ... 'MODE=estimate bash -s' < deploy/vps/04-liquidity.sh                # 見積もり（JSON 1 行）
#   ssh ... 'MODE=invoice AMOUNT_SAT=30000 bash -s' < deploy/vps/04-liquidity.sh  # invoice（1 行）
#   ssh ... 'MODE=channels bash -s' < deploy/vps/04-liquidity.sh                # 開いているチャネルの数
#   ssh ... 'MODE=incoming PAYMENT_HASH=<64桁> bash -s' < deploy/vps/04-liquidity.sh  # 着金の状態（preimage は出さない）
#
# phoenixd は受け取りに容量が足りないと、auto-liquidity（既定 2m sat）でチャネルを開く。
# 費用（サービス手数料 1%＋マイニング手数料）は受け取った額から引かれる。チャネルを開くのに
# 足りない入金は返金できない fee credit になるので、見積もりを上回る額を 1 回で払う。
set -euo pipefail

MODE=${MODE:-report}
pw=$(cat /etc/lnbtc/phoenixd-limited-password)
api() { printf 'user = ":%s"\n' "$pw" | curl -fsS -K - "$@"; }

case "$MODE" in
  estimate)
    api "http://127.0.0.1:9740/estimateliquidityfees?amountSat=2000000"; echo
    ;;
  invoice)
    [[ "${AMOUNT_SAT:-}" =~ ^[0-9]+$ ]] || { echo "AMOUNT_SAT は sat の整数で" >&2; exit 1; }
    api -X POST http://127.0.0.1:9740/createinvoice \
      --data-urlencode "description=lnbtc-facilitator initial liquidity" \
      --data-urlencode "amountSat=${AMOUNT_SAT}" \
      --data-urlencode "expirySeconds=86400" \
      --data-urlencode "externalId=initial-liquidity" \
      | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["serialized"])'
    ;;
  incoming)
    # 着金の状態。preimage・invoice は表示しない。
    [[ "${PAYMENT_HASH:-}" =~ ^[0-9a-f]{64}$ ]] || { echo "PAYMENT_HASH は 64 桁の小文字 16 進で" >&2; exit 1; }
    api "http://127.0.0.1:9740/payments/incoming/${PAYMENT_HASH}" | python3 -I -c '
import json,sys
raw=sys.stdin.read().strip()
if not raw: print("{}"); sys.exit(0)
d=json.loads(raw)
keep=("paymentHash","externalId","isPaid","isExpired","requestedSat","receivedSat","fees","completedAt","createdAt")
print(json.dumps({k:d.get(k) for k in keep}))'
    ;;
  channels)
    api http://127.0.0.1:9740/getinfo | python3 -I -c '
import json,sys
d=json.load(sys.stdin)
print(sum(1 for c in d["channels"] if "Normal" in str(c.get("state"))))'
    ;;
  report)
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
    ;;
  *)
    echo "MODE は report・estimate・invoice・incoming・channels のどれか" >&2; exit 1
    ;;
esac
unset pw
