# x402 exact/lnbtc — Phase 0 読解結果と指示書との差分

対象仕様: `x402-foundation/x402` の `specs/schemes/exact/scheme_exact_lnbtc.md`
（`main` ブランチ、2026-09-26 に `raw.githubusercontent.com` から取得、760 行 / 42KB）。

指示書「JIN への x402 exact/lnbtc 実装指示書（2026-09-26 版）」第 7 節の指定どおり、
差分は仕様を正とする。

## 検証できなかったこと

- PR #2861 / #1873 / #3571 / #3572 の存在と状態は未確認。この実行環境からは
  `api.github.com` と `github.com` の HTML がどちらも 403 で、通るのは
  `raw.githubusercontent.com` だけ。`main` に仕様ファイルが存在することは確認できたので
  仕様側がマージ済みという記述とは整合する。PR 番号そのものは未検証。
- `mechanisms/lnbtc`（Python 実装）は未マージのため raw では取得できず、未読。

## 仕様と一致していた点

scheme / asset / CAIP-2 ネットワーク識別子 / bolt11 が唯一の transfer method /
upfront が唯一の flow / amount はミリサトシの正整数文字列 / 402 ごとに新規 invoice /
invoice の署名鍵は payTo / expiry は maxTimeoutSeconds と一致 /
RFC 8785 JCS の SHA-256 を description hash にコミット / バインディング内容 /
プロファイル `http:1` `mcp:1` / `SHA-256(preimage) == payment_hash` /
消費は保護対象ハンドラ実行前 / 再起動耐性のあるリプレイストア。

## 差分（仕様が正）

### 1. リプレイストアのキーは `network:payment_hash`

指示書は「`payment_hash` をキーに」としているが、仕様は消費キーを ASCII 文字列
`network + ":" + payment_hash` と定め、「同じハッシュでもネットワークが違えば別キー」と
明記している。`payment_hash` 単体だと testnet と mainnet が同一キー空間に入る。
testnet 先行 → mainnet という移行計画があるため、ここは実装前に直す必要がある。

### 2. リプレイ項目の保持期限が指示書に無い

仕様は「`invoice_end + skew` の少なくとも 1 時間後まで項目を残す」「invoice がまだ検証を
通る間は削除してはならない」と要求する。指示書には保持期限の記述が無く、TTL を短く
設定すると、まだ有効な invoice の再利用が通る。

### 3. skew は 2 つではなく 1 つの設定値

指示書は「時刻スキュー = now + 60 秒」と「再送の猶予 = 60 秒」を別項目として並べているが、
仕様ではどちらも同一の `skew`（既定 60 秒、非負）である。用途は 2 か所。

- 作成時刻の上限: `invoice_creation_time <= settlement_time + skew`
- paid-but-expired: `settlement_time <= invoice_end + skew`（境界は有効）

設定を 2 つに分けると仕様と乖離する。

### 4. 検証とリプレイストアはファシリテータの `/settle` 側

仕様の手順は、resource server が payload をファシリテータの `/settle` に送り、
ファシリテータが証明を検証して payment_hash を原子的に記録する。`upfront` flow では
`/verify` を呼んではならない。指示書は検証を resource server のローカル処理として
書いているが、Phase 3 まで自前検証とするなら「自社でファシリテータ役を運用する」と
位置づけたうえで `/settle` 境界を実装する必要がある。

なお、リクエストハッシュの再計算は resource server の責務である（仕様:
paid retry では実行されるリクエストから再計算し、`accepted.extra` や
`PaymentPayload.resource` や accepted invoice から取ってはならない）。

### 5. 指示書第 3 節の表に無い必須要件

- BOLT11 通貨がネットワークと一致すること（mainnet `bc` / testnet `tb`）。
- `payTo` は圧縮 secp256k1 公開鍵、小文字 16 進 66 文字。
- accepted invoice は description hash をちょうど 1 つ持ち、inline description を
  持たないこと（違反は `invalid_exact_lnbtc_invoice_description`）。
- `requirements.extra.invoice` と `accepted.extra.invoice` の一致を要求してはならない。
  決済には accepted 側を使う。
- `SettlementResponse.transaction` は小文字の payment hash、`payer` は省略必須。
- Lightning の過払いがあっても invoice 額で有効な証明を受理する。過払いに追加の
  権利は与えない。返金経路は無い。

## JIN 固有の実装上の問題（指示書に無い）

### A. 有料エンドポイントは TypeScript であって Python ではない

