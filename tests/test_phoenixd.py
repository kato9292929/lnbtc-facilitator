"""phoenixd の受信側アダプタ（facilitator/phoenixd.py）と、その起動の設定。

実の phoenixd には接続しない。httpx の MockTransport で phoenixd v0.9.3 の HTTP の口を真似る。
偽の phoenixd は受け取った依頼の中身から、仕様のテスト専用鍵で invoice を署名して返す。
"""

from __future__ import annotations

import base64
import json
from hashlib import sha256
from urllib.parse import parse_qs

import httpx
import pytest
from fakes import TEST_PAYEE, FakeReceiver
from fastapi.testclient import TestClient
from x402.schemas import PaymentPayload, PaymentRequirements

from facilitator.auth import build_header
from facilitator.lnd import ReceiverError
from facilitator.main import ConfigError, build_app, build_receiver, load_settings
from facilitator.phoenixd import PhoenixdReceiver
from facilitator.settle import create_app
from facilitator.vendor.lnbtc.binding import http_request_binding
from facilitator.vendor.lnbtc.constants import MAINNET, TESTNET
from facilitator.vendor.lnbtc.validation import decode_invoice

NOW = 1_700_000_000
KEY_ID = "jin-2026-11"
SECRET = "s" * 48
PASSWORD = "limited-" + "p" * 32
URL = "http://127.0.0.1:9740"
ORIGIN = "https://jin.example.com"
BINDING = http_request_binding("GET", f"{ORIGIN}/api/jin/series", public_origin=ORIGIN)
DIGEST = BINDING.digest


class FakePhoenixd:
    """phoenixd の HTTP の口の真似。basic 認証はパスワードだけを見る（本物と同じ）。"""

    def __init__(self, *, chain: str = "mainnet", status: int = 200, body=None):
        self.signer = FakeReceiver(clock=lambda: NOW)
        self.chain = chain
        self.status = status
        self.body = body
        self.requests: list[httpx.Request] = []
        self.issued: dict[str, str] = {}

    def _authorized(self, request: httpx.Request) -> bool:
        header = request.headers.get("authorization", "")
        if not header.startswith("Basic "):
            return False
        _, _, password = base64.b64decode(header[6:]).decode().partition(":")
        return password == PASSWORD

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._authorized(request):
            return httpx.Response(401, text="Invalid authentication")
        if self.status != 200:
            return httpx.Response(self.status, text="secret detail from phoenixd")
        path = request.url.path
        if request.method == "GET" and path == "/getinfo":
            return httpx.Response(200, json={"nodeId": TEST_PAYEE, "chain": self.chain})
        if request.method == "POST" and path == "/createinvoice":
            if self.body is not None:
                return httpx.Response(200, json=self.body)
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            network = MAINNET if self.chain == "mainnet" else TESTNET
            invoice = self.signer.create_invoice(
                amount_msat=int(form["amountSat"]) * 1000,
                description_hash=form["descriptionHash"],
                expiry_seconds=int(form["expirySeconds"]),
                network=network,
            )
            payment_hash = decode_invoice(invoice).payment_hash
            self.issued[payment_hash] = invoice
            return httpx.Response(
                200,
                json={
                    "amountSat": int(form["amountSat"]),
                    "paymentHash": payment_hash,
                    "serialized": invoice,
                },
            )
        if request.method == "GET" and path.startswith("/payments/incoming/"):
            payment_hash = path.rsplit("/", 1)[1]
            invoice = self.issued.get(payment_hash)
            if invoice is None:
                return httpx.Response(204)
            # 本物の phoenixd は支払い前でも preimage を返す。
            return httpx.Response(
                200,
                json={
                    "subType": "lightning",
                    "paymentHash": payment_hash,
                    "preimage": self.signer.pay(invoice),
                    "externalId": DIGEST,
                    "description": None,
                    "invoice": invoice,
                    "isPaid": False,
                    "isExpired": False,
                    "requestedSat": 12,
                    "receivedSat": 0,
                    "fees": 0,
                    "expiresAt": None,
                    "completedAt": None,
                    "createdAt": NOW * 1000,
                },
            )
        return httpx.Response(404)

    def receiver(self, *, network: str = MAINNET, password: str = PASSWORD) -> PhoenixdReceiver:
        return PhoenixdReceiver(
            URL, password, network=network, transport=httpx.MockTransport(self.handler)
        )


def call(receiver: PhoenixdReceiver, *, amount_msat: int = 12000, network: str = MAINNET) -> str:
    return receiver.create_invoice(
        amount_msat=amount_msat, description_hash=DIGEST, expiry_seconds=300, network=network
    )


