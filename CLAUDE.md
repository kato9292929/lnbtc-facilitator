# CLAUDE.md — lnbtc-facilitator 作業指示書

このファイルはリポジトリ直下に置く作業指示書で、Claude Code はこれをプロジェクトの前提として読む。チャット履歴は引き継がれないので、作業に必要な判断はすべてここに書く。2026-10-28 時点の状態に基づく。

## 0. これは何か

x402 の `exact` / `lnbtc`（Bitcoin Lightning）スキームのファシリテーター。Lightning の支払いの証明（preimage）を検証して消費する。JIN（jin.x402jp.com）の有料エンドポイントに、既存の Solana USDC と並べて Lightning の支払いを足すために作った。OSD（osd.x402jp.com）からも同じものを使う想定。

仕様の出典は x402 本体の `specs/schemes/exact/scheme_exact_lnbtc.md`（x402-foundation/x402 の main）。仕様と食い違う判断はしない。仕様を取り直して差分が出たら、仕様に合わせてからこのファイルを直す。

経緯は `docs/lnbtc-phase0.md` にある。2026-10-28 に JIN のリポジトリ（kato9292929/Japan-Inflation-Nowcast、ブランチ `claude/sharp-ptolemy-076aoj`）から移した。

## 1. 構成と役割分担

```
[エージェント]
   │ ① GET /api/jin/series
   ▼
[JIN・OSD（Next.js on Vercel）]  ── ② invoice 発行（HMAC 付き）──▶ [このファシリテーター（単一ホストの VPS）]
   │  402 を出す側（resource server）                               Lightning ノード（LND 想定）
   │ ③ 402 + extra.invoice                                         SQLite（リプレイストア・クレジット台帳）
   ▼                                                                    ▲
[エージェント] ④ invoice を支払う ⑤ preimage を付けて再送               │
   ▼                                                                    │
[JIN・OSD] ⑥ リクエストハッシュを再計算 ── ⑦ /settle（HMAC 付き）──────┘
   │                                     検証と消費（ハンドラ実行の前）
   ▼ ⑧ 200 + データ
```

| 役割 | 置き場所 | 中身 |
|---|---|---|
| 402 を出す、リクエストハッシュを再計算する | JIN・OSD のリポジトリ（TypeScript） | JIN では `src/lib/jcs.ts`・`src/lib/lnbtc.ts`。このリポジトリには無い |
| invoice の発行、証明の検証と消費 | このリポジトリ（Python） | `facilitator/` |
| ノードの資格情報、リプレイストア | このリポジトリが動くホストだけ | Vercel には置かない |

このリポジトリを分けた理由は次の 3 つ。

- Vercel はファイルシステムが揮発し、インスタンスも並列に立つので、リプレイストアを置けない
- ノードの資格情報を Vercel に置かずに済む
- JIN と OSD の両方から 1 つのサービスとして使える

## 2. 仕様から決まっていること（変えない）

| 項目 | 要件 |
|---|---|
| scheme / asset | `exact` / `BTC` |
| network（CAIP-2） | mainnet `lnbtc:000000000019d6689c085ae165831e93`、testnet `lnbtc:000000000933ea01ad0ee984209779ba` |
| BOLT11 の通貨 | network と一致（mainnet `bc` / testnet `tb`） |
| transfer method / flow | `bolt11` だけ / `upfront` だけ。`/verify` は呼ばない仕様なので実装しない |
| amount | ミリサトシの正の整数文字列。sats や BTC で持ち回らない（1000 倍の事故になる） |
| invoice | 402 のたびに新しく発行する。description hash をちょうど 1 つ持ち、inline description を持たない。expiry は `maxTimeoutSeconds` と一致 |
| payTo | 66 桁・小文字 16 進の圧縮 secp256k1 公開鍵（受信ノードの鍵） |
| invoice の発行権限 | `payTo` の鍵で署名した invoice を発行できるのは resource server 側だけ。この構成では、認証付きの発行の口を通る JIN・OSD だけ |
| リクエストハッシュ | RFC 8785（JCS）正規化 JSON の SHA-256。resource server が実際に実行するリクエストから再計算する。`accepted.extra` や invoice から取らない |
| 支払いの証明 | `SHA-256(preimage) == payment_hash` をローカルで検証する。ノードに問い合わせない |
| 消費キー | `network + ":" + payment_hash`。payment_hash 単体にしない（testnet と mainnet が衝突する） |
| 消費のタイミング | 保護対象のハンドラを実行する前。後にすると同時実行で二重提供が起きる |
| skew | 既定 60 秒の 1 つの設定値を 2 か所で使う。invoice の作成時刻は settle 時刻 + 60 秒まで有効（+61 秒は無効）、支払い後の再送も期限 + 60 秒まで有効 |
| 保持期限 | `invoice_end + skew` の 1 時間後以降まで消さない。invoice がまだ検証を通る間は削除しない |
| SettleResponse | `transaction` は小文字の payment_hash。`payer` は出さない（省略が必須） |
| 過払い | invoice の金額で受理し、追加の権利は与えない。返金の経路は無い |

