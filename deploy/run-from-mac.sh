#!/bin/bash
# lnbtc-facilitator を XServer VPS クラウドに入れる。本人の Mac で、このリポジトリの中から流す。
#
#   bash deploy/run-from-mac.sh               # 1 回目：VPS を開け、phoenixd・facilitator・Caddy を入れ、依頼を表示する
#   bash deploy/run-from-mac.sh --check       # 依頼を済ませた後：チャネル・DNS・HTTPS を確かめ、mainnet を有効にする
#   bash deploy/run-from-mac.sh --backup-seed # phoenixd のシードを Mac に写す（中身は表示しない）
#   bash deploy/run-from-mac.sh --copy-hmac   # JIN 用の HMAC 鍵をクリップボードに写す（中身は表示しない）
#   bash deploy/run-from-mac.sh --roundtrip   # JIN の有料エンドポイントに 12 sats を払い、往復の記録を残す
#
# - XServer の API キーは ~/.config/xserver/api_key、SSH の秘密鍵は ~/.ssh/lnbtc から読む
# - 何度流しても同じ結果になる。途中で止まったら、表示された対処をしてから同じコマンドを流し直す
# - 鍵・パスワード・シードの中身は画面にもログにも出さない
# - 各段階の成否を 1 行ずつ表示し、詳しい出力は ~/lnbtc-deploy-<日時>.log に残す
#   （失敗したときは、そのログの最後の部分も画面に出す。そのまま貼れば原因を追える）
#
# macOS 標準の bash 3.2 で動くように書いてある。

set -u -o pipefail

VPS_IP=153.115.37.3
DOMAIN=lnbtc.x402jp.com
SSH_KEY="$HOME/.ssh/lnbtc"
API_KEY_FILE="$HOME/.config/xserver/api_key"
KNOWN_HOSTS="$HOME/.ssh/known_hosts_lnbtc"
MIN_LIQUIDITY_SAT=30000
LIQUIDITY_MARGIN_SAT=5000

REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)
LOG="$HOME/lnbtc-deploy-$(date +%Y%m%d-%H%M%S).log"
SSH=(ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10
     -o ServerAliveInterval=15 -o UserKnownHostsFile="$KNOWN_HOSTS" -o StrictHostKeyChecking=accept-new
     "root@$VPS_IP")

ok()   { printf 'OK  %s\n' "$*" | tee -a "$LOG"; }
info() { printf '%s\n' "--  $*" | tee -a "$LOG"; }
ng() {
  # $1: 何が失敗したか / $2 以降: どうすればよいか
  local what=$1; shift
  {
    printf 'NG  %s\n' "$what"
    printf '    対処：%s\n' "$@"
    printf '    ログ：%s\n' "$LOG"
    printf '    --- ログの最後 30 行 ---\n'
    tail -n 30 "$LOG" | sed 's/^/    | /'
  } | tee -a "$LOG" >&2
  exit 1
}
run() { "$@" >>"$LOG" 2>&1; }
remote() { local script=$1; shift; "${SSH[@]}" "$* bash -s" <"$REPO_DIR/deploy/vps/$script"; }
# コマンドだけを流す。手元の標準入力を ssh に読ませない。
rcmd() { "${SSH[@]}" "$@" </dev/null; }

# ------------------------------------------------------------------ 事前確認
preflight() {
  : >"$LOG"; chmod 600 "$LOG"
  info "ログ：$LOG"
  for cmd in python3 curl ssh git; do
    command -v "$cmd" >/dev/null || ng "$cmd が見つかりません" "Xcode Command Line Tools を入れる（xcode-select --install）"
  done
  [ -s "$API_KEY_FILE" ] || ng "XServer の API キーがありません（$API_KEY_FILE）" "API キーをこのファイルに 1 行で保存する"
  [ -s "$SSH_KEY" ] || ng "SSH の秘密鍵がありません（$SSH_KEY）" "鍵をこのパスに置く"
  chmod 600 "$SSH_KEY" "$API_KEY_FILE" 2>/dev/null
  BRANCH=$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD)
  HEAD_SHA=$(git -C "$REPO_DIR" rev-parse HEAD)
  REMOTE_SHA=$(git -C "$REPO_DIR" ls-remote origin "refs/heads/$BRANCH" 2>>"$LOG" | cut -f1)
  [ "$HEAD_SHA" = "$REMOTE_SHA" ] || ng "手元のブランチ $BRANCH が GitHub と一致しません" \
    "git -C $REPO_DIR pull を流してから、もう一度実行する"
  ok "事前確認（ブランチ $BRANCH、${HEAD_SHA:0:7}）"
}

