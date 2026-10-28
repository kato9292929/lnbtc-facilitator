"""LND の REST アダプタ（facilitator/lnd.py）と、/healthz のノード疎通・起動の設定。

実ノードには接続しない。httpx の MockTransport で LND の REST を真似る。偽の LND は
受け取った依頼の中身から、仕様のテスト専用鍵で invoice を署名して返す。
"""

from __future__ import annotations

import base64
import json
from hashlib import sha256

import httpx
import pytest
from fakes import TEST_PAYEE, FakeReceiver
from fastapi.testclient import TestClient
from x402.schemas import PaymentPayload, PaymentRequirements

from facilitator.auth import build_header
from facilitator.lnd import LndRestReceiver, ReceiverError, load_macaroon_hex
from facilitator.main import ConfigError, build_app, build_receiver, load_settings
from facilitator.settle import create_app
from facilitator.vendor.lnbtc.binding import http_request_binding
from facilitator.vendor.lnbtc.constants import MAINNET, TESTNET

NOW = 1_700_000_000
KEY_ID = "jin-2026-10"
SECRET = "s" * 48
MACAROON = "0201036c6e6402" + "ab" * 40
URL = "https://node.example.voltageapp.io:8080"
ORIGIN = "https://jin.example.com"
DIGEST = http_request_binding("GET", f"{ORIGIN}/api/jin/series", public_origin=ORIGIN).digest


class FakeLnd:
    """LND の REST の真似。`POST /v1/invoices` と `GET /v1/invoices` だけ答える。"""

    def __init__(self, *, currency_network: str = TESTNET, status: int = 200, body=None):
        self.signer = FakeReceiver(clock=lambda: NOW)
        self.network = currency_network
        self.status = status
        self.body = body
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "macaroon secret detail"})
        if request.method == "GET" and request.url.path == "/v1/invoices":
            return httpx.Response(200, json={"invoices": []})
        if request.method == "POST" and request.url.path == "/v1/invoices":
            if self.body is not None:
                return httpx.Response(200, json=self.body)
            sent = json.loads(request.content)
            invoice = self.signer.create_invoice(
                amount_msat=int(sent["value_msat"]),
                description_hash=base64.b64decode(sent["description_hash"]).hex(),
                expiry_seconds=int(sent["expiry"]),
                network=self.network,
            )
            return httpx.Response(200, json={"payment_request": invoice, "r_hash": "x"})
        return httpx.Response(404)

    def receiver(self, *, network: str = TESTNET) -> LndRestReceiver:
        return LndRestReceiver(
            URL, MACAROON, network=network, transport=httpx.MockTransport(self.handler)
        )


def call(receiver: LndRestReceiver, network: str = TESTNET) -> str:
    return receiver.create_invoice(
        amount_msat=12000, description_hash=DIGEST, expiry_seconds=300, network=network
    )


# --------------------------------------------------------------------------- #
# 1) LND に送る依頼の形
# --------------------------------------------------------------------------- #
def test_add_invoice_request_shape() -> None:
    lnd = FakeLnd()
    invoice = call(lnd.receiver())
    assert invoice.startswith("lntb")
    (request,) = lnd.requests
    assert request.method == "POST"
    assert str(request.url) == f"{URL}/v1/invoices"
    assert request.headers["grpc-metadata-macaroon"] == MACAROON
    sent = json.loads(request.content)
    # memo は送らない（inline description を作らせない）。値は LND の REST どおり文字列。
    assert sent == {
        "value_msat": "12000",
        "description_hash": base64.b64encode(bytes.fromhex(DIGEST)).decode(),
        "expiry": "300",
    }


def test_network_not_served_is_refused_without_calling_the_node() -> None:
    lnd = FakeLnd(currency_network=MAINNET)
    with pytest.raises(ReceiverError, match="network_not_served"):
        call(lnd.receiver(network=MAINNET), network=TESTNET)
    assert lnd.requests == []


@pytest.mark.parametrize("status", [401, 403, 500, 503])
def test_node_errors_raise(status) -> None:
    with pytest.raises(ReceiverError, match=f"status_{status}"):
        call(FakeLnd(status=status).receiver())


@pytest.mark.parametrize("body", [{}, {"payment_request": ""}, {"payment_request": 1}, []])
def test_malformed_node_responses_raise(body) -> None:
    with pytest.raises(ReceiverError, match="malformed_response"):
        call(FakeLnd(body=body).receiver())


