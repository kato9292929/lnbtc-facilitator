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
