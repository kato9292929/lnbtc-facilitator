# デプロイ手順（phoenixd on XServer VPS クラウド）

2026-11-05 時点。ノードは Voltage をやめ、phoenixd（ACINQ）を facilitator と同じ VPS に置く。スクリプトは `deploy/` にあり、VPS ではまだ流していない（このセッションからは VPS に届かないため）。確かめていない箇所は TODO と書いてある。

## 0. 全体

```
[JIN・OSD（Vercel）]
   │ HTTPS + X-JIN-Signature（HMAC）
   ▼
[XServer VPS クラウド lnbtc-facilitator 153.115.37.3（Ubuntu 24.04）]
   パケットフィルター lnbtc：22（運用者の IP/32 だけ）・80・443
   ├ Caddy :443（lnbtc.x402jp.com、Let's Encrypt）
   │   ▼
   ├ facilitator 127.0.0.1:8000（SQLite は /var/lib/lnbtc）
   │   ▼ 限定パスワード
   └ phoenixd 127.0.0.1:9740（外へ出さない。シードは /var/lib/phoenixd/.phoenix/seed.dat）
        ▼
     ACINQ の LSP（チャネルの相手。auto-liquidity でチャネルを開く）
```

Cloudflare Access は使わない（Caddy で直接公開する）。呼び出し元の認証は HMAC だけになる。Vercel の送信元 IP は固定できないので、443 を IP で絞ることもしない。

## 1. phoenixd について確かめたこと

phoenixd v0.9.3 のソース（`src/commonMain/kotlin/fr/acinq/phoenixd/Api.kt`・`Phoenixd.kt`）で確かめた。公式の API ページ（phoenix.acinq.co/server/api）はこの環境から読めなかった。

- 認証は HTTP basic で、パスワードだけを見る。`http-password`（full access）と `http-password-limited-access`（限定）の 2 本。どちらも指定しなければ初回起動で `phoenix.conf` に作られる
- 限定パスワードで呼べないのは送金系（`payinvoice`・`payoffer`・`paylnaddress`・`lnurlpay`・`lnurlauth`・`sendtoaddress`・`bumpfee`・`closechannel`）。`createinvoice`・`getinfo`・`getbalance`・`estimateliquidityfees`・`payments/incoming/*` は呼べる
- `GET /payments/incoming/{paymentHash}` は、支払い前でも preimage を返す。限定パスワードを持つ者は、払わずに `/settle` を通せる。facilitator は応答から preimage を捨てる（`facilitator/phoenixd.py`）。限定パスワードのファイルは root と lnbtc だけが読める
- `POST /createinvoice` は form-urlencoded。`description` と `descriptionHash` は片方だけ。金額は `amountSat`（sat の整数）だけ
- API は既定で 127.0.0.1:9740。`--chain` の既定は mainnet（testnet は testnet3）
- auto-liquidity の既定は 2m sat。チャネルを開く費用の上限は既定で auto-liquidity の 1%（マイニング手数料）。fee credit の上限は既定で 2.5%（2m なら 50,000 sat）
- 起動時にデータディレクトリを 0700、ファイルを 0600 にする（v0.9.3 のリリースノート）
- リリースの `SHA256SUMS.asc` は 6AA4 5A4C 209A 2D30 64CF 66BE E434 ED29 2E85 643A（Pierre-Marie Padiou, ACINQ）の署名。v0.9.3 の `phoenixd-0.9.3-linux-x64.zip` の SHA-256 は `4eef88f1…25081b2`。2026-11-05 にここで署名とハッシュを確かめた

## 2. 手順

順番に流す。1 は本人の Mac で、2〜5 は Mac から VPS に向けて流す。どれも何度流しても同じ結果になる。

### 2.1 VPS を外から触れる状態にする（Mac）

```bash
python3 deploy/xserver/open_vps.py --dry-run
python3 deploy/xserver/open_vps.py
ssh -i ~/.ssh/lnbtc root@153.115.37.3 true
```

API キーは `~/.config/xserver/api_key` から読み、表示しない。パケットフィルター `lnbtc` を作り、tcp 22（その Mac の IPv4/32）・80・443 を足して、メインの NIC に適用する。Mac の IP が変わったら流し直す（新しい /32 が足される。古いルールはパネルか API で消す）。

### 2.2 初期設定

```bash
ssh -i ~/.ssh/lnbtc root@153.115.37.3 'bash -s' < deploy/vps/01-bootstrap.sh
```

パスワード認証を切る（`PasswordAuthentication no`・`PermitRootLogin prohibit-password`）、更新、自動のセキュリティ更新、chrony、ufw（22・80・443）、ユーザー `phoenixd`・`lnbtc`。

### 2.3 phoenixd

```bash
ssh -i ~/.ssh/lnbtc root@153.115.37.3 'ACCEPT_PHOENIXD_TOS=yes bash -s' < deploy/vps/02-install-phoenixd.sh
```