def test_unreachable_node_raises() -> None:
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    transport = httpx.MockTransport(refuse)
    receiver = LndRestReceiver(URL, MACAROON, network=TESTNET, transport=transport)
    with pytest.raises(ReceiverError, match="unreachable"):
        call(receiver)
    assert receiver.ping() is False


def test_ping_uses_invoice_listing() -> None:
    lnd = FakeLnd()
    assert lnd.receiver().ping() is True
    (request,) = lnd.requests
    assert request.method == "GET"
    assert request.url.path == "/v1/invoices"
    assert request.url.params["num_max_invoices"] == "1"
    assert FakeLnd(status=403).receiver().ping() is False


@pytest.mark.parametrize(
    ("url", "macaroon"),
    [("http://node.example:8080", MACAROON), (URL, ""), (URL, "not-hex")],
)
def test_receiver_refuses_plaintext_and_bad_macaroons(url, macaroon) -> None:
    with pytest.raises(ValueError):
        LndRestReceiver(url, macaroon, network=TESTNET)


def test_macaroon_file_binary_or_hex(tmp_path) -> None:
    binary = tmp_path / "invoice.macaroon"
    binary.write_bytes(bytes.fromhex(MACAROON))
    assert load_macaroon_hex(binary) == MACAROON
    text = tmp_path / "invoice.hex"
    text.write_text(MACAROON.upper() + "\n")
    assert load_macaroon_hex(text) == MACAROON
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    with pytest.raises(ValueError):
        load_macaroon_hex(empty)


# --------------------------------------------------------------------------- #
# 2) /invoice → /settle を LND のアダプタ越しに通す
# --------------------------------------------------------------------------- #
def signed(client: TestClient, path: str, body: dict):
    raw = json.dumps(body).encode()
    value = build_header(KEY_ID, SECRET, timestamp=NOW, method="POST", path=path, body=raw)
    return client.post(
        path, content=raw, headers={"content-type": "application/json", "x-jin-signature": value}
    )


def app_with(tmp_path, receiver, **kwargs) -> TestClient:
    return TestClient(
        create_app(
            store_path=tmp_path / "replay.db",
            credit_path=tmp_path / "credits.db",
            hmac_keys={KEY_ID: SECRET},
            receiver=receiver,
            pay_to=TEST_PAYEE,
            clock=lambda: NOW,
            **kwargs,
        )
    )


def invoice_request(network: str) -> dict:
    return {
        "amount": "12000",
        "descriptionHash": DIGEST,
        "expirySeconds": 300,
        "network": network,
    }


def settle_body(invoice: str, preimage: str, network: str) -> dict:
    reqs = PaymentRequirements(
        scheme="exact",
        network=network,
        asset="BTC",
        amount="12000",
        pay_to=TEST_PAYEE,
        max_timeout_seconds=300,
        extra={
            "assetTransferMethod": "bolt11",
            "paymentFlow": "upfront",
            "invoice": invoice,
            **http_request_binding(
                "GET", f"{ORIGIN}/api/jin/series", public_origin=ORIGIN
            ).extra(),
        },
    )
    payload = PaymentPayload(x402_version=2, accepted=reqs, payload={"preimage": preimage})
    return {
        "paymentPayload": payload.model_dump(by_alias=True),
        "paymentRequirements": reqs.model_dump(by_alias=True),
    }


def test_mainnet_round_trip_through_lnd_adapter(tmp_path) -> None:
    """Voltage の mainnet ノードを想定した往復。mainnet を有効にしたときだけ通る。"""
    lnd = FakeLnd(currency_network=MAINNET)
    client = app_with(tmp_path, lnd.receiver(network=MAINNET), allow_mainnet=True)
    issued = signed(client, "/invoice", invoice_request(MAINNET))
    assert issued.status_code == 200
    invoice = issued.json()["invoice"]
    assert invoice.startswith("lnbc")
    preimage = lnd.signer.pay(invoice)

    first = signed(client, "/settle", settle_body(invoice, preimage, MAINNET))
    assert first.status_code == 200
    assert first.json()["transaction"] == sha256(bytes.fromhex(preimage)).hexdigest()
    again = signed(client, "/settle", settle_body(invoice, preimage, MAINNET))
    assert again.json()["errorReason"] == "duplicate_settlement"


