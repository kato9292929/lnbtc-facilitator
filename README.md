# lnbtc-facilitator

x402 の `exact` / `lnbtc`（Bitcoin Lightning）スキームで、支払いの証明を検証して消費するファシリテーターです。JIN（jin.x402jp.com）の有料エンドポイントに Lightning の支払いを足すために作り、OSD（osd.x402jp.com）からも同じものを使う想定です。

仕様の出典は x402 本体の `specs/schemes/exact/scheme_exact_lnbtc.md`（main）です。

## 構成

```
[エージェント] ── 402 / 支払い付きの再送 ──▶ [JIN・OSD（Next.js on Vercel）]
                                                │ リクエストハッシュを再計算して /settle を呼ぶ
                                                ▼
                                      [このファシリテーター（単一ホスト）]
                                        SQLite ──▶ Voltage の LND（mainnet、REST）
                                        （リプレイストア・クレジット台帳）
```

- 検証と消費はファシリテーターの `/settle` で行います。`upfront` フローでは `/verify` を呼ばない仕様なので、`/verify` は実装していません（呼ぶと 400）。
- Vercel はファイルシステムが揮発し、インスタンスも並列に立つので、リプレイストアは置けません。そのため単一ホストのこのサービスに置いています。
- リクエストハッシュの再計算（TypeScript）は resource server 側の責務なので、JIN のリポジトリにあります（ブランチ `claude/sharp-ptolemy-076aoj` の `src/lib/jcs.ts`・`src/lib/lnbtc.ts`）。

## 中身

| パス | 内容 |
|---|---|
| `facilitator/vendor/lnbtc/` | x402 本体の未マージ PR #1873 の `mechanisms/lnbtc`。BOLT11 の decode・署名検証、バインディング、preimage 検証。変更は import の書き換えのみ（`NOTICE`） |
| `facilitator/replay.py` | リプレイストアの保持期限と掃除（PR #1873 に無い部分） |
| `facilitator/settle.py` | `/settle`・`/invoice` の HTTP の口、mainnet の arming guard、1 コール上限、クレジットの口 |
| `facilitator/invoice.py` | ノードが発行した invoice を依頼と照合する |
| `facilitator/main.py` | 起動の入口（環境変数は `.env.example`） |
| `facilitator/lnd.py` | LND の REST に invoice を発行させる受信側アダプタ（Voltage 想定） |
| `facilitator/purge.py` | リプレイストアの掃除を 1 回行うコマンド |
| `facilitator/auth.py` | 呼び出し元の HMAC 認証（`X-JIN-Signature`）とレート制限 |
| `facilitator/credit.py` | 消費後に提供できなかった分のクレジット台帳 |
| `facilitator/pricing.py` | msat 建て価格の固定値（JIN の series 12000 / movers 24000） |
| `docs/lnbtc-phase0.md` | 仕様の読解、指示書との差分、Phase 1 の経緯 |
| `docs/lnbtc-pricing.md` | msat 価格の根拠 |
| `docs/deploy.md` | VPS・Voltage・Cloudflare Access の設定と mainnet の少額往復の手順（下書き） |

## 状態

- Phase 1（ノード無しの純ロジック）は完了。仕様のテストベクタ、否定系・境界、同時実行、再起動耐性、保持期限、HTTP の口と認証をテストで確かめています。
- Phase 1.5（invoice 発行の口・起動の入口・上限・掃除コマンド・CI）は完了。ノードの代わりに偽の受信アダプタで、HTTP を通した発行から `/settle` までをテストしています。
- Phase 2（mainnet での少額往復）は、ノードを Voltage に決めました（Voltage は testnet を止めているため）。LND のアダプタ・`/healthz` のノード疎通・デプロイ手順の下書きまで入っていて、実ノードでの確認は VPS とノードの用意を待っています。

## 動かし方

```sh
uv sync
uv run pytest
uv run ruff check .
```

起動（設定は `.env.example` の変数を環境に入れる。HMAC 鍵が無いと起動しません）:

```sh
uv run python -m facilitator.main
uv run python -m facilitator.purge   # cron / systemd timer から 1 時間ごと
```

ノード・実支払い・ネットワークには接続しません。invoice は仕様が公開しているテスト専用鍵でローカル署名して作っています。

## 秘密情報

HMAC の鍵とノードの資格情報（macaroon など）はコミットしません。リプレイストアとクレジット台帳の SQLite ファイルも `.gitignore` で除外しています。
