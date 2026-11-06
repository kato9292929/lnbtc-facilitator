#!/usr/bin/env bash
# facilitator と Caddy の導入（root で流す。02-install-phoenixd.sh の後）。
#   ssh -i ~/.ssh/lnbtc root@153.115.37.3 'REF=main bash -s' < deploy/vps/03-install-facilitator.sh
#
# - コードは公開リポジトリから REF（既定 main）を取る。依存は deploy/requirements.txt
#   （uv.lock から書き出したハッシュ付き）を pip --require-hashes で入れる
# - 設定は /etc/lnbtc/facilitator.env（root:lnbtc 640）。HMAC 鍵（JIN 用・OSD 用）はここで
#   作り、すでにあれば作り直さない。鍵は表示しない
# - LNBTC_PAY_TO は phoenixd の nodeId。LNBTC_ALLOW_MAINNET は false で始める
#   （mainnet の往復の直前に運用者の確認を取ってから true にする）
# - Caddy で lnbtc.x402jp.com を HTTPS 公開する（証明書は Let's Encrypt から自動で取る。
#   DNS の A レコードが 153.115.37.3 を向いてから取れる）
set -euo pipefail

REF=${REF:-main}
REPO=https://github.com/kato9292929/lnbtc-facilitator
DOMAIN=lnbtc.x402jp.com
APP=/opt/lnbtc/app
ENV_FILE=/etc/lnbtc/facilitator.env

if [[ $(id -u) -ne 0 ]]; then echo "root で流してください" >&2; exit 1; fi
if [[ ! -s /etc/lnbtc/phoenixd-limited-password ]]; then
  echo "先に 02-install-phoenixd.sh を流してください" >&2; exit 1
fi
export DEBIAN_FRONTEND=noninteractive
apt-get -q -y install python3-venv debian-keyring debian-archive-keyring apt-transport-https

# --- コードと依存
install -d -m 755 /opt/lnbtc
if [[ -d "$APP/.git" ]]; then
  git -C "$APP" fetch -q origin "$REF"
  git -C "$APP" checkout -q --detach FETCH_HEAD
else
  git clone -q "$REPO" "$APP"
  git -C "$APP" checkout -q --detach "$REF"
fi
git -C "$APP" log --oneline -1
python3 -m venv /opt/lnbtc/venv
/opt/lnbtc/venv/bin/pip install -q --upgrade pip
/opt/lnbtc/venv/bin/pip install -q --require-hashes --no-deps -r "$APP/deploy/requirements.txt"

# --- 設定（秘密は表示しない）
umask 027
pw=$(cat /etc/lnbtc/phoenixd-limited-password)
node_id=$(printf 'user = ":%s"\n' "$pw" | curl -fsS -K - http://127.0.0.1:9740/getinfo \
  | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["nodeId"])')
unset pw
if [[ ! -s "$ENV_FILE" ]]; then
  jin=$(openssl rand -hex 32)
  osd=$(openssl rand -hex 32)
  cat > "$ENV_FILE" <<CONF
LNBTC_STORE_PATH=/var/lib/lnbtc/replay.db
LNBTC_CREDIT_PATH=/var/lib/lnbtc/credits.db
LNBTC_HMAC_KEYS=jin-2026-11:${jin},osd-2026-11:${osd}
LNBTC_ALLOW_MAINNET=false
LNBTC_PAY_TO=${node_id}
LNBTC_HOST=127.0.0.1
LNBTC_PORT=8000
PHOENIXD_URL=http://127.0.0.1:9740
PHOENIXD_PASSWORD_FILE=/etc/lnbtc/phoenixd-limited-password
PHOENIXD_NETWORK=mainnet
CONF
  unset jin osd
  echo "設定を作りました: ${ENV_FILE}（HMAC 鍵 jin-2026-11・osd-2026-11 を含む）"
else
  sed -i "s/^LNBTC_PAY_TO=.*/LNBTC_PAY_TO=${node_id}/" "$ENV_FILE"
  echo "既存の設定を使います: $ENV_FILE"
fi
chown root:lnbtc "$ENV_FILE"
chmod 640 "$ENV_FILE"
umask 022

# --- systemd
install -m 644 "$APP/deploy/systemd/lnbtc-facilitator.service" /etc/systemd/system/
install -m 644 "$APP/deploy/systemd/lnbtc-purge.service" /etc/systemd/system/
install -m 644 "$APP/deploy/systemd/lnbtc-purge.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now lnbtc-facilitator.service
systemctl restart lnbtc-facilitator.service
sleep 3
# purge はストアのファイルができてから。facilitator の起動で作られる。
systemctl enable --now lnbtc-purge.timer

# --- Caddy（公式の apt リポジトリ）
if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
    | gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q
  apt-get -q -y install caddy
fi
sed "s/__DOMAIN__/${DOMAIN}/" "$APP/deploy/vps/Caddyfile" > /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl reload caddy || systemctl restart caddy

echo "--- 確認"
curl -fsS http://127.0.0.1:8000/healthz; echo
ss -ltnp | grep -E ':8000|:9740|:443|:80 '
echo "03-install-facilitator: 完了。DNS が向いたら curl https://${DOMAIN}/healthz で確かめる"
