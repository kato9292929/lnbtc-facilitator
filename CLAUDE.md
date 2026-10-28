# CLAUDE.md — lnbtc-facilitator 作業指示書

このファイルはリポジトリ直下に置く作業指示書で、Claude Code はこれをプロジェクトの前提として読む。チャット履歴は引き継がれないので、作業に必要な判断はすべてここに書く。2026-10-28 時点（Phase 1.5 完了、Phase 2 のコード側の作業を実装済み）の状態に基づく。

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
   │  402 を出す側（resource server）                               ──▶ Voltage の LND（mainnet、REST）
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
| ノードの資格情報（invoice 用 macaroon）、リプレイストア | このリポジトリが動くホストだけ | Vercel には置かない |
| Lightning ノード | Voltage（ホスティングの LND、mainnet） | ファシリテーターからインターネット越しに REST で呼ぶ。ノードとホストは同居しない |

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
| 1 コール上限 | 1 コール 1,000 sats 以下。testnet にも同じ上限をかける | `/settle` と `/invoice` で `pricing.exceeds_call_cap()` を呼ぶ。`LNBTC_MAX_SATS_PER_CALL` |
| 呼び出し元の認証 | 外側は Cloudflare Access のサービストークン、内側は HMAC | `facilitator/auth.py` |
| HMAC | ヘッダ `X-JIN-Signature: t=<unix秒>,k=<鍵ID>,v1=<16進>`。署名対象は timestamp・method・path・body。時刻ずれ 30 秒超は拒否。定数時間比較。鍵を 2 本同時に受理して入れ替える | 同上 |
| レート制限 | 鍵 ID ごと、超過は 429。既定 120 回 / 60 秒（根拠のある数字ではない） | 同上 |
| エラー応答 | `errorReason` だけ返す。`error_message` と `payer` は出さない | `facilitator/settle.py` |
| 消費後に提供できなかった分 | クレジット台帳に記録し、次の再送で 1 回だけ消費なしに提供する。`/settle` の意味は変えない（仕様が二重提出に `duplicate_settlement` を求めるため） | `facilitator/credit.py`、`POST /credits`・`POST /credits/claim` |
| mainnet の誤投入防止 | 既定は testnet だけ受け付ける。`allow_mainnet=True` のときだけ mainnet を通す | `facilitator/settle.py` |
| 発行した invoice の照合 | ノードが返した invoice を decode し、description hash・金額・expiry・通貨・署名鍵（`LNBTC_PAY_TO`）・作成時刻（skew 以内）・feature を確かめてから返す。食い違えば 502 で invoice を出さない | `facilitator/invoice.py` |
| feature フィールド | `/settle` は仕様どおり `9` の無い invoice も受理する（緩めも締めもしない）。自分のノードが出す invoice には `var_onion_optin` と `payment_secret` を要求する（LDK などの支払い側が必須とするため）。仕様の要件ではなく発行側の方針 | 同上 |

署名対象に method と path を含めたのは、運用者の指示書（v3、timestamp + body）への追加。body だけだと、`/settle` 向けの署名をそのまま `/credits/claim` へ投げ替えられるため。JIN・OSD 側の署名の作り方もこれに合わせる。

## 4. 今の状態（2026-10-28）

できていること（Phase 1・1.5、ノード無し）。

