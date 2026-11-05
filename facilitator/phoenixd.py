"""phoenixd（ACINQ）に invoice を発行させる受信側アダプタ。同じ VPS の 127.0.0.1:9740 を呼ぶ。

`facilitator/vendor/lnbtc/types.py` の `LightningReceiver` を満たし、`facilitator/lnd.py` と
同じ形（`create_invoice`・`ping`・`network`）にしてある。phoenixd v0.9.3 のソース
（`src/commonMain/kotlin/fr/acinq/phoenixd/Api.kt`）で確かめた振る舞いに合わせている。

- 発行は `POST /createinvoice`（form-urlencoded）。`descriptionHash`（16 進）・`amountSat`・
  `expirySeconds`・`externalId` を送る。`description` は送らない（仕様が inline description を
  禁じているため。phoenixd は description と descriptionHash の片方だけを受け付ける）
- 金額は sat 単位でしか渡せない。msat が 1000 で割り切れない依頼はノードに送らずに拒否する
  （切り捨てると invoice の金額が依頼と食い違う）
- 認証は HTTP basic。ユーザー名は見られず、パスワードだけで判定される。ここには
  `http-password-limited-access` だけを渡す。primary（full access）のパスワードは送金ができるので
  渡さない
- 疎通の確認は `GET /getinfo`。phoenixd の chain が設定した network と違えば届かない扱いにする
- 着金の確認は `GET /payments/incoming/{paymentHash}`。この応答は支払い前でも preimage を含むので
  （限定パスワードでも読める）、preimage は捨てて返す。preimage を持つ者は払わずに `/settle` を
  通せるため、ログにも出さない
- webhook は使わない。着金は照会で確かめる

ノードの応答本文や例外は外へ出さない。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from facilitator.lnd import DEFAULT_TIMEOUT_SECONDS, ReceiverError
from facilitator.vendor.lnbtc.constants import MAINNET, TESTNET

#: phoenixd の `/getinfo` の chain と、CAIP-2 の network。
CHAINS = {MAINNET: "mainnet", TESTNET: "testnet"}

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_PAYMENT_HASH = re.compile(r"[0-9a-f]{64}")

#: 着金の照会で外へ返してよい項目。preimage・invoice・説明は含めない。
INCOMING_FIELDS = (
    "paymentHash",
    "externalId",
    "isPaid",
    "isExpired",
    "requestedSat",
    "receivedSat",
    "fees",
    "expiresAt",
    "completedAt",
    "createdAt",
)


class PhoenixdReceiver:
    def __init__(
        self,
        base_url: str,
        password: str,
        *,
        network: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        parts = urlsplit(base_url)
        if parts.scheme == "http" and parts.hostname not in _LOOPBACK:
            # パスワードを平文で外へ流さない。API は同じホストの 127.0.0.1 だけで待ち受ける。
            raise ValueError("phoenixd の URL は http://127.0.0.1 か https で指定してください")
        if parts.scheme not in ("http", "https"):
            raise ValueError("phoenixd の URL は http://127.0.0.1 か https で指定してください")
        if not password:
            raise ValueError("phoenixd のパスワードが空です")
        if network not in CHAINS:
            raise ValueError("network は mainnet か testnet の CAIP-2 で指定してください")
        self.network = network
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            auth=httpx.BasicAuth("", password),
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def create_invoice(
        self, *, amount_msat: int, description_hash: str, expiry_seconds: int, network: str
    ) -> str:
        if network != self.network:
            raise ReceiverError("network_not_served")
        if amount_msat <= 0 or amount_msat % 1000:
            raise ReceiverError("amount_not_whole_sat")
        form = {
            "descriptionHash": description_hash,
            "amountSat": str(amount_msat // 1000),
            "expirySeconds": str(expiry_seconds),
            # 依頼したリクエストハッシュで照会できるようにする。秘密ではない。
            "externalId": description_hash,
        }
        try:
            response = self._client.post("/createinvoice", data=form)
        except httpx.HTTPError as exc:
            raise ReceiverError("unreachable") from exc
        if response.status_code != 200:
            raise ReceiverError(f"status_{response.status_code}")
        try:
            invoice = response.json()["serialized"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ReceiverError("malformed_response") from exc
        if not isinstance(invoice, str) or not invoice:
            raise ReceiverError("malformed_response")
        return invoice

    def ping(self) -> bool:
        """phoenixd に届き、設定した network の chain で動いているか。"""
        try:
            response = self._client.get("/getinfo")
            if response.status_code != 200:
                return False
            return response.json().get("chain") == CHAINS[self.network]
        except (httpx.HTTPError, ValueError, AttributeError):
            return False

    def incoming_payment(self, payment_hash: str) -> dict[str, Any] | None:
        """着金の状態。invoice が無ければ None。preimage は含めない。

        `/settle` の判定には使わない（仕様は preimage をローカルで検証する）。往復の記録と
        運用の確認のためのもの。
        """
        if not isinstance(payment_hash, str) or not _PAYMENT_HASH.fullmatch(payment_hash):
            raise ValueError("payment_hash は 64 桁の小文字 16 進で指定してください")
        try:
            response = self._client.get(f"/payments/incoming/{payment_hash}")
        except httpx.HTTPError as exc:
            raise ReceiverError("unreachable") from exc
        if response.status_code in (204, 404):
            return None
        if response.status_code != 200:
            raise ReceiverError(f"status_{response.status_code}")
        try:
            body = response.json()
            return {key: body.get(key) for key in INCOMING_FIELDS}
        except (ValueError, AttributeError) as exc:
            raise ReceiverError("malformed_response") from exc