指示書第 1 節は「x402 Python 実装の入手: PR #1873 を vendoring」としているが、
JIN の `series` / `movers` の 402 チャレンジを出しているのは
`src/lib/x402-route.ts` の `withSolanaOnlyPaywall` で、`@x402/core` 2.18 +
`@x402/next` 2.18（Node ランタイム、Next.js 16）である。`api/x402.py` は
CLAUDE.md Phase 6 の FastAPI 雛形で、`jin.x402jp.com` の配信経路ではない。

Python の `mechanisms/lnbtc` をそのまま vendoring しても現行の有料経路には挿さらない。
選択肢は 2 つ。

1. lnbtc を TypeScript で `@x402/core` に対して実装する。
2. Python 実装を自前ファシリテータとして別サービスに立て、Next.js の resource server が
   その `/settle` を呼ぶ。仕様の役割分担にそのまま一致するので、こちらのほうが素直。

どちらでも、リクエストハッシュの再計算は TypeScript 側に必要（上記 4）。

### B. Vercel 上では SQLite のリプレイストアが成立しない

指示書は「リプレイストア: SQLite（単一ホスト前提）」とし、同時に
「複数ホストで SQLite を使ってはならない」と書いている。JIN は Next.js を Vercel の
サーバーレス関数で動かしており、ファイルシステムは揮発、実行インスタンスは複数同時に
立つ。単一ホスト前提は構造的に成立しない。

resource server 側に置くなら最初から外部のトランザクショナルストア
（一意制約付き Postgres、`SET NX` の Redis 等）が必要。上記 A の選択肢 2 で
ファシリテータを単一ホストの別サービスにするなら、そこでは SQLite が成立する。

この判断は Phase 1 の受け入れ条件（リプレイストアの再起動耐性テスト）に直接かかるため、
ストアの実体を書く前に決める必要がある。

## Phase 1 のうち実装済みの範囲

実ノード・実支払い・ネットワークを使わない純ロジックのみ。

- `src/lib/jcs.ts` — RFC 8785。重複メンバ名・孤立サロゲート・非有限数を拒否する。
- `src/lib/lnbtc.ts` — ネットワーク定数と BOLT11 通貨、`http:1` / `mcp:1` の
  バインディング構築、`descriptionBytes` / `requestHash`、ヘッダと metadata の
  valueHash（不在 = SHA-256(0x00)）、`verifyPreimage`、`consumptionKey`、
  `checkInvoiceTiming`、`replayRetainUntil`、`assertMillisatoshiAmount`。
- `tests-ts/lnbtc-binding.test.ts` — 仕様の "Request Binding Test Vectors" を
  既知応答テストとして固定（35 件）。HTTP / MCP の正規化文字列 1 行完全一致、
  仕様に載っている 6 つのダイジェスト、不在と present null の区別、
  境界時刻（skew ちょうど通す / +1 秒で拒否）、消費キーのネットワーク分離。

未実装。invoice の発行と decode、BOLT11 署名検証、リプレイストアの実体、
`/settle` エンドポイント、402 accepts への lnbtc エントリ追加。

---

# Phase 1 の進捗（2026-09-27、v2 指示書に対応）

## PR #1873 は取得できた

v1 の時点では「未マージのため取得できない」としたが、git プロトコルなら取れる。

```sh
git fetch --depth 1 https://github.com/x402-foundation/x402 refs/pull/1873/head
```

`refs/pull/1873/head` = `2d85314c42fab607395c78cb09ecc926110252ff`。これで PR #1873 の
存在も確認できた（HTTP API と HTML は 403 のままなので、他の PR 番号は未検証）。

## vendoring の方針

`python/x402/mechanisms/lnbtc` を `facilitator/vendor/lnbtc/` に取り込んだ。SDK 本体
（`x402.schemas` / `x402.interfaces`）は pypi の `x402==2.24.0` を使う。PR の
`python/x402/pyproject.toml` も version 2.24.0 なので一致する。

vendoring したファイルへの変更は相対 import の絶対化のみ。

```
from ...schemas   -> from x402.schemas
from ....schemas  -> from x402.schemas
from ....interfaces -> from x402.interfaces
```

上流がマージされたら `facilitator/vendor/` を削除して pypi 版へ切り替える。差分を保つため
`pyproject.toml` の ruff に `exclude = ["facilitator/vendor"]` を入れ、整形も lint もしない。

追加依存は `[project.optional-dependencies].lnbtc` に分離した。
`x402==2.24.0` / `bolt11` / `bech32` / `coincurve` / `rfc8785`。
PR 側は lnbtc 用の optional-dependency group を追加していないので、ここは自前で定義した。

