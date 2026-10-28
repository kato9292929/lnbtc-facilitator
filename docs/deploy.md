# デプロイ手順（下書き）

2026-10-28 時点の下書き。VPS はまだ決まっていない。Voltage の項目は公開情報で埋めたもので、実ノードではまだ確かめていない。Voltage と Cloudflare の画面の名前は変わることがあるので、手順どおりに見つからないときは各社のドキュメントで確かめる。確かめていない箇所は TODO と書いてある。

## 0. 全体

```
[JIN・OSD（Vercel）]
   │ HTTPS + CF-Access-Client-Id / CF-Access-Client-Secret + X-JIN-Signature
   ▼
[Cloudflare Access]（サービストークンを持つリクエストだけ通す）
   │ Cloudflare Tunnel（VPS 側からの外向き接続。VPS のポートは開けない）
   ▼
[VPS：ファシリテーター]  127.0.0.1:8000 で待ち受け、SQLite は永続ディスク
   │ HTTPS + invoice 用 macaroon
   ▼
[Voltage：LND（mainnet）]
```

Voltage は testnet を止めていて、代わりの Mutinynet（signet）は x402 の lnbtc 仕様に無い network なので使わない。testnet の往復は行わず、mainnet の少額の往復で確かめる（CLAUDE.md §6 Phase 2）。

## 1. VPS に求める条件

- 永続ディスク（リプレイストアの SQLite を置く。再起動や再作成で消えないこと）
- systemd が動く Linux（以下は Ubuntu LTS を想定）
- NTP による時刻同期（skew の判定に使う。`timedatectl` で `System clock synchronized: yes`）
- 1 台だけで動かす。台数を増やすときは SQLite を共有ストアに置き換える（CLAUDE.md §6 Phase 3）
- 性能はほとんど要らない（最小構成で足りる見込み。TODO：実測）
- 外から入るポートは SSH だけでよい。HTTP は Cloudflare Tunnel を通す
- 固定の外向き IP アドレス（Voltage のノードの API を、この IP からだけ受け付けるように絞るため。§2 の 5）

## 2. Voltage（運用者の作業）

1. Voltage でチームを作り、LND ノードを mainnet で作る
2. ノードの公開鍵（66 桁の 16 進）を控える。これが `LNBTC_PAY_TO` で、JIN・OSD が 402 の `payTo` に載せる値でもある
3. REST の URL（ノードの API エンドポイント。REST のポートは既定で 8080）を控える。これが `LND_REST_URL`
4. macaroon を用意する。権限は `invoices:read` と `invoices:write` だけにする。疎通の確認は invoice の照会で行うので、`info:read` も要らない
   - ダッシュボードの Manage Access → Macaroon Bakery で、この 2 つだけを持つ macaroon を焼く。lncli は要らない
   - lncli で焼くなら `lncli bakemacaroon --save_to invoice-only.macaroon invoices:read invoices:write`。`--timeout` で有効期限も付けられる（期限を付けたら、切れる前に入れ替える手順が要る）
   - 最初から用意されている Invoice macaroon は使わない。LND 0.17 以降ではアドレスの作成など、要らない権限も付いている
   - admin macaroon は使わない
5. ノードの API に接続できる IP を、VPS の固定 IP だけに絞る。ダッシュボードの設定（Settings）か、Voltage API の `POST /organizations/:organization_id/nodes/:node_id/whitelist` で変えられる。許可していない IP から呼ぶと 403 になり、ファシリテーターの `/healthz` は `"node": "unreachable"`（503）を返す
6. invoice を受け取るためのインバウンド流動性（相手側に残高があるチャネル）を用意する。無いと支払いが届かない。やり方は 2 通り
   - 安い方法（おすすめ）：自分のノードからチャネルを開き（オンチェーンの手数料がかかる）、取引所（River、Strike など）への入金やギフトカード（Bitrefill など）の購入で数万 sats を外へ払う。払った分がそのまま受け取れる容量になる。JIN の単価は 1 回 12〜24 sats で、上限も 1,000 sats なので、数万 sats で足りる
   - LSP からチャネルを買う：Olympus、LNBig、Megalith など。Lightning Pool では 1M sats 分のインバウンドで 2,000〜5,000 sats ほどが目安とされる
   - Voltage 自身のインバウンドの提供（以前の 500,000 sats の機能や Voltage Flow）は、今は使えないものとして扱う（Flow は終了。もう一方の機能は今も提供しているか未確認）
   - 相手のノードによっては、小さすぎるチャネルを受け付けない（チャネルの最小額は相手ごとに違う。TODO：開く前に確かめる）

