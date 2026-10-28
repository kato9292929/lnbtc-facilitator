# lnbtc-facilitator

x402 の `exact` / `lnbtc`（Bitcoin Lightning）スキームで、支払いの証明を検証して消費するファシリテーターです。JIN（jin.x402jp.com）の有料エンドポイントに Lightning の支払いを足すために作り、OSD（osd.x402jp.com）からも同じものを使う想定です。

仕様の出典は x402 本体の `specs/schemes/exact/scheme_exact_lnbtc.md`（main）です。

## 構成

```
[エージェント] ── 402 / 支払い付きの再送 ──▶ [JIN・OSD（Next.js on Vercel）]
                                                │ リクエストハッシュを再計算して /settle を呼ぶ
                                                ▼
                                      [このファシリテーター（単一ホスト）]
                                        Lightning ノード（LND 想定）
                                        SQLite（リプレイストア・クレジット台帳）
```

- 検証と消費はファシリテーターの `/settle` で行います。`upfront` フローでは `/verify` を呼ばない仕様なので、`/verify` は実装していません（呼ぶと 400）。
- Vercel はファイルシステムが揮発し、インスタンスも並列に立つので、リプレイストアは置けません。そのため単一ホストのこのサービスに置いています。
- リクエストハッシュの再計算（TypeScript）は resource server 側の責務なので、JIN のリポジトリにあります（ブランチ `claude/sharp-ptolemy-076aoj` の `src/lib/jcs.ts`・`src/lib/lnbtc.ts`）。

## 中身

| パス | 内容 |
|---|---|
| `facilitator/vendor/lnbtc/` | x402 本体の未マージ PR #1873 の `mechanisms/lnbtc`。BOLT11 の decode・署名検証、バインディング、preimage 検証。変更は import の書き換えのみ（`NOTICE`） |
| `facilitator/replay.py` | リプレイストアの保持期限と掃除（PR #1873 に無い部分） |
| `facilitator/settle.py` | `/settle` の HTTP の口、mainnet の arming guard、クレジットの口 |
| `facilitator/auth.py` | 呼び出し元の HMAC 認証（`X-JIN-Signature`）とレート制限 |
| `facilitator/credit.py` | 消費後に提供できなかった分のクレジット台帳 |
| `facilitator/pricing.py` | msat 建て価格の固定値（JIN の series 12000 / movers 24000） |
| `docs/lnbtc-phase0.md` | 仕様の読解、指示書との差分、Phase 1 の経緯 |
| `docs/lnbtc-pricing.md` | msat 価格の根拠 |

## 状態

- Phase 1（ノード無しの純ロジック）は完了。仕様のテストベクタ、否定系・境界、同時実行、再起動耐性、保持期限、HTTP の口と認証をテストで確かめています。
- Phase 2（testnet での往復）は、Lightning ノードと置き場所の VPS が決まっていないため止まっています。

## 動かし方

```sh
uv sync
uv run pytest
uv run ruff check .
```

ノード・実支払い・ネットワークには接続しません。invoice は仕様が公開しているテスト専用鍵でローカル署名して作っています。

## 秘密情報

HMAC の鍵とノードの資格情報（macaroon など）はコミットしません。リプレイストアとクレジット台帳の SQLite ファイルも `.gitignore` で除外しています。