## 実装の正しさを突き合わせた結果

PR の実装は仕様どおりだった。読み違えかけた点を1つ記録しておく。`validation.py` の
`validate_invoice(check_expiry=True)` は skew を足さずに期限判定するが、`/settle` 経路は
`check_expiry=False` で呼び、facilitator 側で `invoice.date + invoice.expiry + skew` を
使っている。仕様の paid-but-expired ポリシーに一致する。`check_expiry=True` は支払う前の
クライアント検証用で、そこで猶予に頼らないのは妥当。

`retain_until` も `invoice_end + skew + REPLAY_RETENTION_SECONDS(3600)` で仕様の下限どおり。

Python の binding と TypeScript の `src/lib/lnbtc.ts` が、仕様の HTTP / MCP ベクタで
同一ダイジェストを出すことを確認した。独立に書いた 2 実装が仕様値に一致している。

## PR に無くて足したもの

### 保持期限の掃除（`facilitator/replay.py`）

PR の `SQLiteReplayStore` は `retain_until` を記録するが、行を削除する処理が無い。
`RetainingSQLiteReplayStore` で以下を足した。

- 保持期限の下限。v2 §2 の固定 24 時間と、仕様が要求する
  `invoice_end + skew + 1h` の大きいほうを採る。固定 24 時間だけにすると
  `maxTimeoutSeconds` が長い場合に仕様下限を割る（テストで固定した）。
- `purge(now)`。`retain_until <= now` の行だけを削除する。invoice がまだ検証を通る間は
  絶対に消えないことをテストで示した。

### `/settle` の HTTP 境界（`facilitator/settle.py`）

- `POST /settle`。成功は 200、失敗は 402 で `errorReason` のみ返す。
  `payer` は出さない（仕様上省略必須）。`error_message` は入力依存の詳細を含み得るので
  ワイヤに出さない。
- `POST /verify` は実装しない。`upfront` flow の誤用として 400 +
  `invalid_exact_lnbtc_payment_flow` を返す。
- arming guard。`allow_mainnet=False`（既定）では mainnet の network 識別子を
  検証にも消費にも進ませず 400 で拒否する。
- `GET /healthz` で許可ネットワーク・skew・保持期限を確認できる。

### TypeScript 側の公開オリジン固定

v2 §6 の要求。Python の `http_request_binding` は `public_origin` を必須にして
URL のオリジン一致を検査していたが、こちらの TS 実装には無かった。`httpBinding` に
`publicOrigin` を必須引数として足し、不一致と path/query 付きオリジンを拒否する。
リクエストハッシュを再計算するのは resource server（TS 側）なので、ここが効く。

## 検証

- Python 183 件（新規 26 件）。仕様ベクタ一致、否定系（preimage 不一致・別リクエストへの
  付け替え・inline description・通貨不一致）、境界時刻（`invoice_end + skew` ちょうど通す /
  +1 秒で拒否、作成時刻 `settlement + skew` ちょうど通す / +1 秒で拒否）、
  消費キーのネットワーク分離、同時実行（16 スレッド × 64 回で成功 1 件のみ、
  `/settle` 同時 16 回で成功 1 件のみ）、再起動耐性、保持期限と掃除、`/settle` の HTTP 境界。
- TypeScript 48 件。`npm run typecheck` 成功。
- `npm run build` は exit 0。ただしこのセッションで 1 回だけ exit 1 になった。ログは
  `facilitator.payai.network` へのサンドボックス遮断（403）で、既存の Solana 経路が
  ビルド時に facilitator 初期化を試みて失敗するもの。再実行 2 回はいずれも exit 0。
  コード側の失敗ではない。
- ノード・実支払い・Lightning ネットワークへの通信は一切していない。invoice は
  仕様が公開しているテスト鍵でローカル署名して作っている。

## 残っている判断

- Lightning ノードの選定（v2 §2 は LND 自前 VPS が第一候補、既存 VPS の有無で最終決定）。
- msat 建て価格。`msat = ceil(USD ÷ BTCUSD × 10^11)` の入力となる BTC/USD スポットを
  この実行環境から取得できない（外向き通信はパッケージレジストリと
  `raw.githubusercontent.com` 系のみ）。運用者がスポット値・取得時刻・出典を決めて
  `docs/lnbtc-pricing.md` に記録する必要がある。
- ファシリテータのデプロイ先と、resource server からの認証方法（Vercel から
  単一ホストの `/settle` を叩く経路の保護）。v2 には項目が無い。
