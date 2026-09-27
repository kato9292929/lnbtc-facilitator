"""x402 exact/lnbtc ファシリテータのテスト（Phase 1: ノード・実支払い・通信なし）。

invoice は仕様が公開しているテスト鍵でローカル署名して作る。Lightning ノードには接続しない。
期待値のうち仕様ベクタに由来するものは scheme_exact_lnbtc.md の
"Request Binding Test Vectors" から取った。
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256

import pytest
from bolt11 import Bolt11, encode
from bolt11.models.tags import Tag, TagChar, Tags
from fastapi.testclient import TestClient
from x402.schemas import PaymentPayload, PaymentRequirements

from facilitator.replay import RetainingSQLiteReplayStore
from facilitator.settle import create_app
from facilitator.vendor.lnbtc.binding import http_request_binding
from facilitator.vendor.lnbtc.constants import MAINNET, TESTNET, LightningValidationError
from facilitator.vendor.lnbtc.exact.facilitator import ExactLnbtcScheme

NOW = 1_700_000_000
# 仕様が公開しているテスト専用鍵とそれに対応する payTo。
KEY = "0" * 63 + "1"
PAYEE = "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
PREIMAGE = "42" * 32
PAYMENT_HASH = sha256(bytes.fromhex(PREIMAGE)).hexdigest()
ORIGIN = "https://api.example.com"
SPEC_HTTP_A = "0d6623f775e025501fa7f0a30b54da25aad62b6ccfe35c85da38016711e6c018"
AMOUNT_MSAT = 21_000
EXPIRY = 300


def binding(article: str = "A"):
    return http_request_binding(
        "GET", f"{ORIGIN}/article/{article}", public_origin=ORIGIN
    )


def make_invoice(
    digest: str,
    *,
    preimage: str = PREIMAGE,
    amount: int = AMOUNT_MSAT,
    expiry: int = EXPIRY,
    date: int = NOW,
    currency: str = "tb",
    inline_description: bool = False,
) -> str:
    tags = [
        Tag(TagChar.payment_hash, sha256(bytes.fromhex(preimage)).hexdigest()),
        Tag(TagChar.payment_secret, "23" * 32),
        Tag(TagChar.expire_time, expiry),
        Tag(
            TagChar.description if inline_description else TagChar.description_hash,
            "test" if inline_description else digest,
        ),
    ]
    return encode(
        Bolt11(currency=currency, amount_msat=amount, date=date, tags=Tags(tags)),
        private_key=KEY,
    )


def requirements(invoice: str, *, network: str = TESTNET, bind=None) -> PaymentRequirements:
    bind = bind or binding()
    return PaymentRequirements(
        scheme="exact",
        network=network,
        asset="BTC",
        amount=str(AMOUNT_MSAT),
        pay_to=PAYEE,
        max_timeout_seconds=EXPIRY,
        extra={
            "assetTransferMethod": "bolt11",
            "paymentFlow": "upfront",
            "invoice": invoice,
            **bind.extra(),
        },
    )


def payload(reqs: PaymentRequirements, *, preimage: str = PREIMAGE) -> PaymentPayload:
    return PaymentPayload(x402_version=2, accepted=reqs, payload={"preimage": preimage})


@pytest.fixture
def store(tmp_path):
    return RetainingSQLiteReplayStore(tmp_path / "replay.db", clock=lambda: NOW)


@pytest.fixture
def scheme(store):
    return ExactLnbtcScheme(store, clock=lambda: NOW)


# --------------------------------------------------------------------------- #
# 1) バインディングが仕様ベクタと一致する（TS 実装との突き合わせ）
# --------------------------------------------------------------------------- #
def test_binding_matches_spec_vector() -> None:
    assert binding("A").digest == SPEC_HTTP_A
    assert binding("B").digest != SPEC_HTTP_A


def test_binding_rejects_untrusted_origin() -> None:
    # 公開オリジンと URL のオリジンが食い違うリクエストは通さない（転送ヘッダ上書き対策）。
    with pytest.raises(LightningValidationError):
        http_request_binding("GET", "https://evil.example.com/article/A", public_origin=ORIGIN)


# --------------------------------------------------------------------------- #
# 2) 正常系と否定系（settle）
# --------------------------------------------------------------------------- #
def test_settle_succeeds_and_returns_payment_hash(scheme) -> None:
    reqs = requirements(make_invoice(SPEC_HTTP_A))
    result = scheme.settle(payload(reqs), reqs)
    assert result.success is True
    assert result.transaction == PAYMENT_HASH
    assert result.network == TESTNET
    # payer は仕様上省略必須。
    assert result.payer in (None, "")


def test_duplicate_settlement_is_rejected(scheme) -> None:
    reqs = requirements(make_invoice(SPEC_HTTP_A))
    assert scheme.settle(payload(reqs), reqs).success is True
    again = scheme.settle(payload(reqs), reqs)
    assert again.success is False
    assert again.error_reason == "duplicate_settlement"


def test_preimage_mismatch_is_rejected(scheme) -> None:
    reqs = requirements(make_invoice(SPEC_HTTP_A))
    result = scheme.settle(payload(reqs, preimage="00" * 32), reqs)
    assert result.success is False
    assert result.error_reason == "invalid_exact_lnbtc_preimage_hash_mismatch"


def test_proof_moved_to_another_request_is_rejected(scheme) -> None:
    """article A の invoice を、実行されるリクエストが B の requirements に付け替える。"""
    invoice = make_invoice(SPEC_HTTP_A)
    reqs_b = requirements(invoice, bind=binding("B"))
    result = scheme.settle(payload(reqs_b), reqs_b)
    assert result.success is False
    assert result.error_reason == "invalid_exact_lnbtc_invoice_request_mismatch"


def test_inline_description_is_rejected(scheme) -> None:
    reqs = requirements(make_invoice(SPEC_HTTP_A, inline_description=True))
    result = scheme.settle(payload(reqs), reqs)
    assert result.success is False
    assert result.error_reason == "invalid_exact_lnbtc_invoice_description"


def test_currency_must_match_network(scheme) -> None:
    # testnet の requirements に mainnet 通貨（bc）の invoice を載せる。
    reqs = requirements(make_invoice(SPEC_HTTP_A, currency="bc"))
    result = scheme.settle(payload(reqs), reqs)
    assert result.success is False
    assert result.error_reason == "invalid_exact_lnbtc_invoice_currency_mismatch"


def test_verify_is_never_valid_for_upfront(scheme) -> None:
    reqs = requirements(make_invoice(SPEC_HTTP_A))
    result = scheme.verify(payload(reqs), reqs)
    assert result.is_valid is False
    assert result.invalid_reason == "invalid_exact_lnbtc_payment_flow"


# --------------------------------------------------------------------------- #
# 3) 境界時刻（skew を 2 か所で使う単一設定）
# --------------------------------------------------------------------------- #
def test_expiry_grace_boundary_is_inclusive(store) -> None:
    """settlement_time == invoice_end + skew は有効、+1 秒で無効。"""
    end = NOW + EXPIRY
    at_boundary = ExactLnbtcScheme(store, clock=lambda: end + 60)
    reqs = requirements(make_invoice(SPEC_HTTP_A))
    assert at_boundary.settle(payload(reqs), reqs).success is True

    past = ExactLnbtcScheme(
        RetainingSQLiteReplayStore(store.path.parent / "b.db", clock=lambda: NOW),
        clock=lambda: end + 61,
    )
    result = past.settle(payload(reqs), reqs)
    assert result.success is False
    assert result.error_reason == "invalid_exact_lnbtc_invoice_expired"


def test_invoice_created_in_future_is_rejected(store) -> None:
    """作成時刻 == settlement + skew は有効、+1 秒で無効。同じ skew 値を使う。"""
    reqs = requirements(make_invoice(SPEC_HTTP_A, date=NOW + 60))
    assert ExactLnbtcScheme(store, clock=lambda: NOW).settle(payload(reqs), reqs).success is True

    ahead = requirements(make_invoice(SPEC_HTTP_A, date=NOW + 61))
    other = ExactLnbtcScheme(
        RetainingSQLiteReplayStore(store.path.parent / "c.db", clock=lambda: NOW),
        clock=lambda: NOW,
    )
    result = other.settle(payload(ahead), ahead)
    assert result.success is False
    assert result.error_reason == "invalid_exact_lnbtc_invoice_created_in_future"


# --------------------------------------------------------------------------- #
# 4) 消費キーのネットワーク分離
# --------------------------------------------------------------------------- #
def test_consumption_key_separates_networks(store) -> None:
    key_t = f"{TESTNET}:{PAYMENT_HASH}"
    key_m = f"{MAINNET}:{PAYMENT_HASH}"
    assert store.consume(key_t, NOW + EXPIRY) is True
    # 同じ payment_hash でもネットワークが違えば別キーなので消費できる。
    assert store.consume(key_m, NOW + EXPIRY) is True
    assert store.consume(key_t, NOW + EXPIRY) is False


# --------------------------------------------------------------------------- #
# 5) 同時実行で二重消費が起きない
# --------------------------------------------------------------------------- #
def test_concurrent_consume_admits_exactly_one(store) -> None:
    key = f"{TESTNET}:{PAYMENT_HASH}"
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: store.consume(key, NOW + EXPIRY), range(64)))
    assert sum(results) == 1
    assert store.count() == 1


def test_concurrent_settle_admits_exactly_one(store) -> None:
    reqs = requirements(make_invoice(SPEC_HTTP_A))
    scheme = ExactLnbtcScheme(store, clock=lambda: NOW)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: scheme.settle(payload(reqs), reqs), range(16)))
    assert sum(1 for r in results if r.success) == 1
    assert {r.error_reason for r in results if not r.success} == {"duplicate_settlement"}


# --------------------------------------------------------------------------- #
# 6) 再起動耐性
# --------------------------------------------------------------------------- #
def test_consumed_state_survives_restart(tmp_path) -> None:
    path = tmp_path / "replay.db"
    key = f"{TESTNET}:{PAYMENT_HASH}"
    assert RetainingSQLiteReplayStore(path, clock=lambda: NOW).consume(key, NOW + EXPIRY) is True
    # 新しいプロセス相当の別インスタンス。同じファイルを開き直す。
    assert RetainingSQLiteReplayStore(path, clock=lambda: NOW).consume(key, NOW + EXPIRY) is False


def test_store_refuses_non_persistent_paths(tmp_path) -> None:
    for path in (":memory:", "", "file:x?mode=memory"):
        with pytest.raises(ValueError):
            RetainingSQLiteReplayStore(path)


# --------------------------------------------------------------------------- #
# 7) 保持期限と掃除
# --------------------------------------------------------------------------- #
def test_retention_floor_is_applied(store) -> None:
    key = f"{TESTNET}:{PAYMENT_HASH}"
    store.consume(key, NOW + EXPIRY + 60 + 3600)
    # 仕様下限（invoice_end + skew + 1h）より運用固定の 24h が長いので 24h を採る。
    assert store.retain_until(key) == NOW + 24 * 3600


def test_retention_floor_never_undercuts_spec_lower_bound(tmp_path) -> None:
    """maxTimeoutSeconds が長い場合は仕様下限のほうが 24h より後になる。そちらを採る。"""
    store = RetainingSQLiteReplayStore(tmp_path / "long.db", clock=lambda: NOW)
    spec_lower_bound = NOW + 7 * 24 * 3600 + 60 + 3600
    key = f"{TESTNET}:{PAYMENT_HASH}"
    store.consume(key, spec_lower_bound)
    assert store.retain_until(key) == spec_lower_bound


def test_purge_keeps_entries_while_invoice_could_still_validate(tmp_path) -> None:
    store = RetainingSQLiteReplayStore(
        tmp_path / "p.db", minimum_retention_seconds=0, clock=lambda: NOW
    )
    key = f"{TESTNET}:{PAYMENT_HASH}"
    invoice_end = NOW + EXPIRY
    retain_until = invoice_end + 60 + 3600
    store.consume(key, retain_until)

    # invoice がまだ検証を通る間（invoice_end + skew まで）は絶対に消えない。
    for moment in (NOW, invoice_end, invoice_end + 60, retain_until - 1):
        assert store.purge(moment) == 0
        assert store.count() == 1

    assert store.purge(retain_until) == 1
    assert store.count() == 0


def test_purged_key_is_only_reusable_after_retention(tmp_path) -> None:
    store = RetainingSQLiteReplayStore(
        tmp_path / "q.db", minimum_retention_seconds=0, clock=lambda: NOW
    )
    key = f"{TESTNET}:{PAYMENT_HASH}"
    retain_until = NOW + EXPIRY + 60 + 3600
    store.consume(key, retain_until)
    store.purge(retain_until - 1)
    assert store.consume(key, retain_until) is False  # まだ残っている
    store.purge(retain_until)
    assert store.consume(key, retain_until) is True  # 掃除後は空き


# --------------------------------------------------------------------------- #
# 8) /settle の HTTP 境界
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(tmp_path):
    app = create_app(store_path=tmp_path / "http.db", clock=lambda: NOW)
    return TestClient(app)


def _body(reqs: PaymentRequirements) -> dict:
    return {
        "paymentPayload": payload(reqs).model_dump(by_alias=True),
        "paymentRequirements": reqs.model_dump(by_alias=True),
    }


def test_http_settle_success_and_duplicate(client) -> None:
    reqs = requirements(make_invoice(SPEC_HTTP_A))
    first = client.post("/settle", json=_body(reqs))
    assert first.status_code == 200
    assert first.json() == {
        "success": True,
        "transaction": PAYMENT_HASH,
        "network": TESTNET,
    }
    # payer は応答に出さない。
    assert "payer" not in first.json()

    second = client.post("/settle", json=_body(reqs))
    assert second.status_code == 402
    assert second.json()["errorReason"] == "duplicate_settlement"


def test_http_verify_is_not_implemented(client) -> None:
    result = client.post("/verify", json={})
    assert result.status_code == 400
    assert result.json()["invalidReason"] == "invalid_exact_lnbtc_payment_flow"


def test_http_mainnet_is_refused_until_armed(client) -> None:
    reqs = requirements(make_invoice(SPEC_HTTP_A, currency="bc"), network=MAINNET)
    result = client.post("/settle", json=_body(reqs))
    assert result.status_code == 400
    assert result.json()["errorReason"] == "unsupported_network"


def test_http_mainnet_allowed_when_armed(tmp_path) -> None:
    app = create_app(store_path=tmp_path / "m.db", allow_mainnet=True, clock=lambda: NOW)
    reqs = requirements(make_invoice(SPEC_HTTP_A, currency="bc"), network=MAINNET)
    result = TestClient(app).post("/settle", json=_body(reqs))
    assert result.status_code == 200
    assert result.json()["network"] == MAINNET


def test_healthz_reports_configured_networks(client) -> None:
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["networks"] == [TESTNET]
    assert body["clock_skew_seconds"] == 60


def test_store_schema_uses_primary_key(tmp_path) -> None:
    """二重消費を防いでいるのは UNIQUE 制約であることを固定する。"""
    store = RetainingSQLiteReplayStore(tmp_path / "s.db", clock=lambda: NOW)
    with sqlite3.connect(store.path) as connection:
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'x402_lightning_consumed'"
        ).fetchone()[0]
    assert "key TEXT PRIMARY KEY" in sql
