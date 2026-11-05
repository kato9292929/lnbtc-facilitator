#!/usr/bin/env bash
# phoenixd の導入（root で流す）。
#   ssh -i ~/.ssh/lnbtc root@153.115.37.3 'ACCEPT_PHOENIXD_TOS=yes bash -s' < deploy/vps/02-install-phoenixd.sh
#
# - ACINQ の GitHub リリースから取り、署名（指紋を固定）と SHA-256 を確かめてから入れる
# - API は 127.0.0.1:9740 だけで待ち受ける（外へ出さない）。auto-liquidity は既定（2m）
# - 初回起動で phoenix.conf にパスワードが作られる。facilitator には
#   http-password-limited-access だけを /etc/lnbtc/phoenixd-limited-password に写して渡す。
#   primary（送金ができる）のパスワードは写さない
# - ACINQ の利用規約への同意は運用者が決める。ACCEPT_PHOENIXD_TOS=yes が無ければ止める
#   （規約：https://phoenix.acinq.co/server の案内に従う）
# - シード（/var/lib/phoenixd/.phoenix/seed.dat）の中身は表示しない
set -euo pipefail

VERSION=0.9.3
ASSET="phoenixd-${VERSION}-linux-x64.zip"
# v0.9.3 の SHA256SUMS.asc に載っている値（2026-11-05 に署名を確かめた）。
EXPECTED_SHA256=4eef88f1067f8d3d38c9e8877a690e04caef18f6a007156953e7b74cc25081b2
# リリースに署名している鍵（Pierre-Marie Padiou, ACINQ）。https://acinq.co/pgp/padioupm.asc
SIGNER_FPR=6AA45A4C209A2D3064CF66BEE434ED292E85643A

if [[ $(id -u) -ne 0 ]]; then echo "root で流してください" >&2; exit 1; fi
if [[ "${ACCEPT_PHOENIXD_TOS:-}" != "yes" ]]; then
  echo "ACINQ の phoenixd の利用規約に同意する場合だけ ACCEPT_PHOENIXD_TOS=yes を付けて流してください" >&2
  exit 1
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cd "$work"
export GNUPGHOME="$work/gnupg"
install -d -m 700 "$GNUPGHOME"

base="https://github.com/ACINQ/phoenixd/releases/download/v${VERSION}"
curl -fsSLO "$base/$ASSET"
curl -fsSLO "$base/SHA256SUMS.asc"
curl -fsSL -o signer.asc https://acinq.co/pgp/padioupm.asc

gpg --batch -q --import signer.asc
gpg --batch --status-fd 1 --verify SHA256SUMS.asc 2>/dev/null > verify.txt || true
if ! grep -q "^\[GNUPG:\] VALIDSIG ${SIGNER_FPR} " verify.txt; then
  echo "SHA256SUMS.asc の署名が ${SIGNER_FPR} のものではありません。止めます" >&2
  exit 1
fi
gpg --batch -q -d SHA256SUMS.asc 2>/dev/null > SHA256SUMS
grep " \*${ASSET}\$" SHA256SUMS | sed 's/ \*/  /' | sha256sum -c -
echo "${EXPECTED_SHA256}  ${ASSET}" | sha256sum -c -

unzip -q "$ASSET"
install -d -m 755 /opt/phoenixd
install -m 755 "phoenixd-${VERSION}-linux-x64/phoenixd" /opt/phoenixd/phoenixd
install -m 755 "phoenixd-${VERSION}-linux-x64/phoenix-cli" /opt/phoenixd/phoenix-cli
/opt/phoenixd/phoenixd --version

install -m 644 /dev/stdin /etc/systemd/system/phoenixd.service <<'UNIT'
[Unit]
Description=phoenixd (ACINQ)
After=network-online.target time-sync.target
Wants=network-online.target

[Service]
User=phoenixd
Group=phoenixd
WorkingDirectory=/var/lib/phoenixd
ExecStart=/opt/phoenixd/phoenixd --agree-to-terms-of-service --http-bind-ip=127.0.0.1 --http-bind-port=9740
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/lib/phoenixd
PrivateTmp=true
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now phoenixd

conf=/var/lib/phoenixd/.phoenix/phoenix.conf
for _ in $(seq 1 60); do [[ -s "$conf" ]] && grep -q '^http-password-limited-access=' "$conf" && break; sleep 2; done
if ! grep -q '^http-password-limited-access=' "$conf"; then
  echo "phoenix.conf に限定パスワードがありません。journalctl -u phoenixd を見てください" >&2
  exit 1
fi

# 限定パスワードだけを facilitator が読めるファイルに写す。値は表示しない。
umask 027
grep '^http-password-limited-access=' "$conf" | head -1 | cut -d= -f2- > /etc/lnbtc/phoenixd-limited-password
chown root:lnbtc /etc/lnbtc/phoenixd-limited-password
chmod 640 /etc/lnbtc/phoenixd-limited-password

# 疎通と nodeId（= LNBTC_PAY_TO）。パスワードは curl の引数に載せない（ps に出さない）。
pw=$(cat /etc/lnbtc/phoenixd-limited-password)
for _ in $(seq 1 30); do
  if printf 'user = ":%s"\n' "$pw" | curl -fsS -K - http://127.0.0.1:9740/getinfo > "$work/getinfo.json" 2>/dev/null; then break; fi
  sleep 2
done
unset pw
python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); print("nodeId:", d["nodeId"]); print("chain:", d["chain"]); print("channels:", len(d["channels"]))' "$work/getinfo.json"
ss -ltnp | grep 9740
echo "02-install-phoenixd: 完了（nodeId を LNBTC_PAY_TO に使う）"
