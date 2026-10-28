"""LND の REST に invoice を発行させる受信側アダプタ（Voltage の LND ノードを想定）。

`facilitator/vendor/lnbtc/types.py` の `LightningReceiver` を満たす。

- 発行は `POST /v1/invoices`。`value_msat`・`description_hash`（REST では base64）・`expiry`
  だけを渡す。`memo` は渡さない（仕様が inline description を禁じているため）
- macaroon は 16 進にして `Grpc-Metadata-macaroon` ヘッダで送る。invoice の発行と照会に
  絞ったもの（`invoices:read`・`invoices:write`）を使う。手順は docs/deploy.md
- 疎通の確認は `GET /v1/invoices?num_max_invoices=1`。`/v1/getinfo` は `info:read` が要り、
  invoice 用の macaroon では通らないため
- ノードは 1 つの network にしか属さないので、設定した network 以外の依頼は送らずに拒否する
- 発行された invoice の中身はここでは確かめない。`/invoice` 側の照合
  （facilitator/invoice.py）がすべての項目を確かめる

ノードの応答本文や例外は資格情報やノードの詳細を含み得るので、外へ出さない。
"""

from __future__ import annotations

import base64
import re
import ssl
from pathlib import Path

import httpx

#: 1 回の呼び出しの上限（秒）。JIN・OSD は /invoice を 1 秒で諦めるので、それより長く
#: 待っても使われない。疎通の確認も同じ値を使う。
DEFAULT_TIMEOUT_SECONDS = 2.0

_HEX = re.compile(r"[0-9a-fA-F]+")


class ReceiverError(Exception):
    """ノードへの発行が失敗した。理由は内部用で、ワイヤには固定文字列しか出さない。"""


def load_macaroon_hex(path: str | Path) -> str:
    """macaroon ファイルを 16 進で読む。バイナリでも、16 進のテキストでも受け付ける。"""
    raw = Path(path).read_bytes()
    text = raw.strip()
    if text and _HEX.fullmatch(text.decode("ascii", errors="replace")) and len(text) % 2 == 0:
        return text.decode("ascii").lower()
    if not raw:
        raise ValueError("macaroon ファイルが空です")
    return raw.hex()


class LndRestReceiver:
    def __init__(
        self,
        base_url: str,
        macaroon_hex: str,
        *,
        network: str,
        tls_cert_path: str | Path | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url.startswith("https://"):
            # macaroon を平文で流さない。
            raise ValueError("LND の REST は https で指定してください")
        if not macaroon_hex or not _HEX.fullmatch(macaroon_hex):
            raise ValueError("macaroon は 16 進で渡してください")
        # 証明書を渡したらそれだけを信頼する（自己署名のノード向け）。渡さなければ OS の CA。
        verify: ssl.SSLContext | bool = (
            ssl.create_default_context(cafile=str(tls_cert_path)) if tls_cert_path else True
        )
        self.network = network
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Grpc-Metadata-macaroon": macaroon_hex},
            timeout=timeout,
            verify=verify,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )

    def create_invoice(
        self, *, amount_msat: int, description_hash: str, expiry_seconds: int, network: str
    ) -> str:
        if network != self.network:
            raise ReceiverError("network_not_served")
        body = {
            "value_msat": str(amount_msat),
            "description_hash": base64.b64encode(bytes.fromhex(description_hash)).decode(),
            "expiry": str(expiry_seconds),
        }
        try:
            response = self._client.post("/v1/invoices", json=body)
        except httpx.HTTPError as exc:
            raise ReceiverError("unreachable") from exc
        if response.status_code != 200:
            raise ReceiverError(f"status_{response.status_code}")
        try:
            invoice = response.json()["payment_request"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ReceiverError("malformed_response") from exc
        if not isinstance(invoice, str) or not invoice:
            raise ReceiverError("malformed_response")
        return invoice

    def ping(self) -> bool:
        """ノードに届き、macaroon で invoice を照会できるか。"""
        try:
            response = self._client.get("/v1/invoices", params={"num_max_invoices": "1"})
        except httpx.HTTPError:
            return False
        return response.status_code == 200