Voltage の REST は信頼された CA が署名した TLS 証明書を使うので、`LND_TLS_CERT_PATH` は空のままでよい。

macaroon と URL は VPS にだけ置く。Vercel には置かない。

## 3. VPS（運用者の作業、手順は Claude が補える）

```bash
sudo useradd --system --create-home --home-dir /opt/lnbtc lnbtc
sudo mkdir -p /var/lib/lnbtc /etc/lnbtc
sudo chown lnbtc:lnbtc /var/lib/lnbtc
sudo chmod 700 /var/lib/lnbtc /etc/lnbtc
```

コードは `/opt/lnbtc/app` に置き、`uv sync --locked` で依存を入れる（uv は公式の手順で入れる）。

`/etc/lnbtc/facilitator.env`（`chmod 600`、所有者 root）に `.env.example` の変数を書く。例：

```
LNBTC_STORE_PATH=/var/lib/lnbtc/replay.db
LNBTC_CREDIT_PATH=/var/lib/lnbtc/credits.db
LNBTC_HMAC_KEYS=<鍵ID>:<秘密>
LNBTC_ALLOW_MAINNET=false
LNBTC_PAY_TO=<ノードの公開鍵>
LND_REST_URL=https://<ノードのエンドポイント>:8080
LND_MACAROON_PATH=/etc/lnbtc/invoice-only.macaroon
LND_NETWORK=mainnet
```

`LNBTC_ALLOW_MAINNET` は往復の直前まで `false` にしておく。`false` のままだと `/invoice` は mainnet の依頼を `unsupported_network` で断り、testnet の依頼は mainnet のノードに送らずに 502 を返す。`/healthz` でノードへの疎通（`"node": "ok"`）だけは先に確かめられる。

macaroon は `/etc/lnbtc/invoice-only.macaroon`（`chmod 640`、グループ lnbtc）。`LND_TLS_CERT_PATH` は書かない（Voltage は信頼された CA の証明書。§2）。自己署名のノードに替えたときだけ、その証明書のパスを入れる。

`/etc/systemd/system/lnbtc-facilitator.service`：

```ini
[Unit]
Description=lnbtc facilitator
After=network-online.target time-sync.target
Wants=network-online.target

[Service]
User=lnbtc
Group=lnbtc
WorkingDirectory=/opt/lnbtc/app
EnvironmentFile=/etc/lnbtc/facilitator.env
ExecStart=/opt/lnbtc/app/.venv/bin/python -m facilitator.main
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/lib/lnbtc
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

掃除（1 時間ごと）。`/etc/systemd/system/lnbtc-purge.service`：

```ini
[Unit]
Description=lnbtc replay store purge