## 3. 運用で決めたこと

| 項目 | 決定 | 実装 |
|---|---|---|
| 保持期限 | 24 時間固定。ただし仕様の下限（`invoice_end + skew + 1h`）のほうが後なら下限を採る | `facilitator/replay.py` |
| 価格 | series 12000 msat（12 sats）/ movers 24000 msat（24 sats）。基準 BTC/USD 84,464.00（Bybit、2026-09-27）。見直しは月初と、基準から ±30% 乖離したとき | `facilitator/pricing.py`、`docs/lnbtc-pricing.md` |
| mainnet の上限 | 1 コール 1,000 sats 以下 | `pricing.exceeds_call_cap()` はあるが、まだどこからも使っていない（Phase 1.5） |
| 呼び出し元の認証 | 外側は Cloudflare Access のサービストークン、内側は HMAC | `facilitator/auth.py` |
| HMAC | ヘッダ `X-JIN-Signature: t=<unix秒>,k=<鍵ID>,v1=<16進>`。署名対象は timestamp・method・path・body。時刻ずれ 30 秒超は拒否。定数時間比較。鍵を 2 本同時に受理して入れ替える | 同上 |
| レート制限 | 鍵 ID ごと、超過は 429。既定 120 回 / 60 秒（根拠のある数字ではない） | 同上 |
| エラー応答 | `errorReason` だけ返す。`error_message` と `payer` は出さない | `facilitator/settle.py` |
| 消費後に提供できなかった分 | クレジット台帳に記録し、次の再送で 1 回だけ消費なしに提供する。`/settle` の意味は変えない（仕様が二重提出に `duplicate_settlement` を求めるため） | `facilitator/credit.py`、`POST /credits`・`POST /credits/claim` |
| mainnet の誤投入防止 | 既定は testnet だけ受け付ける。`allow_mainnet=True` のときだけ mainnet を通す | `facilitator/settle.py` |

署名対象に method と path を含めたのは、運用者の指示書（v3、timestamp + body）への追加。body だけだと、`/settle` 向けの署名をそのまま `/credits/claim` へ投げ替えられるため。JIN・OSD 側の署名の作り方もこれに合わせる。

## 4. 今の状態（2026-10-28）

できていること（Phase 1、ノード無し）。

- `facilitator/vendor/lnbtc/`：x402 本体の未マージ PR #1873（`refs/pull/1873/head` = `2d85314`）の `mechanisms/lnbtc`。BOLT11 の decode と署名検証、バインディング、preimage 検証、SQLite のリプレイストア（`INSERT OR IGNORE` による消費）
- `facilitator/replay.py`：保持期限の下限と、期限切れの行だけを消す `purge()`
- `facilitator/settle.py`：`create_app()`。`/healthz`・`/settle`・`/credits`・`/credits/claim`、`/verify` は 400
- `facilitator/auth.py`・`facilitator/credit.py`・`facilitator/pricing.py`
- テスト 57 件：仕様ベクタとの一致、否定系、境界時刻の両側、消費キーのネットワーク分離、同時実行（成功は 1 件だけ）、再起動をまたいだ消費済みの保持、保持期限と掃除、HTTP の口・認証・429

まだ無いもの。

- invoice 発行の口（構成図の ②）
- 起動の入口と、環境変数からの設定の読み込み（`create_app()` は引数で受け取るだけ）
- 1 コール上限の強制
- 掃除（`purge()`）の定期実行
- BOLT11 の feature フィールドの非互換を固定するテスト
- Lightning ノードへの接続（受信側のアダプタ）
- CI

## 5. 作業の進め方