# ------------------------------------------------------------------ 1. XServer API
open_vps() {
  python3 "$REPO_DIR/deploy/xserver/open_vps.py" >>"$LOG" 2>&1 \
    || ng "XServer API でパケットフィルターを適用できませんでした" "ログの「XServer API がエラーを返しました」の行を見る。権限不足ならキーの権限（パケットフィルター：full）を確かめる"
  ok "1/7 パケットフィルター lnbtc を適用（22 はこの Mac の IP だけ、80・443）"
}

wait_ssh() {

  for _ in $(seq 1 30); do
    if rcmd true >>"$LOG" 2>&1; then ok "${1:-}SSH で入れる（root@$VPS_IP）"; return; fi
    sleep 10
  done
  ng "SSH で入れません（5 分待ちました）" \
    "XServer のパネルで VPS が起動しているか確かめる" \
    "OS 再インストールのときに登録した公開鍵が ~/.ssh/lnbtc と対になっているか確かめる"
}

# ------------------------------------------------------------------ 2〜5. VPS
bootstrap() {
  run remote 01-bootstrap.sh || ng "VPS の初期設定に失敗しました" "同じコマンドをもう一度流す（何度流しても同じ結果になる）"
  ok "3/7 初期設定（鍵認証のみ・自動更新・時刻同期・ufw）"
}

install_phoenixd() {
  run remote 02-install-phoenixd.sh ACCEPT_PHOENIXD_TOS=yes \
    || ng "phoenixd を入れられませんでした" "ログに「署名」「sha256」とあれば配布物の検証で止めたもの。そのまま貼って相談する"
  NODE_ID=$(grep '^nodeId:' "$LOG" | tail -1 | awk '{print $2}')
  ok "4/7 phoenixd（署名とハッシュを確認、127.0.0.1:9740、nodeId ${NODE_ID:-不明}）"
}

install_facilitator() {
  run remote 03-install-facilitator.sh "REF=$BRANCH" \
    || ng "facilitator と Caddy を入れられませんでした" "ログを貼って相談する"
  run remote 05-selftest.sh || ng "facilitator の自己テストが通りません" "ログの NG の行を見る"
  ok "5/7 facilitator と Caddy（127.0.0.1:8000、自己テスト OK、mainnet はまだ無効）"
}

