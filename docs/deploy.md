# デプロイ手順（phoenixd on XServer VPS クラウド）

2026-11-06 時点。ノードは Voltage をやめ、phoenixd（ACINQ）を facilitator と同じ VPS に置く。スクリプトは `deploy/` にあり、VPS ではまだ流していない（Claude のセッションからは VPS に届かないため、本人が Mac で流す）。確かめていない箇所は TODO と書いてある。

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
- 2026-11-06 に v0.9.3 の linux-x64 のバイナリをこのリポジトリの作業環境で起動した。ネイティブで JVM は要らない。ただし外の Lightning ピアと electrum に届かない環境では HTTP の口が開かなかったので、実際の API に当てた確認は VPS で初めて行う（`05-selftest.sh`）
- リリースの `SHA256SUMS.asc` は 6AA4 5A4C 209A 2D30 64CF 66BE E434 ED29 2E85 643A（Pierre-Marie Padiou, ACINQ）の署名。v0.9.3 の `phoenixd-0.9.3-linux-x64.zip` の SHA-256 は `4eef88f1…25081b2`。2026-11-05 にここで署名とハッシュを確かめた

## 2. 手順（本人の Mac で 1 本流す）

`deploy/run-from-mac.sh` が §2.1〜2.6 を順に流す。各段階の成否を 1 行ずつ表示し、詳しい出力は `~/lnbtc-deploy-<日時>.log` に残す。止まったときは「対処」とログの最後 30 行を表示する。鍵・パスワード・シードの中身は画面にもログにも出さない。macOS 標準の bash 3.2 で動く。何度流しても同じ結果になる。

| コマンド | すること |
|---|---|
| `bash deploy/run-from-mac.sh` | 2.1〜2.5 を流し、本人への依頼（invoice の支払い・DNS・シードのバックアップ）を表示する |
| `bash deploy/run-from-mac.sh --check` | チャネル・DNS・HTTPS を確かめ、mainnet を有効にして自己テストを流す。JIN の Vercel に入れる値を表示する |
| `bash deploy/run-from-mac.sh --backup-seed` | シードを `~/lnbtc-seed-backup/seed.dat` に写す |
| `bash deploy/run-from-mac.sh --copy-hmac` | JIN 用の HMAC 鍵をクリップボードに写す（Vercel の画面に貼るため） |
| `bash deploy/run-from-mac.sh --roundtrip` | JIN の有料エンドポイントに 12 sats を払い、往復の記録を `~/lnbtc-roundtrip-<日時>.json` に残す（`deploy/roundtrip.py`） |

VPS に入れるコードは、Mac で今いるブランチと同じもの（GitHub と一致していなければ止まる）。

### 2.1 VPS を外から触れる状態にする

`deploy/xserver/open_vps.py`。API キーは `~/.config/xserver/api_key` から読み、表示しない。パケットフィルター `lnbtc` を作り、tcp 22（その Mac の IPv4/32）・80・443 を足して、メインの NIC に適用する。Mac の IP が変わったら流し直す（新しい /32 が足される。古いルールはパネルか API で消す）。SSH の接続先の鍵は `~/.ssh/known_hosts_lnbtc` に分けて記録する（OS 再インストールの前の記録とぶつからないように）。

### 2.2 初期設定（`deploy/vps/01-bootstrap.sh`）

パスワード認証を切る（`PasswordAuthentication no`・`PermitRootLogin prohibit-password`）、更新、自動のセキュリティ更新、chrony、ufw（22・80・443）、ユーザー `phoenixd`・`lnbtc`。

### 2.3 phoenixd（`deploy/vps/02-install-phoenixd.sh`）

ACINQ の利用規約には運用者が同意済み（2026-11-06、`ACCEPT_PHOENIXD_TOS=yes`）。署名とハッシュを確かめてから `/opt/phoenixd` に入れ、systemd で常駐させ、限定パスワードだけを `/etc/lnbtc/phoenixd-limited-password` に写す。nodeId（= `LNBTC_PAY_TO`）を表示する。