- 返答・進捗・コミットメッセージは日本語。太字は使わない
- 実装していないものや確かめていないものを「動いた」と書かない。分からないことは TODO と書く
- 秘密情報（HMAC 鍵、ノードの macaroon、TLS 証明書、Cloudflare のトークン）はコミットしない。`.env` は `.gitignore` 済み。例は `.env.example` に値なしで書く
- `facilitator/vendor/` は書き換えない。上流との差分を保つため ruff の対象外にしてある。直す必要が出たら、その理由を NOTICE と `docs/` に書き、上流の PR にも伝える
- 決済の経路（`/settle`、invoice 発行、認証、消費キー、金額、network）に触る変更は、テストを足してから入れる。外向きの操作（mainnet の有効化、本番ホストへのデプロイ）は運用者の確認を取ってから行う
- 作業の前後で `uv run pytest` と `uv run ruff check .` を通す

## 6. フェーズ

### Phase 1.5：ノード無しで足せる残り（いま着手できる）

1. 受信側アダプタの口を切る。`facilitator/vendor/lnbtc/types.py` の `LightningReceiver`（`create_invoice(amount_msat, description_hash, expiry_seconds, network)`）を実装する形にする。テスト用に、仕様のテスト専用鍵でローカル署名する偽のアダプタを置く（`tests/test_lnbtc_facilitator.py` の `make_invoice` と同じ作り方）
2. invoice 発行の口 `POST /invoice` を足す。入力は `amount`（msat の文字列）・`descriptionHash`（64 桁の小文字 16 進）・`expirySeconds`・`network`。HMAC 認証とレート制限は `/settle` と同じにする。返すのは invoice の文字列だけ
   - ファシリテーターはリクエストハッシュを計算しない。JIN・OSD が計算したハッシュを受け取って invoice に載せるだけ
   - 許可していない network（既定では mainnet）、正の整数でない amount、1 コール上限を超える amount は拒否する
   - 発行した invoice を decode し直して、description hash・金額・expiry・通貨・署名鍵が依頼どおりかを確かめてから返す（ノードの設定ずれを外に出さない）
3. 起動の入口を足す。`facilitator/main.py` で環境変数から設定を読み、`create_app()` に渡す。変数名の案は次のとおりで、`.env.example` に値なしで書く

| 変数 | 内容 |
|---|---|
| `LNBTC_STORE_PATH` | リプレイストアの SQLite ファイル（永続ディスク上） |
| `LNBTC_CREDIT_PATH` | クレジット台帳の SQLite ファイル |
| `LNBTC_HMAC_KEYS` | `鍵ID:秘密,鍵ID:秘密` の形。入れ替え中は 2 本 |
| `LNBTC_ALLOW_MAINNET` | `true` のときだけ mainnet を受け付ける。既定 false |
| `LNBTC_SKEW_SECONDS` | 既定 60。設定は 1 つだけ |
| `LNBTC_MAX_SATS_PER_CALL` | 既定 1000 |
| `LNBTC_RATE_LIMIT` / `LNBTC_RATE_LIMIT_WINDOW_SECONDS` | 既定 120 / 60 |
| `LND_REST_URL` / `LND_MACAROON_PATH` / `LND_TLS_CERT_PATH` | Phase 2 で使う。macaroon は invoice 発行と照会に絞ったもの |

   - HMAC 鍵が 1 本も無いときは起動を止める。いまの `create_app()` は鍵が無いと認証なしで動くが、それはテスト用に限る
   - uvicorn は 1 プロセスで動かす。レート制限がプロセス内のメモリにあるため
4. 1 コール上限を `/settle` と `/invoice` の両方で強制する（`pricing.exceeds_call_cap()`）
5. 掃除の定期実行。`python -m facilitator.purge` のような 1 回きりのコマンドにして、ホストの cron か systemd timer から 1 時間ごとに呼ぶ。期限内の行を消さないことは `purge()` のテストで固定済み
6. BOLT11 の feature フィールドの非互換をテストに固定する。仕様の初期フィクスチャは feature フィールドを省いていて、LDK などの実装は invalid features として拒否する。検証を緩めて通すのではなく、「この invoice はどこで拒否されるか」をテストに残す
7. CI を足す（GitHub Actions で `uv run pytest` と `uv run ruff check .`）

受け入れ条件：ノードに接続せず、偽のアダプタで「invoice 発行 → 支払い（preimage を使う）→ `/settle` 成功 → 同じ証明で `duplicate_settlement`」が HTTP の口を通して通る。上限・network・認証の否定系も通る。

### Phase 2：testnet での往復（運用者の準備が要る）

このリポジトリ側の作業。