`ACCEPT_PHOENIXD_TOS=yes` は ACINQ の利用規約に同意するという意味で、運用者が決める。署名とハッシュを確かめてから `/opt/phoenixd` に入れ、systemd で常駐させ、限定パスワードだけを `/etc/lnbtc/phoenixd-limited-password` に写す。最後に nodeId（= `LNBTC_PAY_TO`）を表示する。

### 2.4 facilitator と Caddy

```bash
ssh -i ~/.ssh/lnbtc root@153.115.37.3 'REF=main bash -s' < deploy/vps/03-install-facilitator.sh
```

公開リポジトリから取り、`deploy/requirements.txt`（`uv.lock` から書き出したハッシュ付き。CI で一致を確かめる）を `pip --require-hashes` で入れる。`/etc/lnbtc/facilitator.env` を作り、HMAC 鍵を 2 本（`jin-2026-11`・`osd-2026-11`）作る。`LNBTC_ALLOW_MAINNET=false` で始める。Caddy は公式の apt リポジトリから入れる。証明書は DNS の A レコードが向いてから取れる。

Python は Ubuntu 24.04 の 3.12 を使う。`bolt11` 2.2.0 が Python 3.13 未満しか受け付けないため（3.12 の venv で全件入ることを確かめた）。

### 2.5 チャネルを開く

```bash
ssh -i ~/.ssh/lnbtc root@153.115.37.3 'bash -s' < deploy/vps/04-liquidity.sh
ssh -i ~/.ssh/lnbtc root@153.115.37.3 'AMOUNT_SAT=30000 bash -s' < deploy/vps/04-liquidity.sh
```

1 回目で 2m sat のチャネルを開く費用（サービス手数料 1%＋マイニング手数料）を見積もる。2 回目で、見積もりを上回る額の invoice を作る（説明は `lnbtc-facilitator initial liquidity`）。本人が払うと phoenixd がチャネルを開き、費用を引いた残りが残高になる。見積もりより少ない入金は、返金できない fee credit になる。

### 2.6 mainnet の往復

運用者の確認を取ってから `LNBTC_ALLOW_MAINNET=true` にして `systemctl restart lnbtc-facilitator` する。記録には、リクエスト・402・invoice・着金（`incoming_payment` の応答。preimage は含まない）・200 を残す。

TODO：JIN の有料エンドポイントに lnbtc を組み込む作業（CLAUDE.md §6 の JIN 側の作業）は、まだ JIN の main に入っていない。

## 3. 本人の作業

### 3.1 DNS（XServer）

`lnbtc.x402jp.com` の A レコードを `153.115.37.3` に向ける。XServer のどのサービスで x402jp.com の DNS を管理しているか（XServer レンタルサーバーのサーバーパネルか、XServer ドメインの管理画面か）によって画面が違う。TODO：本人に確かめる。入力値はどちらでも次のとおり。

| 項目 | 値 |
|---|---|
| ホスト名 | `lnbtc`（.x402jp.com は画面で補われる） |
| 種別 | `A` |
| 内容 | `153.115.37.3` |
| TTL | 既定のまま（3600 など） |
| 優先度 | 空欄 |

### 3.2 シードのバックアップ

`/var/lib/phoenixd/.phoenix/seed.dat` は資金を取り戻すための唯一の鍵（12 語のシード）。失うと、チャネルの残高を取り戻せない。中身をチャットや画面に貼らない。手元に写すなら Mac で次を流し、オフラインの媒体（暗号化した USB など）に移してから Mac から消す。

```bash
umask 077
scp -i ~/.ssh/lnbtc root@153.115.37.3:/var/lib/phoenixd/.phoenix/seed.dat ~/lnbtc-seed.dat
```

### 3.3 HMAC 鍵を Vercel に入れる

鍵は `/etc/lnbtc/facilitator.env` の `LNBTC_HMAC_KEYS` にある。チャットに貼らず、Mac から VPS を経由して Vercel の CLI に直接渡す（TODO：JIN 側の環境変数名は、JIN の組み込みの実装で決める）。

## 4. 運用

- 状態：`systemctl status phoenixd lnbtc-facilitator caddy`、`curl -s localhost:8000/healthz`
- 掃除：`lnbtc-purge.timer` が 1 時間ごとに `python -m facilitator.purge` を流す
- 止める：`LNBTC_ALLOW_MAINNET=false` にして再起動すると、`/invoice` は mainnet を断る。JIN・OSD は lnbtc を 402 から外す
- リプレイストア（`/var/lib/lnbtc/replay.db`）を古いバックアップで戻さない（CLAUDE.md §7）
- phoenixd の更新：`02-install-phoenixd.sh` の VERSION・EXPECTED_SHA256 を新しいリリースに合わせ、署名を確かめてから流す

## 出典

- phoenixd v0.9.3 のソース：<https://github.com/ACINQ/phoenixd/tree/v0.9.3>
- phoenixd のリリースと署名：<https://github.com/ACINQ/phoenixd/releases/tag/v0.9.3>、<https://acinq.co/pgp/padioupm.asc>
- XServer VPS クラウド API：<https://developer.xserver.ne.jp/api/vps-cloud/>（openapi.json で項目名を確かめた）