### 2.4 facilitator と Caddy（`deploy/vps/03-install-facilitator.sh`・`05-selftest.sh`）

公開リポジトリから取り、`deploy/requirements.txt`（`uv.lock` から書き出したハッシュ付き。CI で一致を確かめる）を `pip --require-hashes` で入れる。`/etc/lnbtc/facilitator.env` を作り、HMAC 鍵を 2 本（`jin-2026-11`・`osd-2026-11`）作る。`LNBTC_ALLOW_MAINNET=false` で始める。Caddy は公式の apt リポジトリから入れる。証明書は DNS の A レコードが向いてから取れる。最後に自己テスト（`/healthz`、署名なしの `/invoice` が 401、mainnet が無効のあいだは `unsupported_network`）。

Python は Ubuntu 24.04 の 3.12 を使う。`bolt11` 2.2.0 が Python 3.13 未満しか受け付けないため（3.12 の venv で全件入ることを確かめた）。

### 2.5 チャネルを開く invoice（`deploy/vps/04-liquidity.sh`）

2m sat のチャネルを開く費用（サービス手数料 1%＋マイニング手数料）を見積もり、`max(30,000, 見積もり + 5,000)` sats（1,000 sats 単位に切り上げ）の invoice を作る（有効 24 時間、説明は `lnbtc-facilitator initial liquidity`）。本人が払うと phoenixd がチャネルを開き、費用を引いた残りが残高になる。見積もりより少ない入金は、返金できない fee credit になる。チャネルがもう開いていれば作らない。

### 2.6 mainnet の有効化と往復（`--check`・`--roundtrip`）

`--check` は、チャネルが開いていて DNS と HTTPS が通ることを確かめてから `LNBTC_ALLOW_MAINNET=true` にし、自己テスト（12 sats の invoice が出る・1,000 sats 超は `amount_exceeds_call_cap`）を流す。mainnet の有効化は、運用者の指示（2026-11-05「mainnet で 12 sats の往復を 1 件成立させる」）による。

`--roundtrip` は JIN の 402 から invoice を取り出して表示し、本人がウォレットで払って preimage を貼る（画面に出さない）と、再送して 200 を確かめ、同じ preimage の再送が `duplicate_settlement` になることを確かめる。phoenixd の着金の状態も記録に足す。記録に preimage は含めない。前提：JIN の lnbtc の組み込み（kato9292929/Japan-Inflation-Nowcast#15）がマージ・デプロイされ、Vercel の環境変数が入っていること。

## 3. 本人の作業

`bash deploy/run-from-mac.sh` の最後に、次の 3 つをまとめて表示する。

1. チャネルを開く invoice の支払い（金額と用途を添える）
2. DNS の A レコード：XServer レンタルサーバーのサーバーパネル →「DNSレコード設定」→ x402jp.com の「選択する」→「DNSレコード追加」で、ホスト名 `lnbtc`・種別 `A`・内容 `153.115.37.3`・TTL 3600・優先度 0 →「確認画面へ進む」→「追加する」（jin・osd の CNAME と同じ画面）
3. シードのバックアップ：`bash deploy/run-from-mac.sh --backup-seed` で Mac に写し、オフラインの媒体に移してから Mac から消す

そのあと JIN の Vercel の環境変数（`LNBTC_ENABLED=true`・`LNBTC_HMAC_KEY_ID=jin-2026-11`・`LNBTC_HMAC_SECRET`・`LNBTC_PAY_TO`・`LNBTC_NETWORK=mainnet`）を入れる。秘密は `--copy-hmac` でクリップボードに写して貼る。OSD 用の鍵（`osd-2026-11`）は作ってあるが、OSD にはまだ lnbtc を組み込んでいないので入れなくてよい。

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