# ------------------------------------------------------------------ 6. チャネルを開く invoice
liquidity_invoice() {
  local channels estimate fees amount
  channels=$(remote 04-liquidity.sh MODE=channels 2>>"$LOG") || ng "phoenixd のチャネルを確かめられません" "ログを貼って相談する"
  if [ "${channels:-0}" -gt 0 ]; then
    ok "6/7 チャネルはもう開いている（$channels 本）"
    INVOICE=""
    return
  fi
  estimate=$(remote 04-liquidity.sh MODE=estimate 2>>"$LOG") || ng "チャネルを開く費用を見積もれません" "数分待って流し直す（phoenixd が ACINQ に接続中のことがある）"
  echo "estimate: $estimate" >>"$LOG"
  fees=$(printf '%s' "$estimate" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(int(d["miningFeeSat"])+int(d["serviceFeeSat"]))') \
    || ng "見積もりを読めません" "ログを貼って相談する"
  amount=$(( fees + LIQUIDITY_MARGIN_SAT ))
  amount=$(( (amount + 999) / 1000 * 1000 ))
  [ "$amount" -lt "$MIN_LIQUIDITY_SAT" ] && amount=$MIN_LIQUIDITY_SAT
  INVOICE=$(remote 04-liquidity.sh MODE=invoice "AMOUNT_SAT=$amount" 2>>"$LOG") || ng "invoice を作れません" "ログを貼って相談する"
  INVOICE_AMOUNT=$amount
  FEES=$fees
  ok "6/7 チャネルを開く invoice（$amount sats、費用の見積もり $fees sats）"
}

requests() {
  local seed_cmd="bash deploy/run-from-mac.sh --backup-seed"
  cat <<EOF | tee -a "$LOG"

OK  7/7 VPS の準備が終わりました。ここから先は次の 3 つをお願いします（順番は問いません）。

■ 1. チャネルを開く invoice の支払い
EOF
  if [ -n "${INVOICE:-}" ]; then
    cat <<EOF | tee -a "$LOG"
  金額：${INVOICE_AMOUNT} sats
  用途：phoenixd が受け取り用のチャネル（2,000,000 sats 分）を ACINQ と開く費用（見積もり ${FEES} sats）に充て、残りは phoenixd の残高になります。
  手元の Lightning ウォレットで、次の invoice を払ってください（有効 24 時間）。
  ${INVOICE}
EOF
  else
    echo "  不要です（チャネルはもう開いています）。" | tee -a "$LOG"
  fi
  cat <<EOF | tee -a "$LOG"

■ 2. DNS の A レコード追加（XServer レンタルサーバーのサーバーパネル）
  1) サーバーパネルにログインし、「ドメイン」の欄にある「DNSレコード設定」を押す
  2) ドメインの一覧で x402jp.com の「選択する」を押す
  3) 「DNSレコード追加」のタブを開き、次のとおり入力する
       ホスト名：lnbtc（右側の .x402jp.com はそのまま）
       種別：A
       内容：${VPS_IP}
       TTL：3600（既定のまま）
       優先度：0（既定のまま）
  4) 「確認画面へ進む」を押し、内容を確かめて「追加する」を押す
  jin・osd の CNAME と同じ画面です。反映には数分〜1 時間ほどかかります。

■ 3. phoenixd のシードのバックアップ
  シードは、phoenixd の資金を取り戻すための唯一の鍵です（VPS の /var/lib/phoenixd/.phoenix/seed.dat）。
  このフォルダで次の 1 行を流すと、Mac の ~/lnbtc-seed-backup/seed.dat に写します。中身は表示しません。
    ${seed_cmd}
  写したファイルは、暗号化した USB メモリなどオフラインの媒体に移し、Mac からは消してください。
  ファイルの中身をチャットやメールに貼らないでください。

3 つとも済んだら、このフォルダで次の 1 行を流してください。チャネル・DNS・HTTPS を確かめ、mainnet を有効にします。
    bash deploy/run-from-mac.sh --check
EOF
}

# ------------------------------------------------------------------ --check
check() {
  local channels resolved health
  channels=$(remote 04-liquidity.sh MODE=channels 2>>"$LOG") || ng "phoenixd に問い合わせられません" "ログを貼って相談する"
  [ "${channels:-0}" -gt 0 ] || ng "チャネルがまだ開いていません" \
    "invoice を払った直後なら 10〜30 分待って流し直す（ブロックへの取り込みを待つ）" \
    "払っていなければ bash deploy/run-from-mac.sh を流すと invoice をもう一度表示する"
  ok "1/5 チャネルが開いている（$channels 本）"

  resolved=$(python3 -c "import socket; print(socket.gethostbyname('$DOMAIN'))" 2>>"$LOG") || resolved=""
  [ "$resolved" = "$VPS_IP" ] || ng "$DOMAIN がまだ $VPS_IP を向いていません（いまは ${resolved:-未登録}）" \
    "サーバーパネルの DNS レコードを確かめ、反映を待ってから流し直す"
  ok "2/5 DNS（$DOMAIN → $VPS_IP）"

  run rcmd "systemctl reload caddy" || true
  health=""
  for _ in $(seq 1 12); do
    health=$(curl -fsS --max-time 10 "https://$DOMAIN/healthz" 2>>"$LOG") && break
    sleep 10
  done
  [ -n "$health" ] || ng "https://$DOMAIN/healthz に届きません" \
    "証明書の取得待ちのことがある。数分待って流し直す" \
    "続くようなら ssh で入って journalctl -u caddy --since -10min をログに足して相談する"
  echo "healthz: $health" >>"$LOG"
  ok "3/5 HTTPS（Let's Encrypt の証明書で https://$DOMAIN に届く）"

  run rcmd "sed -i 's/^LNBTC_ALLOW_MAINNET=.*/LNBTC_ALLOW_MAINNET=true/' /etc/lnbtc/facilitator.env && systemctl restart lnbtc-facilitator && sleep 3" \
    || ng "mainnet を有効にできませんでした" "ログを貼って相談する"
  ok "4/5 mainnet の受付を有効にした（LNBTC_ALLOW_MAINNET=true）"

  run remote 05-selftest.sh || ng "mainnet での自己テストが通りません" "ログの NG の行を見る"
  ok "5/5 自己テスト（12 sats の invoice を発行できる・1,000 sats 超は amount_exceeds_call_cap）"
  cat <<EOF | tee -a "$LOG"

facilitator は mainnet で動いています。JIN の Vercel に入れる値は次のとおりです。
  LNBTC_ENABLED=true
  LNBTC_HMAC_KEY_ID=jin-2026-11
  LNBTC_HMAC_SECRET=（bash deploy/run-from-mac.sh --copy-hmac でクリップボードに写して貼る）
  LNBTC_PAY_TO=$(pay_to)
  LNBTC_NETWORK=mainnet
EOF
}

