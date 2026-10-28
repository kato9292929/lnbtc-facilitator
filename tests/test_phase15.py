"""Phase 1.5：invoice 発行の口・1 コール上限・起動の設定・掃除・feature の非互換。

Lightning ノードには接続しない。invoice は偽の受信側アダプタ（tests/fakes.py）が
仕様のテスト専用鍵でローカル署名して作る。
"""

from __future__ import annotations

import json
from hashlib import sha256

import pytest
from bolt11 import Bolt11, encode
from bolt11.models.tags import Tag, TagChar, Tags
from fakes import LND_LIKE_FEATURES, TEST_KEY, TEST_PAYEE, FakeReceiver
from fastapi.testclient import TestClient
from x402.schemas import PaymentPayload, PaymentRequirements

import facilitator.main as entry
from facilitator.auth import build_header
from facilitator.invoice import IssuanceMismatch, check_issued_invoice
from facilitator.main import ConfigError, build_app, load_settings, parse_hmac_keys
from facilitator.purge import run as purge_run
from facilitator.replay import RetainingSQLiteReplayStore
from facilitator.settle import create_app
from facilitator.vendor.lnbtc.binding import http_request_binding
from facilitator.vendor.lnbtc.constants import MAINNET, TESTNET, LightningValidationError
from facilitator.vendor.lnbtc.validation import decode_invoice

NOW = 1_700_000_000
KEY_ID = "jin-2026-10"
SECRET = "s" * 48
ORIGIN = "https://jin.example.com"
EXPIRY = 300
AMOUNT = "12000"
CAP_MSAT = 1000 * 1000


def bind(path: str = "/api/jin/series"):
    return http_request_binding("GET", f"{ORIGIN}{path}", public_origin=ORIGIN)


def signed(client: TestClient, path: str, body: dict, *, sign_path: str | None = None):
    raw = json.dumps(body).encode()
    value = build_header(
        KEY_ID, SECRET, timestamp=NOW, method="POST", path=sign_path or path, body=raw
    )
    return client.post(
        path,
        content=raw,
        headers={"content-type": "application/json", "x-jin-signature": value},
    )


def invoice_body(**overrides) -> dict:
    body = {
        "amount": AMOUNT,
        "descriptionHash": bind().digest,
        "expirySeconds": EXPIRY,
        "network": TESTNET,
    }
    body.update(overrides)
    return body


def make_app(tmp_path, receiver=None, **kwargs) -> TestClient:
    kwargs.setdefault("hmac_keys", {KEY_ID: SECRET})
    app = create_app(
        store_path=tmp_path / "replay.db",
        credit_path=tmp_path / "credits.db",
        receiver=receiver,
        pay_to=TEST_PAYEE if receiver is not None else None,
        clock=lambda: NOW,
        **kwargs,
    )
    return TestClient(app)


def requirements_for(invoice: str, *, amount: str = AMOUNT, network: str = TESTNET):
    return PaymentRequirements(
        scheme="exact",
        network=network,
        asset="BTC",
        amount=amount,
        pay_to=TEST_PAYEE,
        max_timeout_seconds=EXPIRY,
        extra={
            "assetTransferMethod": "bolt11",
            "paymentFlow": "upfront",
            "invoice": invoice,
            **bind().extra(),
        },
    )


def settle_body(reqs: PaymentRequirements, preimage: str) -> dict:
    payload = PaymentPayload(x402_version=2, accepted=reqs, payload={"preimage": preimage})
    return {
        "paymentPayload": payload.model_dump(by_alias=True),
        "paymentRequirements": reqs.model_dump(by_alias=True),
    }


@pytest.fixture
def receiver():
    return FakeReceiver(clock=lambda: NOW)


@pytest.fixture
def client(tmp_path, receiver):
    return make_app(tmp_path, receiver)


# --------------------------------------------------------------------------- #
# 1) 受け入れ条件：発行 → 支払い → /settle 成功 → 同じ証明で duplicate_settlement
# --------------------------------------------------------------------------- #
def test_round_trip_through_http(client, receiver) -> None:
    issued = signed(client, "/invoice", invoice_body())
    assert issued.status_code == 200
    assert set(issued.json()) == {"invoice"}
    invoice = issued.json()["invoice"]

    preimage = receiver.pay(invoice)
    reqs = requirements_for(invoice)
    first = signed(client, "/settle", settle_body(reqs, preimage))
    assert first.status_code == 200
    assert first.json() == {
        "success": True,
        "transaction": sha256(bytes.fromhex(preimage)).hexdigest(),
        "network": TESTNET,
    }

    again = signed(client, "/settle", settle_body(reqs, preimage))
    assert again.status_code == 402
    assert again.json()["errorReason"] == "duplicate_settlement"