# --------------------------------------------------------------------------- #
# 1) phoenixd に送る依頼の形
# --------------------------------------------------------------------------- #
def test_createinvoice_request_shape() -> None:
    phoenixd = FakePhoenixd()
    invoice = call(phoenixd.receiver())
    assert invoice.startswith("lnbc")
    (request,) = phoenixd.requests
    assert request.method == "POST"
    assert str(request.url) == f"{URL}/createinvoice"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    form = parse_qs(request.content.decode())
    # description は送らない（inline description を作らせない）。金額は sat。
    assert form == {
        "descriptionHash": [DIGEST],
        "amountSat": ["12"],
        "expirySeconds": ["300"],
        "externalId": [DIGEST],
    }
    credentials = base64.b64decode(request.headers["authorization"][6:]).decode()
    user, _, password = credentials.partition(":")
    assert (user, password) == ("", PASSWORD)


@pytest.mark.parametrize("amount_msat", [12001, 999, 1, 0, -1000])
def test_amounts_that_are_not_whole_sats_are_refused_before_calling(amount_msat) -> None:
    phoenixd = FakePhoenixd()
    with pytest.raises(ReceiverError, match="amount_not_whole_sat"):
        call(phoenixd.receiver(), amount_msat=amount_msat)
    assert phoenixd.requests == []


def test_network_not_served_is_refused_without_calling_the_node() -> None:
    phoenixd = FakePhoenixd()
    with pytest.raises(ReceiverError, match="network_not_served"):
        call(phoenixd.receiver(network=MAINNET), network=TESTNET)
    assert phoenixd.requests == []


@pytest.mark.parametrize("status", [400, 401, 500, 503])
def test_node_errors_raise(status) -> None:
    with pytest.raises(ReceiverError, match=f"status_{status}"):
        call(FakePhoenixd(status=status).receiver())


def test_wrong_password_is_a_node_error() -> None:
    with pytest.raises(ReceiverError, match="status_401"):
        call(FakePhoenixd().receiver(password="primary-would-be-wrong-here"))


@pytest.mark.parametrize("body", [{}, {"serialized": ""}, {"serialized": 1}, []])
def test_malformed_node_responses_raise(body) -> None:
    with pytest.raises(ReceiverError, match="malformed_response"):
        call(FakePhoenixd(body=body).receiver())


def test_unreachable_node_raises() -> None:
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    transport = httpx.MockTransport(refuse)
    receiver = PhoenixdReceiver(URL, PASSWORD, network=MAINNET, transport=transport)
    with pytest.raises(ReceiverError, match="unreachable"):
        call(receiver)
    assert receiver.ping() is False


def test_ping_checks_the_chain() -> None:
    assert FakePhoenixd(chain="mainnet").receiver(network=MAINNET).ping() is True
    assert FakePhoenixd(chain="testnet").receiver(network=MAINNET).ping() is False
    assert FakePhoenixd(chain="testnet").receiver(network=TESTNET).ping() is True
    assert FakePhoenixd(status=500).receiver().ping() is False
    assert FakePhoenixd().receiver(password="wrong-password-wrong-password").ping() is False


@pytest.mark.parametrize(
    ("url", "password"),
    [
        ("http://153.115.37.3:9740", PASSWORD),
        ("ftp://127.0.0.1:9740", PASSWORD),
        (URL, ""),
    ],
)
def test_receiver_refuses_remote_plaintext_and_empty_passwords(url, password) -> None:
    with pytest.raises(ValueError):
        PhoenixdReceiver(url, password, network=MAINNET)


def test_receiver_refuses_unknown_networks() -> None:
    with pytest.raises(ValueError):
        PhoenixdReceiver(URL, PASSWORD, network="lnbtc:signet")


# --------------------------------------------------------------------------- #
# 2) 着金の照会は preimage を外に出さない
# --------------------------------------------------------------------------- #
def test_incoming_payment_drops_the_preimage() -> None:
    phoenixd = FakePhoenixd()
    receiver = phoenixd.receiver()
    invoice = call(receiver)
    payment_hash = decode_invoice(invoice).payment_hash
    status = receiver.incoming_payment(payment_hash)
    assert status["paymentHash"] == payment_hash
    assert status["isPaid"] is False
    assert status["externalId"] == DIGEST
    assert "preimage" not in status
    assert "invoice" not in status
    assert phoenixd.signer.pay(invoice) not in json.dumps(status)


def test_incoming_payment_unknown_hash_is_none() -> None:
    assert FakePhoenixd().receiver().incoming_payment("00" * 32) is None


@pytest.mark.parametrize("value", ["AB" * 32, "ab" * 31, "../getinfo", ""])
def test_incoming_payment_refuses_malformed_hashes(value) -> None:
    phoenixd = FakePhoenixd()
    with pytest.raises(ValueError):
        phoenixd.receiver().incoming_payment(value)
    assert phoenixd.requests == []


# --------------------------------------------------------------------------- #
# 3) /invoice → /settle を phoenixd のアダプタ越しに通す
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


def invoice_request(amount: str = "12000", network: str = MAINNET) -> dict:
    return {"amount": amount, "descriptionHash": DIGEST, "expirySeconds": 300, "network": network}