pay_to() { rcmd "grep '^LNBTC_PAY_TO=' /etc/lnbtc/facilitator.env | cut -d= -f2" 2>>"$LOG"; }

backup_seed() {
  local dir="$HOME/lnbtc-seed-backup"
  (umask 077; mkdir -p "$dir")
  ( umask 077; scp -i "$SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o UserKnownHostsFile="$KNOWN_HOSTS" \
      -o StrictHostKeyChecking=accept-new -q "root@$VPS_IP:/var/lib/phoenixd/.phoenix/seed.dat" "$dir/seed.dat" ) >>"$LOG" 2>&1 \
    || ng "シードを写せませんでした" "ログを貼って相談する"
  chmod 600 "$dir/seed.dat"
  ok "シードを $dir/seed.dat に写した（$(wc -c <"$dir/seed.dat" | tr -d ' ') バイト。中身は表示しない）"
  info "オフラインの媒体に移したら、rm -P $dir/seed.dat で Mac から消してください"
}

copy_hmac() {
  command -v pbcopy >/dev/null || ng "pbcopy がありません" "Mac で流す"
  rcmd "grep '^LNBTC_HMAC_KEYS=' /etc/lnbtc/facilitator.env | cut -d= -f2- | tr ',' '\n' | grep '^jin-2026-11:' | cut -d: -f2-" 2>>"$LOG" \
    | tr -d '\n' | pbcopy || ng "鍵を読めませんでした" "ログを貼って相談する"
  ok "JIN 用の HMAC 鍵（jin-2026-11）をクリップボードに写した。Vercel の LNBTC_HMAC_SECRET に貼ったら、別の文字をコピーして上書きしてください"
  info "LNBTC_PAY_TO=$(pay_to)"
}

roundtrip() {
  local url=${1:-https://jin.x402jp.com/api/jin/series} hash status
  python3 -u "$REPO_DIR/deploy/roundtrip.py" "$url" | tee -a "$LOG"
  status=${PIPESTATUS[0]}
  hash=$(grep '^PAYMENT_HASH=' "$LOG" | tail -1 | cut -d= -f2)
  if [ -n "$hash" ]; then
    info "phoenixd の着金：$(remote 04-liquidity.sh MODE=incoming "PAYMENT_HASH=$hash" 2>>"$LOG")"
  fi
  [ "$status" -eq 0 ] || ng "往復が最後まで通りませんでした" "上の NG の行と、保存した記録（~/lnbtc-roundtrip-*.json）を貼って相談する"
  ok "mainnet で 12 sats の往復が通った"
}

case "${1:-}" in
  "")
    preflight; open_vps; wait_ssh "2/7 "; bootstrap; install_phoenixd; install_facilitator; liquidity_invoice; requests ;;
  --check)
    preflight; wait_ssh; check ;;
  --backup-seed)
    preflight; wait_ssh; backup_seed ;;
  --copy-hmac)
    preflight; wait_ssh; copy_hmac ;;
  --roundtrip)
    preflight; wait_ssh; roundtrip "${2:-}" ;;
  *)
    echo "使い方：bash deploy/run-from-mac.sh [--check|--backup-seed|--copy-hmac|--roundtrip [URL]]" >&2; exit 2 ;;
esac