def test_each_issuance_is_a_new_invoice(client) -> None:
    a = signed(client, "/invoice", invoice_body()).json()["invoice"]
    b = signed(client, "/invoice", invoice_body()).json()["invoice"]
    assert a != b
    assert decode_invoice(a).payment_hash != decode_invoice(b).payment_hash


def test_issued_invoice_carries_the_requested_terms(client) -> None:
    invoice = signed(client, "/invoice", invoice_body()).json()["invoice"]
    decoded = decode_invoice(invoice)
    assert decoded.description_hash == bind().digest
    assert decoded.amount_msat == int(AMOUNT)
    assert decoded.expiry == EXPIRY
    assert decoded.currency == "tb"
    assert decoded.payee == TEST_PAYEE


# --------------------------------------------------------------------------- #
# 2) /invoice の否定系。拒否したときはノードを呼ばない
# --------------------------------------------------------------------------- #
def test_invoice_requires_authentication(client, receiver) -> None:
    result = client.post("/invoice", json=invoice_body())
    assert result.status_code == 401
    assert result.json() == {"success": False, "errorReason": "unauthenticated"}
    assert receiver.calls == []


def test_settle_signature_cannot_be_replayed_to_invoice(client, receiver) -> None:
    result = signed(client, "/invoice", invoice_body(), sign_path="/settle")
    assert result.status_code == 401
    assert receiver.calls == []