[Service]
Type=oneshot
User=lnbtc
Group=lnbtc
WorkingDirectory=/opt/lnbtc/app
EnvironmentFile=/etc/lnbtc/facilitator.env
ExecStart=/opt/lnbtc/app/.venv/bin/python -m facilitator.purge
```

`/etc/systemd/system/lnbtc-purge.timer`：

```ini
[Unit]
Description=hourly lnbtc replay store purge

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now lnbtc-facilitator.service lnbtc-purge.timer
curl -s localhost:8000/healthz
```

## 4. Cloudflare Tunnel と Access（運用者の作業）

前提：x402jp.com が Cloudflare に載っている。ホスト名は仮に `lnbtc.x402jp.com` とする。

1. Zero Trust のダッシュボードで Tunnel を作る（Networks → Tunnels）。表示されるコマンドで VPS に cloudflared を入れ、サービスとして登録する
2. その Tunnel の公開ホスト名に `lnbtc.x402jp.com` を足し、転送先を `http://localhost:8000` にする
3. Access → Service Auth（Service credentials）でサービストークンを作る。JIN 用と OSD 用で分けると、片方だけ取り消せる。Client Secret は作成時にしか表示されないので、その場で控える
4. Access → Applications で self-hosted のアプリケーションを作り、ドメインを `lnbtc.x402jp.com` にする
5. そのアプリケーションのポリシーを作る。action は Service Auth、include にさっきのサービストークンを入れる。action を Allow にするとログイン画面に飛ばされ、JIN から呼べない
6. 確かめる：トークンなしの `curl https://lnbtc.x402jp.com/healthz` が拒否され、ヘッダを付けると通る

```bash
curl -s https://lnbtc.x402jp.com/healthz -H "CF-Access-Client-Id: <id>" -H "CF-Access-Client-Secret: <secret>"
```

7. JIN・OSD の Vercel の環境変数にトークンの 2 つの値を入れる（JIN・OSD 側の実装で `CF-Access-Client-Id` と `CF-Access-Client-Secret` のヘッダに載せる）

参考：<https://developers.cloudflare.com/cloudflare-one/identity/service-tokens/>

## 4.5 運用者の作業のまとめ

1. VPS を契約する（§1。固定 IP があること）
2. Voltage で mainnet の LND ノードを作り、Macaroon Bakery で invoice 専用の macaroon を焼き、API の接続元を VPS の IP に絞る（§2）
3. Cloudflare Tunnel と Access を設定する（§4）
4. チャネルを開き、インバウンドを作るための最初の送金をする（§2 の 6）。要る資金は、開くチャネルの容量とオンチェーン手数料の合計
5. HMAC 鍵を作り、VPS と JIN・OSD の Vercel に入れる

順番は 1 → 2 → 3。2 の接続元 IP の制限は、1 で VPS の固定 IP が決まってからでないとできない。4 と 5 は 2 の後なら並行して進めてよい。

## 5. mainnet の少額往復（Phase 2 の受け入れ）

運用者の確認を取ってから行う。

1. `/healthz` が `"node": "ok"` を返すことを確かめる
2. `LNBTC_ALLOW_MAINNET=true` にして再起動する
3. JIN の lnbtc の 402 から invoice を受け取り、自分のウォレットで 12 sats を払い、preimage を付けて再送して 200 を受け取る
4. 同じ preimage で再送して `duplicate_settlement` になること、invoice の期限切れのあとは新しい 402 が出ることを確かめる
5. 1,000 sats を超える額の `/invoice` が `amount_exceeds_call_cap` になることを確かめる
6. 往復の記録（invoice、payment_hash、各応答、時刻）を `docs/` に残す。preimage は残さない

## 6. 戻すとき・止めるとき

- Lightning の受付を止めるだけなら `LNBTC_ALLOW_MAINNET=false` にして再起動する。JIN・OSD は `/invoice` の失敗で lnbtc を 402 から外す
- リプレイストアを古いバックアップで戻さない（CLAUDE.md §7）

## 出典

- Voltage の Macaroon Bakery と macaroon の権限：<https://docs.voltage.cloud/macaroons>
- Voltage API（接続元 IP の許可リスト）：<https://docs.voltage.cloud/voltage-api>
- Voltage の testnet 停止と Mutinynet：<https://docs.voltage.cloud/dev-sandbox-mutinynet>
- LND の REST（AddInvoice）：<https://lightning.engineering/api-docs/api/lnd/lightning/add-invoice/>
- インバウンド流動性・TLS 証明書・`lncli bakemacaroon` の `--timeout` は、運用者が Voltage と LND の公開ドキュメントで調べたもの（2026-10-28）