- `facilitator/vendor/lnbtc/`：x402 本体の未マージ PR #1873（`refs/pull/1873/head` = `2d85314`）の `mechanisms/lnbtc`。BOLT11 の decode と署名検証、バインディング、preimage 検証、SQLite のリプレイストア（`INSERT OR IGNORE` による消費）
- `facilitator/replay.py`：保持期限の下限と、期限切れの行だけを消す `purge()`
- `facilitator/settle.py`：`create_app()`。`/healthz`・`/settle`・`/invoice`・`/credits`・`/credits/claim`、`/verify` は 400。1 コール上限を `/settle` と `/invoice` で強制
- `facilitator/invoice.py`：発行した invoice の照合（§3）
- `facilitator/main.py`：起動の入口。環境変数（`.env.example`）を読み、HMAC 鍵が無ければ起動を止める。uvicorn は 1 プロセス
- `facilitator/lnd.py`：LND の REST の受信側アダプタ（Voltage 想定）。`POST /v1/invoices` に `value_msat`・`description_hash`（base64）・`expiry` だけを送る（`memo` は送らない）。疎通は `GET /v1/invoices?num_max_invoices=1`（invoice 用の macaroon で通るため）。設定した network 以外の依頼はノードに送らずに拒否する。https のみ。タイムアウト 2 秒
- `/healthz`：ノードに届かないときは 503・`"status": "degraded"`・`"node": "unreachable"`・`"invoice_issuance": false`。mainnet のノードを有効化前に置いたときも `invoice_issuance` は false
- `facilitator/purge.py`：掃除を 1 回だけ行うコマンド（`python -m facilitator.purge`）。ストアのファイルが無ければ作らずに止める
- `facilitator/auth.py`・`facilitator/credit.py`・`facilitator/pricing.py`
- `tests/fakes.py`：偽の受信側アダプタ（仕様のテスト専用鍵でローカル署名、LND と同じ feature を立てる）。本番の入口からは使わない
- `docs/deploy.md`：VPS（systemd、掃除の timer）、Voltage（macaroon、接続元 IP の制限、インバウンド流動性）、Cloudflare Tunnel と Access、mainnet の少額往復の手順の下書き。Voltage の項目は公開情報で埋めたもので、実ノードでは未確認
- `.github/workflows/ci.yml`：`uv sync --locked`・`uv run ruff check .`・`uv run pytest`
- テスト 165 件：LND のアダプタ（依頼の形、ノードの失敗、network の不一致、疎通、macaroon の読み込み）、アダプタ越しの mainnet の往復、`/healthz` のノード疎通、`LND_*` の設定を含む。Phase 1.5 までの内訳は Phase 1 の 57 件に加え、HTTP を通した「発行 → 支払い → `/settle` 成功 → `duplicate_settlement`」、`/invoice` の否定系（認証・署名の投げ替え・429・network・amount・上限の境界・description hash・expiry の範囲（1〜3600 秒）・型）、ノードの設定ずれ 8 通りを外に出さないこと、`/settle` の上限、feature フィールドの拒否箇所、環境変数の読み込みと起動拒否、掃除コマンド

まだ無いもの。

- 実ノードでの確認。LND のアダプタは REST の仕様に合わせて書き、偽の LND でしか確かめていない（TODO：Voltage の実ノードで発行・疎通を確かめる）
- VPS の上での実際のデプロイ
- クレジット台帳の掃除（いまは消さない。件数が増えたら考える）

## 5. 作業の進め方

- 返答・進捗・コミットメッセージは日本語。太字は使わない
- 実装していないものや確かめていないものを「動いた」と書かない。分からないことは TODO と書く
- 秘密情報（HMAC 鍵、ノードの macaroon、TLS 証明書、Cloudflare のトークン）はコミットしない。`.env` は `.gitignore` 済み。例は `.env.example` に値なしで書く
- `facilitator/vendor/` は書き換えない。上流との差分を保つため ruff の対象外にしてある。直す必要が出たら、その理由を NOTICE と `docs/` に書き、上流の PR にも伝える
- 決済の経路（`/settle`、invoice 発行、認証、消費キー、金額、network）に触る変更は、テストを足してから入れる。外向きの操作（mainnet の有効化、本番ホストへのデプロイ）は運用者の確認を取ってから行う
- 作業の前後で `uv run pytest` と `uv run ruff check .` を通す

## 6. フェーズ

### Phase 1.5：ノード無しで足せる残り（完了）

2026-10-28 に完了。中身は §4。決めたことのうち仕様に無いもの。

- `/invoice` の入力は `amount`（msat の文字列。数値は拒否）・`descriptionHash`・`expirySeconds`（1 以上 3600 以下の整数。BOLT11 の既定 expiry と同じ上限。保持期限を呼び出し元の指定で延ばさせないため）・`network`。未知のフィールドは拒否。返すのは `{"invoice": "..."}` だけ
- 照合に使う署名鍵は `LNBTC_PAY_TO`（受信ノードの公開鍵）。JIN・OSD が 402 の `payTo` に載せる値と同じもの
- エラー応答は `errorReason` だけ。入力の誤りは 400（`invalid_invoice_request`・`unsupported_network`・`invalid_exact_lnbtc_amount`・`amount_exceeds_call_cap`・`invalid_description_hash`・`invalid_expiry`）、ノードの失敗は 502 `invoice_issuance_failed`、照合の食い違いは 502 `invoice_issuance_mismatch`、アダプタ未設定は 503 `invoice_issuance_unavailable`
- `/settle` の上限超過は 400 `amount_exceeds_call_cap`（消費しない）。金額の形式の誤りは従来どおり scheme 側の理由で返す
- 環境変数の真偽値は `true` / `false` だけ受け付ける（`1` や `yes` は起動を止める）。待ち受けは `LNBTC_HOST` / `LNBTC_PORT`（既定 127.0.0.1:8000）

### Phase 2：mainnet での少額往復（運用者の準備が要る）