def test_invoice_rate_limit_returns_429(tmp_path, receiver) -> None:
    client = make_app(tmp_path, receiver, rate_limit=2)
    codes = [signed(client, "/invoice", invoice_body()).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_invoice_refuses_mainnet_until_armed(client, receiver) -> None:
    result = signed(client, "/invoice", invoice_body(network=MAINNET))
    assert result.status_code == 400
    assert result.json()["errorReason"] == "unsupported_network"
    assert receiver.calls == []


def test_invoice_issues_mainnet_when_armed(tmp_path, receiver) -> None:
    client = make_app(tmp_path, receiver, allow_mainnet=True)
    result = signed(client, "/invoice", invoice_body(network=MAINNET))
    assert result.status_code == 200
    assert decode_invoice(result.json()["invoice"]).currency == "bc"


def test_invoice_refuses_unknown_network(client, receiver) -> None:
    result = signed(client, "/invoice", invoice_body(network="lnbtc:regtest"))
    assert result.json()["errorReason"] == "unsupported_network"
    assert receiver.calls == []


@pytest.mark.parametrize("amount", ["0", "-1", "12.5", "1e3", "abc", "", " 12000", "１２"])
def test_invoice_refuses_non_positive_integer_amounts(client, receiver, amount) -> None:
    result = signed(client, "/invoice", invoice_body(amount=amount))
    assert result.status_code == 400
    assert result.json()["errorReason"] == "invalid_exact_lnbtc_amount"
    assert receiver.calls == []


def test_invoice_refuses_numeric_amount(client, receiver) -> None:
    # msat は文字列で持ち回る。数値で来たら型の取り違えとして拒否する。
    result = signed(client, "/invoice", invoice_body(amount=12000))
    assert result.status_code == 400
    assert result.json()["errorReason"] == "invalid_invoice_request"
    assert receiver.calls == []


def test_invoice_call_cap_boundary(client, receiver) -> None:
    assert signed(client, "/invoice", invoice_body(amount=str(CAP_MSAT))).status_code == 200
    over = signed(client, "/invoice", invoice_body(amount=str(CAP_MSAT + 1)))
    assert over.status_code == 400
    assert over.json()["errorReason"] == "amount_exceeds_call_cap"
    assert len(receiver.calls) == 1


def test_invoice_call_cap_is_configurable(tmp_path, receiver) -> None:
    client = make_app(tmp_path, receiver, max_sats_per_call=10)
    assert signed(client, "/invoice", invoice_body(amount="10000")).status_code == 200
    result = signed(client, "/invoice", invoice_body(amount="10001"))
    assert result.json()["errorReason"] == "amount_exceeds_call_cap"


@pytest.mark.parametrize(
    "digest", ["AB" * 32, "ab" * 31, "ab" * 33, "zz" * 32, ""]
)
def test_invoice_refuses_malformed_description_hash(client, receiver, digest) -> None:
    result = signed(client, "/invoice", invoice_body(descriptionHash=digest))
    assert result.status_code == 400
    assert result.json()["errorReason"] == "invalid_description_hash"
    assert receiver.calls == []


@pytest.mark.parametrize("expiry", [0, -1])
def test_invoice_refuses_non_positive_expiry(client, receiver, expiry) -> None:
    result = signed(client, "/invoice", invoice_body(expirySeconds=expiry))
    assert result.json()["errorReason"] == "invalid_expiry"
    assert receiver.calls == []


@pytest.mark.parametrize(
    "body",
    [
        invoice_body(expirySeconds="300"),
        invoice_body(expirySeconds=300.0),
        invoice_body(extra="x"),
        {k: v for k, v in invoice_body().items() if k != "network"},
    ],
)
def test_invoice_refuses_malformed_requests(client, receiver, body) -> None:
    result = signed(client, "/invoice", body)
    assert result.status_code == 400
    assert result.json()["errorReason"] == "invalid_invoice_request"
    assert receiver.calls == []


def test_invoice_without_receiver_is_unavailable(tmp_path) -> None:
    client = make_app(tmp_path)
    result = signed(client, "/invoice", invoice_body())
    assert result.status_code == 503
    assert result.json() == {"success": False, "errorReason": "invoice_issuance_unavailable"}


def test_receiver_failure_does_not_leak_details(tmp_path) -> None:
    client = make_app(tmp_path, FakeReceiver(clock=lambda: NOW, fail=True))
    result = signed(client, "/invoice", invoice_body())
    assert result.status_code == 502
    assert result.json() == {"success": False, "errorReason": "invoice_issuance_failed"}
    assert "secret" not in result.text


def test_receiver_requires_pay_to() -> None:
    with pytest.raises(ValueError):
        create_app(store_path="unused.db", receiver=FakeReceiver(clock=lambda: NOW))


# --------------------------------------------------------------------------- #
# 3) ノードの設定ずれを外に出さない（発行し直した invoice の decode と照合）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "receiver_kwargs",
    [
        {"override": {"amount_msat": 12001}},
        {"override": {"description_hash": "00" * 32}},
        {"override": {"expiry_seconds": EXPIRY + 1}},
        {"override": {"currency": "bc"}},
        {"override": {"date": NOW - 61}},
        {"override": {"date": NOW + 61}},
        {"key": "0" * 63 + "2"},
        {"features": None},
    ],
    ids=["amount", "hash", "expiry", "currency", "date-past", "date-future", "key", "features"],
)
def test_mismatched_issuance_is_not_returned(tmp_path, receiver_kwargs) -> None:
    client = make_app(tmp_path, FakeReceiver(clock=lambda: NOW, **receiver_kwargs))
    result = signed(client, "/invoice", invoice_body())
    assert result.status_code == 502
    assert result.json() == {"success": False, "errorReason": "invoice_issuance_mismatch"}
    assert "invoice" not in result.json()


def test_issuance_date_within_skew_is_accepted(tmp_path) -> None:
    for date in (NOW - 60, NOW + 60):
        client = make_app(tmp_path, FakeReceiver(clock=lambda: NOW, override={"date": date}))
        assert signed(client, "/invoice", invoice_body()).status_code == 200


# --------------------------------------------------------------------------- #
# 4) /settle の 1 コール上限
# --------------------------------------------------------------------------- #
def _invoice(amount_msat: int, *, preimage: str = "42" * 32) -> str:
    tags = [
        Tag(TagChar.payment_hash, sha256(bytes.fromhex(preimage)).hexdigest()),
        Tag(TagChar.payment_secret, "23" * 32),
        Tag(TagChar.expire_time, EXPIRY),
        Tag(TagChar.description_hash, bind().digest),
    ]
    return encode(
        Bolt11(currency="tb", amount_msat=amount_msat, date=NOW, tags=Tags(tags)),
        private_key=TEST_KEY,
    )


def test_settle_refuses_amount_over_call_cap(tmp_path) -> None:
    client = make_app(tmp_path)
    reqs = requirements_for(_invoice(CAP_MSAT + 1), amount=str(CAP_MSAT + 1))
    result = signed(client, "/settle", settle_body(reqs, "42" * 32))
    assert result.status_code == 400
    assert result.json()["errorReason"] == "amount_exceeds_call_cap"
    # 拒否した証明は消費していないので、上限を上げれば通る。
    raised = make_app(tmp_path, max_sats_per_call=CAP_MSAT)
    assert signed(raised, "/settle", settle_body(reqs, "42" * 32)).status_code == 200


