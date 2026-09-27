"""msat 価格・HMAC 認証・レート制限・クレジット台帳（v3 §2 / §6）。

実通信なし。Lightning ノードにも接続しない。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from facilitator.auth import (
    MAX_TIMESTAMP_SKEW_SECONDS,
    AuthError,
    RateLimited,
    RateLimiter,
    RequestAuthenticator,
    build_header,
    signed_message,
)
from facilitator.credit import CreditLedger
from facilitator.pricing import (
    MAX_SATS_PER_CALL,
    PRICES_MSAT,
    REFERENCE_BTCUSD,
    exceeds_call_cap,
    needs_repricing,
    price_msat,
    usd_to_msat,
)
from facilitator.settle import create_app

NOW = 1_700_000_000
KEY_ID = "jin-2026-09"
SECRET = "s" * 48
OTHER_ID = "jin-2026-10"
OTHER_SECRET = "n" * 48


# --------------------------------------------------------------------------- #
# 1) msat 価格（v3 §2）
# --------------------------------------------------------------------------- #
def test_fixed_prices_match_the_recorded_formula() -> None:
    """固定した msat が基準レートからの再計算と一致する。"""
    assert usd_to_msat("0.01") == PRICES_MSAT["/api/jin/series"] == 12_000
    assert usd_to_msat("0.02") == PRICES_MSAT["/api/jin/movers"] == 24_000


def test_price_is_a_positive_integer_msat_string() -> None:
    """仕様は amount をミリサトシの正の整数文字列に限る。sats や BTC で持ち回らない。"""
    for path in PRICES_MSAT:
        value = price_msat(path)
        assert isinstance(value, str) and value.isdigit() and int(value) > 0
    with pytest.raises(KeyError):
        price_msat("/api/jin/latest")  # 無料。価格を持たない。


def test_both_quoted_sources_round_to_the_same_sats() -> None:
    """Bybit と CoinDesk のどちらを採っても同じ sats になる（出典選択で価格が動かない）。"""
    for rate in ("84464.00", "84602.39"):
        assert usd_to_msat("0.01", rate) == 12_000
        assert usd_to_msat("0.02", rate) == 24_000


def test_rounding_is_up_to_whole_sats() -> None:
    # 1 sat 未満は 1 sat へ切り上げ、端数は必ず上へ。
    assert usd_to_msat("0.000000001") == 1_000
    assert usd_to_msat("0.01", "100000") == 10_000  # 10000.00 msat ちょうど -> 10 sats
    assert usd_to_msat("0.01", "99999") == 11_000  # 10000.1 msat -> 11 sats（切り上げ）


def test_prices_are_within_the_mainnet_call_cap() -> None:
    for msat in PRICES_MSAT.values():
        assert not exceeds_call_cap(msat)
    assert exceeds_call_cap((MAX_SATS_PER_CALL + 1) * 1000)


def test_repricing_threshold_is_plus_minus_30_percent() -> None:
    reference = Decimal(REFERENCE_BTCUSD)
    assert needs_repricing(reference) is False
    assert needs_repricing(reference * Decimal("1.30")) is False  # 境界は含めない
    assert needs_repricing(reference * Decimal("1.31")) is True
    assert needs_repricing(reference * Decimal("0.69")) is True


def test_invalid_pricing_inputs_are_rejected() -> None:
    for bad in ("0", "-1"):
        with pytest.raises(ValueError):
            usd_to_msat(bad)
        with pytest.raises(ValueError):
            usd_to_msat("0.01", bad)


# --------------------------------------------------------------------------- #
# 2) HMAC 認証（v3 §6）
# --------------------------------------------------------------------------- #
@pytest.fixture
def auth():
    return RequestAuthenticator({KEY_ID: SECRET}, clock=lambda: NOW)


def header(
    *, key_id=KEY_ID, secret=SECRET, timestamp=NOW, method="POST", path="/settle", body=b"{}"
):
    return {
        "x-jin-signature": build_header(
            key_id, secret, timestamp=timestamp, method=method, path=path, body=body
        )
    }


def test_valid_signature_is_accepted(auth) -> None:
    assert auth.authenticate(header(), method="POST", path="/settle", body=b"{}") == KEY_ID


def test_header_name_is_case_insensitive(auth) -> None:
    value = header()["x-jin-signature"]
    assert (
        auth.authenticate({"X-JIN-Signature": value}, method="POST", path="/settle", body=b"{}")
        == KEY_ID
    )


def test_missing_and_malformed_signatures_are_rejected(auth) -> None:
    with pytest.raises(AuthError):
        auth.authenticate({}, method="POST", path="/settle", body=b"{}")
    for bad in ("", "garbage", f"t={NOW},v1=" + "0" * 64, f"t={NOW},k={KEY_ID},v1=zz"):
        with pytest.raises(AuthError):
            auth.authenticate(
                {"x-jin-signature": bad}, method="POST", path="/settle", body=b"{}"
            )


def test_body_tampering_is_rejected(auth) -> None:
    with pytest.raises(AuthError):
        auth.authenticate(header(body=b"{}"), method="POST", path="/settle", body=b'{"a":1}')


def test_path_and_method_are_covered(auth) -> None:
    """署名対象に method と path を含めたので、別経路への投げ替えが通らない。"""
    signed = header(method="POST", path="/settle", body=b"{}")
    with pytest.raises(AuthError):
        auth.authenticate(signed, method="POST", path="/credits/claim", body=b"{}")
    with pytest.raises(AuthError):
        auth.authenticate(signed, method="GET", path="/settle", body=b"{}")


def test_signed_message_separates_fields() -> None:
    # 可変長フィールドの混同（"/a" + "b" と "/ab" が同じにならないこと）。
    assert signed_message(timestamp=1, method="POST", path="/a", body=b"b") != signed_message(
        timestamp=1, method="POST", path="/ab", body=b""
    )


def test_timestamp_window_boundary(auth) -> None:
    skew = MAX_TIMESTAMP_SKEW_SECONDS
    for offset in (-skew, 0, skew):
        assert (
            auth.authenticate(
                header(timestamp=NOW + offset), method="POST", path="/settle", body=b"{}"
            )
            == KEY_ID
        )
    for offset in (-skew - 1, skew + 1):
        with pytest.raises(AuthError):
            auth.authenticate(
                header(timestamp=NOW + offset), method="POST", path="/settle", body=b"{}"
            )


def test_key_rotation_accepts_two_keys_during_overlap() -> None:
    rotating = RequestAuthenticator(
        {KEY_ID: SECRET, OTHER_ID: OTHER_SECRET}, clock=lambda: NOW
    )
    assert rotating.authenticate(header(), method="POST", path="/settle", body=b"{}") == KEY_ID
    assert (
        rotating.authenticate(
            header(key_id=OTHER_ID, secret=OTHER_SECRET),
            method="POST",
            path="/settle",
            body=b"{}",
        )
        == OTHER_ID
    )


def test_unknown_key_id_is_rejected(auth) -> None:
    with pytest.raises(AuthError):
        auth.authenticate(
            header(key_id="unknown", secret=SECRET), method="POST", path="/settle", body=b"{}"
        )


def test_wrong_secret_for_known_key_is_rejected(auth) -> None:
    with pytest.raises(AuthError):
        auth.authenticate(
            header(secret=OTHER_SECRET), method="POST", path="/settle", body=b"{}"
        )


def test_short_secrets_and_empty_key_sets_are_refused() -> None:
    with pytest.raises(ValueError):
        RequestAuthenticator({})
    with pytest.raises(ValueError):
        RequestAuthenticator({KEY_ID: "short"})
    with pytest.raises(ValueError):
        RequestAuthenticator({"bad id!": SECRET})


# --------------------------------------------------------------------------- #
# 3) レート制限
# --------------------------------------------------------------------------- #
def test_rate_limiter_counts_per_caller() -> None:
    now = [NOW]
    limiter = RateLimiter(limit=2, window_seconds=60, clock=lambda: now[0])
    limiter.check("a")
    limiter.check("a")
    with pytest.raises(RateLimited):
        limiter.check("a")
    limiter.check("b")  # 呼び出し元ごとなので別枠


def test_rate_limiter_window_slides() -> None:
    now = [NOW]
    limiter = RateLimiter(limit=1, window_seconds=60, clock=lambda: now[0])
    limiter.check("a")
    with pytest.raises(RateLimited):
        limiter.check("a")
    now[0] = NOW + 61
    limiter.check("a")


# --------------------------------------------------------------------------- #
# 4) クレジット台帳（v3 §6）
# --------------------------------------------------------------------------- #
KEY = "lnbtc:000000000933ea01ad0ee984209779ba:" + "ab" * 32
RESOURCE = "/api/jin/series"


@pytest.fixture
def ledger(tmp_path):
    return CreditLedger(tmp_path / "credits.db", clock=lambda: NOW)


def test_record_then_claim_once(ledger) -> None:
    assert ledger.record(KEY, resource=RESOURCE, reason="upstream_unavailable", failed_at=NOW)
    assert ledger.outstanding() == [
        {
            "consumption_key": KEY,
            "resource": RESOURCE,
            "failed_at": NOW,
            "reason": "upstream_unavailable",
        }
    ]
    assert ledger.claim(KEY, resource=RESOURCE) is True
    # 引き換えは 1 回だけ。
    assert ledger.claim(KEY, resource=RESOURCE) is False
    assert ledger.outstanding() == []


def test_record_is_idempotent_per_consumption_key(ledger) -> None:
    assert ledger.record(KEY, resource=RESOURCE, reason="a", failed_at=NOW) is True
    assert ledger.record(KEY, resource=RESOURCE, reason="b", failed_at=NOW + 1) is False
    assert len(ledger.outstanding()) == 1


def test_claim_requires_matching_resource(ledger) -> None:
    ledger.record(KEY, resource=RESOURCE, reason="upstream_unavailable", failed_at=NOW)
    # 別エンドポイントへの流用は許さない。
    assert ledger.claim(KEY, resource="/api/jin/movers") is False
    assert ledger.claim(KEY, resource=RESOURCE) is True


def test_claim_without_credit_is_false(ledger) -> None:
    assert ledger.claim(KEY, resource=RESOURCE) is False


def test_concurrent_claim_grants_exactly_one(ledger) -> None:
    ledger.record(KEY, resource=RESOURCE, reason="upstream_unavailable", failed_at=NOW)
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: ledger.claim(KEY, resource=RESOURCE), range(32)))
    assert sum(results) == 1


def test_credit_survives_restart(tmp_path) -> None:
    path = tmp_path / "c.db"
    CreditLedger(path, clock=lambda: NOW).record(
        KEY, resource=RESOURCE, reason="upstream_unavailable", failed_at=NOW
    )
    assert CreditLedger(path, clock=lambda: NOW).claim(KEY, resource=RESOURCE) is True


def test_ledger_refuses_non_persistent_paths() -> None:
    for path in (":memory:", "", "file:x?mode=memory"):
        with pytest.raises(ValueError):
            CreditLedger(path)


# --------------------------------------------------------------------------- #
# 5) HTTP 境界に認証がかかっている
# --------------------------------------------------------------------------- #
@pytest.fixture
def secured(tmp_path):
    app = create_app(
        store_path=tmp_path / "s.db",
        credit_path=tmp_path / "c.db",
        hmac_keys={KEY_ID: SECRET},
        rate_limit=3,
        rate_limit_window_seconds=60,
        clock=lambda: NOW,
    )
    return TestClient(app)


def _post(client, path, body: dict):
    import json

    raw = json.dumps(body).encode()
    return client.post(
        path,
        content=raw,
        headers={
            "content-type": "application/json",
            **header(method="POST", path=path, body=raw),
        },
    )


def test_unsigned_requests_are_unauthenticated(secured) -> None:
    for path in ("/settle", "/credits", "/credits/claim"):
        result = secured.post(path, json={})
        assert result.status_code == 401
        assert result.json() == {"success": False, "errorReason": "unauthenticated"}


def test_signed_credit_round_trip(secured) -> None:
    body = {"consumptionKey": KEY, "resource": RESOURCE, "reason": "upstream_unavailable"}
    assert _post(secured, "/credits", body).json() == {"success": True, "recorded": True}
    assert _post(secured, "/credits/claim", body).json() == {"success": True, "granted": True}
    assert _post(secured, "/credits/claim", body).json() == {"success": True, "granted": False}


def test_rate_limit_returns_429(secured) -> None:
    body = {"consumptionKey": KEY, "resource": RESOURCE, "reason": "x"}
    codes = [_post(secured, "/credits", body).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3:] == [429, 429]


def test_healthz_reports_authentication_state(secured, tmp_path) -> None:
    assert secured.get("/healthz").json()["authenticated"] is True
    open_app = TestClient(create_app(store_path=tmp_path / "o.db", clock=lambda: NOW))
    assert open_app.get("/healthz").json()["authenticated"] is False