def test_mainnet_node_is_not_used_until_armed(tmp_path) -> None:
    lnd = FakeLnd(currency_network=MAINNET)
    client = app_with(tmp_path, lnd.receiver(network=MAINNET))
    result = signed(client, "/invoice", invoice_request(MAINNET))
    assert result.json()["errorReason"] == "unsupported_network"
    # testnet の依頼は mainnet のノードに送らない。
    result = signed(client, "/invoice", invoice_request(TESTNET))
    assert result.status_code == 502
    assert result.json()["errorReason"] == "invoice_issuance_failed"
    assert lnd.requests == []


def test_node_error_details_do_not_leak(tmp_path) -> None:
    client = app_with(tmp_path, FakeLnd(status=500).receiver())
    result = signed(client, "/invoice", invoice_request(TESTNET))
    assert result.status_code == 502
    assert result.json() == {"success": False, "errorReason": "invoice_issuance_failed"}
    assert "secret" not in result.text


# --------------------------------------------------------------------------- #
# 3) /healthz のノード疎通
# --------------------------------------------------------------------------- #
def test_healthz_reports_reachable_node(tmp_path) -> None:
    result = app_with(tmp_path, FakeLnd().receiver()).get("/healthz")
    assert result.status_code == 200
    assert result.json()["node"] == "ok"
    assert result.json()["invoice_issuance"] is True


def test_healthz_is_503_when_node_is_unreachable(tmp_path) -> None:
    result = app_with(tmp_path, FakeLnd(status=503).receiver()).get("/healthz")
    assert result.status_code == 503
    body = result.json()
    assert body["status"] == "degraded"
    assert body["node"] == "unreachable"
    assert body["invoice_issuance"] is False


def test_healthz_reports_no_issuance_before_mainnet_is_armed(tmp_path) -> None:
    lnd = FakeLnd(currency_network=MAINNET)
    body = app_with(tmp_path, lnd.receiver(network=MAINNET)).get("/healthz").json()
    assert body["node"] == "ok"
    assert body["invoice_issuance"] is False
    armed = tmp_path / "armed"
    armed.mkdir()
    body = app_with(armed, lnd.receiver(network=MAINNET), allow_mainnet=True).get("/healthz")
    assert body.json()["invoice_issuance"] is True


def test_healthz_without_node(tmp_path) -> None:
    client = TestClient(create_app(store_path=tmp_path / "r.db", clock=lambda: NOW))
    result = client.get("/healthz")
    assert result.status_code == 200
    assert result.json()["node"] == "not_configured"
    assert result.json()["invoice_issuance"] is False


# --------------------------------------------------------------------------- #
# 4) 起動の設定（LND_*）
# --------------------------------------------------------------------------- #
def env(tmp_path, **overrides) -> dict[str, str]:
    macaroon = tmp_path / "invoice.macaroon"
    macaroon.write_bytes(bytes.fromhex(MACAROON))
    base = {
        "LNBTC_STORE_PATH": str(tmp_path / "replay.db"),
        "LNBTC_CREDIT_PATH": str(tmp_path / "credits.db"),
        "LNBTC_HMAC_KEYS": f"{KEY_ID}:{SECRET}",
        "LNBTC_PAY_TO": TEST_PAYEE,
        "LND_REST_URL": URL,
        "LND_MACAROON_PATH": str(macaroon),
        "LND_NETWORK": "mainnet",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def test_lnd_settings_build_a_receiver(tmp_path) -> None:
    settings = load_settings(env(tmp_path))
    assert settings.lnd_network == MAINNET
    # 実ノードに接続しないよう、/healthz は叩かずにアダプタができることだけを確かめる。
    receiver = build_receiver(settings)
    assert isinstance(receiver, LndRestReceiver)
    assert receiver.network == MAINNET


def test_no_lnd_url_means_no_receiver(tmp_path) -> None:
    app, _ = build_app(env(tmp_path, LND_REST_URL=None))
    assert TestClient(app).get("/healthz").json()["node"] == "not_configured"


@pytest.mark.parametrize(
    "overrides",
    [
        {"LND_REST_URL": "http://node.example:8080"},
        {"LND_MACAROON_PATH": None},
        {"LND_NETWORK": None},
        {"LND_NETWORK": "signet"},
        {"LNBTC_PAY_TO": None},
    ],
)
def test_incomplete_lnd_settings_stop_startup(tmp_path, overrides) -> None:
    with pytest.raises(ConfigError):
        load_settings(env(tmp_path, **overrides))


def test_unreadable_macaroon_stops_startup_without_leaking(tmp_path) -> None:
    with pytest.raises(ConfigError) as raised:
        build_app(env(tmp_path, LND_MACAROON_PATH=str(tmp_path / "missing")))
    assert MACAROON not in str(raised.value)