2026-10-28 に決定。ノードは Voltage。Voltage は testnet を止めていて、代わりの Mutinynet（signet）は仕様に無い network なので使わない（Mutinynet を受け付けるように改造しない）。testnet の往復はやめ、mainnet で 1 回 12 sats の往復をして確かめる。1 コール上限（1,000 sats）がかかっているので、誤っても損は小さい。testnet の経路は偽のアダプタのテストで守る。

このリポジトリ側の作業（実装済み。実ノードでは未確認）。

1. LND の REST の受信側アダプタ（`facilitator/lnd.py`）と、`facilitator/main.py` からの組み立て（`LND_REST_URL`・`LND_MACAROON_PATH`・`LND_TLS_CERT_PATH`・`LND_NETWORK`）
2. `/healthz` のノード疎通（§4）
3. `docs/deploy.md` の下書き

運用者の作業（手順は `docs/deploy.md`）。

1. Voltage で mainnet の LND ノードを作り、公開鍵・REST の URL を控え、Macaroon Bakery で `invoices:read`・`invoices:write` だけの macaroon を焼き、API の接続元を VPS の固定 IP に絞る
2. VPS を選んで用意する（固定 IP があること）
3. Cloudflare Tunnel と Access（サービストークン、Service Auth のポリシー）を設定する
4. チャネルを開き、自分のノードから数万 sats を外へ払ってインバウンドを作る（LSP から買うより安い）
5. HMAC 鍵を作り、VPS と JIN・OSD の Vercel に入れる

JIN・OSD のリポジトリ側の作業（このリポジトリではやらない。依頼として書き出す）。

1. 402 の accepts に lnbtc の項目を 1 件足す。invoice はファシリテーターの `/invoice` から 402 のたびに取る。1 秒でタイムアウトし、失敗したら lnbtc を外して Solana だけで 402 を返す
2. 支払い付きの再送で、リクエストハッシュを実際のリクエストから再計算して `/settle` に送る。公開オリジンは設定値で固定し、転送ヘッダによる上書きを拒否する（JIN の `httpBinding` は `publicOrigin` を必須にしてある）
3. `/settle` が成功してからハンドラを実行する。ハンドラが失敗したら `/credits` に記録し、`duplicate_settlement` が返ったときは `/credits/claim` を叩く
4. 署名の作り方を §3 の HMAC に合わせる。Cloudflare Access のサービストークン（`CF-Access-Client-Id`・`CF-Access-Client-Secret`）もヘッダに載せる

受け入れ条件：運用者の確認を取ってから `LNBTC_ALLOW_MAINNET=true` にし、mainnet で 1 件のエンドツーエンドの決済（12 sats）が成立して消費が記録に残る。同じ preimage での再送が `duplicate_settlement` になり、invoice の期限切れのあとは新しい 402 が出て、1,000 sats を超える `/invoice` が `amount_exceeds_call_cap` になることを実環境で確かめる。往復の記録を保存する（preimage は残さない）。

### Phase 3：mainnet での提供

1. JIN の 402 に Solana と lnbtc を並べ、どちらで払っても同じデータが返ることを確かめる
2. 監視：invoice の発行数、成立数、検証失敗の理由別の件数、`duplicate_settlement` の件数、ノードへの疎通、`/settle` の応答時間
3. 複数ホストにする場合は、SQLite を共有のトランザクショナルなストアに置き換える。複数ホストで SQLite を共有してはならない

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
| Lightning ノード | 決定：Voltage（mainnet）。残りはノードの作成、macaroon、接続元 IP の制限、インバウンド流動性の用意（`docs/deploy.md` §2） | Phase 2 の往復 |
| ホスト | ファシリテーターを置く VPS（これから選ぶ。条件は `docs/deploy.md` §1）と、その前段の Cloudflare Tunnel・Access の設定（運用者が行う） | Phase 2 のデプロイ |
| HMAC 鍵の配布 | 鍵の生成と、JIN・OSD の Vercel 環境変数とファシリテーター側の `LNBTC_HMAC_KEYS` への投入 | Phase 2 の往復 |
| HMAC の署名対象 | method と path を含める追加（§3）を受け入れるか | JIN・OSD 側の実装 |
| レート制限の値 | 既定 120 回 / 60 秒でよいか | Phase 3 |
| ライセンス | このリポジトリの自前部分のライセンス（いまは LICENSE ファイルが無く、NOTICE に未定と書いてある） | 公開リポジトリとしての扱い |
| 価格の見直し | 月初、または BTC/USD が基準から ±30% 乖離したときに、`docs/lnbtc-pricing.md` と `PRICES_MSAT` を同時に直す | なし（運用） |