1. LND の REST に invoice を発行するアダプタを書く（`AddInvoice` に `value_msat`・`description_hash`・`expiry` を渡す）。macaroon は invoice の発行と照会だけに絞ったものを使う
2. `/healthz` にノードへの疎通を足す。ノードに届かないときは、JIN・OSD が Lightning の選択肢を 402 から外せるよう、はっきり分かる応答にする
3. VPS へのデプロイ手順を `docs/deploy.md` に書く（systemd、永続ディスク上の SQLite、時刻同期、Cloudflare Access の前段）

JIN・OSD のリポジトリ側の作業（このリポジトリではやらない。依頼として書き出す）。

1. 402 の accepts に lnbtc の項目を 1 件足す。invoice はファシリテーターの `/invoice` から 402 のたびに取る。1 秒でタイムアウトし、失敗したら lnbtc を外して Solana だけで 402 を返す
2. 支払い付きの再送で、リクエストハッシュを実際のリクエストから再計算して `/settle` に送る。公開オリジンは設定値で固定し、転送ヘッダによる上書きを拒否する（JIN の `httpBinding` は `publicOrigin` を必須にしてある）
3. `/settle` が成功してからハンドラを実行する。ハンドラが失敗したら `/credits` に記録し、`duplicate_settlement` が返ったときは `/credits/claim` を叩く
4. 署名の作り方を §3 の HMAC に合わせる

受け入れ条件：testnet で 1 件のエンドツーエンドの決済が成立し、消費が記録に残る。同じ preimage での再送が `duplicate_settlement` になり、invoice の期限切れのあとは新しい 402 が出ることを実環境で確かめる。往復の記録を保存する。

### Phase 3：mainnet での提供

1. `LNBTC_ALLOW_MAINNET=true` に切り替え、1 コール上限が効いていることを確かめる
2. JIN の 402 に Solana と lnbtc を並べ、どちらで払っても同じデータが返ることを確かめる
3. 監視：invoice の発行数、成立数、検証失敗の理由別の件数、`duplicate_settlement` の件数、ノードへの疎通、`/settle` の応答時間
4. 複数ホストにする場合は、SQLite を共有のトランザクショナルなストアに置き換える。複数ホストで SQLite を共有してはならない

受け入れ条件：mainnet の実支払いが 1 件成立し、同じエンドポイントが Solana でも払える。

### Phase 4：自律エージェントによる実走

自律エージェントに lnbtc の経路で JIN を叩かせ、Spend Guard の shadow mode で購買判断を記録する。1 週間分の記録（成功・失敗・失敗理由・コスト）を note 記事の素材にする。記事は実走のあとに書く。

## 7. 運用上の注意

- リプレイストアの SQLite を古いバックアップで戻さない。戻すと、消費済みのキーがまだ有効な invoice の期間内に復活し、同じ証明で二重に提供できてしまう。戻すなら、戻した時点から 24 時間以上の間は Lightning の受付を止める
- ホストの時刻がずれると skew の判定が狂う。時刻同期（NTP）を必ず入れる
- 402 は invoice の発行でステートフルになる。ファシリテーターやノードが遅いと JIN の 402 がそのまま遅くなるので、JIN 側のタイムアウトとフォールバックを先に入れる
- x402 本体で PR #1873 がマージされたら、`facilitator/vendor/` を消して pypi の `x402` に切り替える。そのとき仕様ファイルも取り直して §2 と突き合わせる
- `bitstring` は 5 未満に固定してある。5.0 で `ConstBitStream` が無くなり、`bolt11` 2.2.0 の import が壊れるため

## 8. 運用者が決めること

| 項目 | 必要な判断 | 止まるもの |
|---|---|---|
| Lightning ノード | 自前の VPS に LND（第一候補）、Voltage、Lexe のどれにするか。既存の VPS があるか | Phase 2 全体 |
| ホスト | ファシリテーターを置く VPS（ノードと同居が前提）と、その前段の Cloudflare Access の設定 | Phase 2 のデプロイ |
| HMAC 鍵の配布 | 鍵の生成と、JIN・OSD の Vercel 環境変数とファシリテーター側の `LNBTC_HMAC_KEYS` への投入 | Phase 2 の往復 |
| HMAC の署名対象 | method と path を含める追加（§3）を受け入れるか | JIN・OSD 側の実装 |
| レート制限の値 | 既定 120 回 / 60 秒でよいか | Phase 3 |
| ライセンス | このリポジトリの自前部分のライセンス（いまは LICENSE ファイルが無く、NOTICE に未定と書いてある） | 公開リポジトリとしての扱い |
| 価格の見直し | 月初、または BTC/USD が基準から ±30% 乖離したときに、`docs/lnbtc-pricing.md` と `PRICES_MSAT` を同時に直す | なし（運用） |