def test_settle_accepts_amount_at_call_cap(tmp_path) -> None:
    client = make_app(tmp_path)
    reqs = requirements_for(_invoice(CAP_MSAT), amount=str(CAP_MSAT))
    assert signed(client, "/settle", settle_body(reqs, "42" * 32)).status_code == 200


def test_settle_malformed_amount_keeps_scheme_reason(tmp_path) -> None:
    client = make_app(tmp_path)
    reqs = requirements_for(_invoice(12000), amount="12.0")
    result = signed(client, "/settle", settle_body(reqs, "42" * 32))
    assert result.json()["errorReason"] == "invalid_exact_lnbtc_amount"


def test_healthz_reports_cap_and_issuance(client, tmp_path) -> None:
    body = client.get("/healthz").json()
    assert body["max_sats_per_call"] == 1000
    assert body["invoice_issuance"] is True
    (tmp_path / "other").mkdir()
    other = make_app(tmp_path / "other")
    assert other.get("/healthz").json()["invoice_issuance"] is False


# --------------------------------------------------------------------------- #
# 5) BOLT11 の feature フィールドの非互換：どこで拒否されるかを固定する
# --------------------------------------------------------------------------- #
def test_spec_style_invoice_without_features_passes_settle(tmp_path) -> None:
    """仕様の初期フィクスチャと同じく `9` を省いた invoice。

    /settle（vendoring した検証）は仕様どおり受理する。ここでは拒否しない。
    LDK などの支払い側は invalid features として拒否するが、それはこのリポジトリの外。
    """
    invoice = _invoice(12000)
    assert decode_invoice(invoice).features is None
    client = make_app(tmp_path)
    result = signed(client, "/settle", settle_body(requirements_for(invoice), "42" * 32))
    assert result.status_code == 200


def test_spec_style_invoice_without_features_is_refused_at_issuance() -> None:
    """自分のノードが `9` の無い invoice を出したら、/invoice の照合で止める。"""
    with pytest.raises(IssuanceMismatch, match="features:var_onion_optin"):
        check_issued_invoice(
            _invoice(12000),
            amount_msat=12000,
            description_hash=bind().digest,
            expiry_seconds=EXPIRY,
            network=TESTNET,
            pay_to=TEST_PAYEE,
            now=NOW,
            clock_skew=60,
        )


def test_lnd_like_features_pass_issuance_and_settle(client, receiver) -> None:
    invoice = signed(client, "/invoice", invoice_body()).json()["invoice"]
    assert decode_invoice(invoice).features.readable == {
        "var_onion_optin": "required",
        "payment_secret": "required",
    }
    body = settle_body(requirements_for(invoice), receiver.pay(invoice))
    result = signed(client, "/settle", body)
    assert result.status_code == 200


def test_duplicate_feature_fields_are_rejected_by_decode() -> None:
    tags = [
        Tag(TagChar.payment_hash, sha256(b"x").hexdigest()),
        Tag(TagChar.payment_secret, "23" * 32),
        Tag(TagChar.expire_time, EXPIRY),
        Tag(TagChar.description_hash, bind().digest),
        Tag(TagChar.features, LND_LIKE_FEATURES),
        Tag(TagChar.features, LND_LIKE_FEATURES),
    ]
    invoice = encode(
        Bolt11(currency="tb", amount_msat=12000, date=NOW, tags=Tags(tags)),
        private_key=TEST_KEY,
    )
    with pytest.raises(LightningValidationError, match="invoice_decode_failed"):
        decode_invoice(invoice)


