#!/usr/bin/env bash
# VPS の初期設定（Ubuntu 24.04、root で 1 回流す。何度流しても同じ結果になる）。
#   ssh -i ~/.ssh/lnbtc root@153.115.37.3 'bash -s' < deploy/vps/01-bootstrap.sh
#
# - SSH：パスワード認証を切り、root は鍵だけで入れるようにする
# - パッケージの更新、自動のセキュリティ更新、時刻同期（skew の判定に使う）
# - ufw：XServer のパケットフィルターに加えて、ホスト側でも 22・80・443 だけを開ける
# - phoenixd・facilitator を動かすユーザーとディレクトリ
set -euo pipefail

if [[ $(id -u) -ne 0 ]]; then echo "root で流してください" >&2; exit 1; fi
export DEBIAN_FRONTEND=noninteractive

# 鍵で入れることを確かめてから流す前提。authorized_keys が空なら止める（締め出し防止）。
if [[ ! -s /root/.ssh/authorized_keys ]]; then
  echo "/root/.ssh/authorized_keys が空です。パスワード認証を切ると入れなくなるので止めます" >&2
  exit 1
fi

cat > /etc/ssh/sshd_config.d/10-lnbtc.conf <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
CONF
sshd -t
systemctl restart ssh

apt-get update -q
apt-get -q -y upgrade
apt-get -q -y install unattended-upgrades chrony ufw curl unzip gnupg git sqlite3 python3-venv \
  debian-keyring debian-archive-keyring apt-transport-https
dpkg-reconfigure -f noninteractive unattended-upgrades
systemctl enable --now chrony

ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

id phoenixd >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/phoenixd --shell /usr/sbin/nologin phoenixd
id lnbtc >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/lnbtc --shell /usr/sbin/nologin lnbtc
install -d -m 700 -o phoenixd -g phoenixd /var/lib/phoenixd
install -d -m 700 -o lnbtc -g lnbtc /var/lib/lnbtc
install -d -m 750 -o root -g lnbtc /etc/lnbtc

echo "--- 時刻同期"
chronyc tracking | grep -E "Leap status|System time" || true
timedatectl show -p NTPSynchronized
echo "--- ufw"
ufw status | head -8
echo "01-bootstrap: 完了"