def settle_body(invoice: str, preimage: str) -> dict:
    reqs = PaymentRequirements(
        scheme="exact",
        network=MAINNET,
        asset="BTC",
        amount="12000",
        pay_to=TEST_PAYEE,
        max_timeout_seconds=300,
        extra={
            "assetTransferMethod": "bolt11",
            "paymentFlow": "upfront",
            "invoice": invoice,
            **BINDING.extra(),
        },
    )
    payload = PaymentPayload(x402_version=2, accepted=reqs, payload={"preimage": preimage})
    return {
        "paymentPayload": payload.model_dump(by_alias=True),
        "paymentRequirements": reqs.model_dump(by_alias=True),
    }


def test_mainnet_round_trip_through_phoenixd_adapter(tmp_path) -> None:
    phoenixd = FakePhoenixd()
    receiver = phoenixd.receiver()
    client = app_with(tmp_path, receiver, allow_mainnet=True)
    issued = signed(client, "/invoice", invoice_request())
    assert issued.status_code == 200
    invoice = issued.json()["invoice"]
    preimage = phoenixd.signer.pay(invoice)

    first = signed(client, "/settle", settle_body(invoice, preimage))
    assert first.status_code == 200
    assert first.json()["transaction"] == sha256(bytes.fromhex(preimage)).hexdigest()
    again = signed(client, "/settle", settle_body(invoice, preimage))
    assert again.json()["errorReason"] == "duplicate_settlement"


def test_whole_sat_rule_surfaces_as_issuance_failure(tmp_path) -> None:
    phoenixd = FakePhoenixd()
    client = app_with(tmp_path, phoenixd.receiver(), allow_mainnet=True)
    result = signed(client, "/invoice", invoice_request(amount="12500"))
    assert result.status_code == 502
    assert result.json() == {"success": False, "errorReason": "invoice_issuance_failed"}
    assert phoenixd.requests == []


def test_node_error_details_do_not_leak(tmp_path) -> None:
    client = app_with(tmp_path, FakePhoenixd(status=500).receiver(), allow_mainnet=True)
    result = signed(client, "/invoice", invoice_request())
    assert result.status_code == 502
    assert "secret" not in result.text


def test_healthz_reflects_phoenixd(tmp_path) -> None:
    ok = app_with(tmp_path, FakePhoenixd().receiver(), allow_mainnet=True).get("/healthz")
    assert ok.status_code == 200
    assert ok.json()["node"] == "ok"
    assert ok.json()["invoice_issuance"] is True
    (tmp_path / "down").mkdir()
    down = app_with(tmp_path / "down", FakePhoenixd(status=503).receiver(), allow_mainnet=True)
    assert down.get("/healthz").status_code == 503


# --------------------------------------------------------------------------- #
# 4) 起動の設定（PHOENIXD_*）
# --------------------------------------------------------------------------- #
def env(tmp_path, **overrides) -> dict[str, str]:
    password = tmp_path / "phoenixd-limited-password"
    password.write_text(PASSWORD + "\n")
    base = {
        "LNBTC_STORE_PATH": str(tmp_path / "replay.db"),
        "LNBTC_CREDIT_PATH": str(tmp_path / "credits.db"),
        "LNBTC_HMAC_KEYS": f"{KEY_ID}:{SECRET}",
        "LNBTC_PAY_TO": TEST_PAYEE,
        "PHOENIXD_URL": URL,
        "PHOENIXD_PASSWORD_FILE": str(password),
        "PHOENIXD_NETWORK": "mainnet",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def test_phoenixd_settings_build_a_receiver(tmp_path) -> None:
    settings = load_settings(env(tmp_path))
    assert settings.phoenixd_network == MAINNET
    assert PASSWORD not in repr(settings)
    receiver = build_receiver(settings)
    assert isinstance(receiver, PhoenixdReceiver)
    assert receiver.network == MAINNET


@pytest.mark.parametrize(
    "overrides",
    [
        {"PHOENIXD_PASSWORD_FILE": None},
        {"PHOENIXD_NETWORK": None},
        {"PHOENIXD_NETWORK": "signet"},
        {"LNBTC_PAY_TO": None},
        {"LND_REST_URL": "https://node.example:8080"},
    ],
)
def test_incomplete_phoenixd_settings_stop_startup(tmp_path, overrides) -> None:
    with pytest.raises(ConfigError):
        load_settings(env(tmp_path, **overrides))


def test_remote_plaintext_url_stops_startup(tmp_path) -> None:
    with pytest.raises(ConfigError):
        build_app(env(tmp_path, PHOENIXD_URL="http://153.115.37.3:9740"))


def test_unreadable_or_multiline_password_stops_startup_without_leaking(tmp_path) -> None:
    with pytest.raises(ConfigError) as raised:
        build_app(env(tmp_path, PHOENIXD_PASSWORD_FILE=str(tmp_path / "missing")))
    assert PASSWORD not in str(raised.value)
    multi = tmp_path / "multi"
    multi.write_text(PASSWORD + "\nsecond-line\n")
    with pytest.raises(ConfigError) as raised:
        build_app(env(tmp_path, PHOENIXD_PASSWORD_FILE=str(multi)))
    assert PASSWORD not in str(raised.value)