# --------------------------------------------------------------------------- #
# 6) 起動の設定（環境変数）
# --------------------------------------------------------------------------- #
def env(tmp_path, **overrides) -> dict[str, str]:
    base = {
        "LNBTC_STORE_PATH": str(tmp_path / "replay.db"),
        "LNBTC_CREDIT_PATH": str(tmp_path / "credits.db"),
        "LNBTC_HMAC_KEYS": f"{KEY_ID}:{SECRET}",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def test_settings_defaults(tmp_path) -> None:
    settings = load_settings(env(tmp_path))
    assert settings.allow_mainnet is False
    assert settings.clock_skew == 60
    assert settings.max_sats_per_call == 1000
    assert settings.rate_limit == 120
    assert settings.rate_limit_window_seconds == 60
    assert settings.pay_to is None
    assert settings.host == "127.0.0.1"
    # 秘密は repr に出さない。
    assert SECRET not in repr(settings)


def test_startup_is_refused_without_hmac_keys(tmp_path) -> None:
    for value in (None, "", " , "):
        with pytest.raises(ConfigError):
            load_settings(env(tmp_path, LNBTC_HMAC_KEYS=value))


@pytest.mark.parametrize("name", ["LNBTC_STORE_PATH", "LNBTC_CREDIT_PATH"])
def test_startup_is_refused_without_paths(tmp_path, name) -> None:
    with pytest.raises(ConfigError, match=name):
        load_settings(env(tmp_path, **{name: None}))


def test_store_and_credit_paths_must_differ(tmp_path) -> None:
    same = str(tmp_path / "one.db")
    with pytest.raises(ConfigError):
        load_settings(env(tmp_path, LNBTC_STORE_PATH=same, LNBTC_CREDIT_PATH=same))


def test_hmac_keys_parse_two_keys_and_colons() -> None:
    assert parse_hmac_keys(f"a:{SECRET}, b:x:{SECRET}") == {"a": SECRET, "b": f"x:{SECRET}"}
    for raw in ("a", ":secret", "a:", f"a:{SECRET},a:{SECRET}"):
        with pytest.raises(ConfigError):
            parse_hmac_keys(raw)


def test_short_hmac_secret_stops_startup(tmp_path) -> None:
    with pytest.raises(ConfigError):
        build_app(env(tmp_path, LNBTC_HMAC_KEYS="a:short"))


@pytest.mark.parametrize("value", ["1", "yes", "TRUEISH"])
def test_allow_mainnet_must_be_true_or_false(tmp_path, value) -> None:
    with pytest.raises(ConfigError):
        load_settings(env(tmp_path, LNBTC_ALLOW_MAINNET=value))


def test_allow_mainnet_true_arms_mainnet(tmp_path) -> None:
    app, settings = build_app(env(tmp_path, LNBTC_ALLOW_MAINNET="true"))
    assert settings.allow_mainnet is True
    assert MAINNET in TestClient(app).get("/healthz").json()["networks"]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LNBTC_SKEW_SECONDS", "-1"),
        ("LNBTC_SKEW_SECONDS", "6.5"),
        ("LNBTC_MAX_SATS_PER_CALL", "0"),
        ("LNBTC_RATE_LIMIT", "0"),
        ("LNBTC_RATE_LIMIT_WINDOW_SECONDS", "x"),
        ("LNBTC_PAY_TO", "02" + "AB" * 32),
        ("LNBTC_PAY_TO", "04" + "ab" * 32),
    ],
)
def test_invalid_numbers_and_keys_stop_startup(tmp_path, name, value) -> None:
    with pytest.raises(ConfigError, match=name):
        load_settings(env(tmp_path, **{name: value}))


def test_settings_reach_the_app(tmp_path) -> None:
    app, _ = build_app(
        env(
            tmp_path,
            LNBTC_SKEW_SECONDS="30",
            LNBTC_MAX_SATS_PER_CALL="24",
            LNBTC_PAY_TO=TEST_PAYEE,
        )
    )
    body = TestClient(app).get("/healthz").json()
    assert body["clock_skew_seconds"] == 30
    assert body["max_sats_per_call"] == 24
    assert body["authenticated"] is True
    assert body["invoice_issuance"] is False  # 受信アダプタは Phase 2


def test_main_exits_without_keys(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(entry.os, "environ", env(tmp_path, LNBTC_HMAC_KEYS=None))
    with pytest.raises(SystemExit) as raised:
        entry.main()
    assert "LNBTC_HMAC_KEYS" in str(raised.value)


# --------------------------------------------------------------------------- #
# 7) 掃除コマンド
# --------------------------------------------------------------------------- #
def test_purge_command_deletes_only_expired_rows(tmp_path) -> None:
    path = tmp_path / "replay.db"
    store = RetainingSQLiteReplayStore(path, minimum_retention_seconds=0, clock=lambda: NOW)
    store.consume("old", NOW - 1)
    store.consume("live", 2**40)
    assert purge_run({"LNBTC_STORE_PATH": str(path)}) == 1
    assert store.retain_until("old") is None
    assert store.retain_until("live") == 2**40


def test_purge_command_refuses_missing_store(tmp_path) -> None:
    path = tmp_path / "missing.db"
    with pytest.raises(ConfigError):
        purge_run({"LNBTC_STORE_PATH": str(path)})
    assert not path.exists()
    with pytest.raises(ConfigError):
        purge_run({})
