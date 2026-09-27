"""`/settle` の HTTP 境界。`upfront` flow では `/verify` を呼んではならないので実装しない。

役割分担（仕様どおり）:
- resource server（Vercel 上の Next.js）は実行されるリクエストから requestHash を再計算し、
  `requirements.extra.requestHash` に入れてここへ送る。accepted 側や invoice から取らない。
- ここ（ファシリテータ）が証明を検証し、`network + ":" + payment_hash` を原子的に消費する。
- resource server は `/settle` 成功後にのみ保護対象ハンドラを実行する。

`SettleResponse.payer` は仕様上省略必須。preimage は支払者を明かさないため常に出さない。
"""

from __future__ import annotations

import os
import time
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from x402.schemas import PaymentPayload, PaymentRequirements

from facilitator.replay import DEFAULT_MINIMUM_RETENTION_SECONDS, RetainingSQLiteReplayStore
from facilitator.vendor.lnbtc.constants import DEFAULT_CLOCK_SKEW, MAINNET, NETWORKS
from facilitator.vendor.lnbtc.exact.facilitator import ExactLnbtcScheme


class SettleRequest(BaseModel):
    """x402 ファシリテータの /settle 本体。"""

    paymentPayload: PaymentPayload
    paymentRequirements: PaymentRequirements


def _allowed_networks(*, allow_mainnet: bool) -> frozenset[str]:
    """arming guard。mainnet を明示的に許可しない限り testnet だけを受け付ける。"""
    return frozenset(NETWORKS) if allow_mainnet else frozenset(NETWORKS) - {MAINNET}


def create_app(
    *,
    store_path: str | os.PathLike[str],
    allow_mainnet: bool = False,
    clock_skew: int = DEFAULT_CLOCK_SKEW,
    minimum_retention_seconds: int = DEFAULT_MINIMUM_RETENTION_SECONDS,
    clock=time.time,
) -> FastAPI:
    store = RetainingSQLiteReplayStore(
        store_path, minimum_retention_seconds=minimum_retention_seconds, clock=clock
    )
    scheme = ExactLnbtcScheme(store, clock=clock, clock_skew=clock_skew)
    allowed = _allowed_networks(allow_mainnet=allow_mainnet)

    app = FastAPI(title="JIN x402 exact/lnbtc facilitator", version="0.1.0")
    app.state.store = store
    app.state.allowed_networks = allowed

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "scheme": scheme.scheme,
            "networks": sorted(allowed),
            "clock_skew_seconds": clock_skew,
            "minimum_retention_seconds": minimum_retention_seconds,
        }

    @app.post("/settle")
    def settle(body: SettleRequest) -> JSONResponse:
        requirements = body.paymentRequirements
        # ネットワーク不許可は検証にも消費にも進ませない（testnet 先行の arming guard）。
        if requirements.network not in allowed:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "errorReason": "unsupported_network",
                    "transaction": "",
                    "network": requirements.network,
                },
            )
        result = scheme.settle(body.paymentPayload, requirements)
        payload = {
            "success": result.success,
            "transaction": result.transaction,
            "network": result.network,
        }
        if not result.success:
            payload["errorReason"] = result.error_reason
        # payer は省略必須。error_message は入力依存の詳細を含み得るので外に出さない。
        return JSONResponse(status_code=200 if result.success else 402, content=payload)

    @app.post("/verify")
    def verify() -> JSONResponse:
        # upfront flow は /verify を使わない。実装せず、誤用を明示的に拒否する。
        return JSONResponse(
            status_code=400,
            content={"isValid": False, "invalidReason": "invalid_exact_lnbtc_payment_flow"},
        )

    @app.exception_handler(ValidationError)
    def _on_validation_error(_request, _exc) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"success": False, "errorReason": "invalid_exact_lnbtc_request_binding"},
        )

    return app
