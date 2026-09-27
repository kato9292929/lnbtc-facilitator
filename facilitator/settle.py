"""`/settle` の HTTP 境界。`upfront` flow では `/verify` を呼んではならないので実装しない。

役割分担（仕様どおり）:
- resource server（Vercel 上の Next.js）は実行されるリクエストから requestHash を再計算し、
  `requirements.extra.requestHash` に入れてここへ送る。accepted 側や invoice から取らない。
- ここ（ファシリテータ）が証明を検証し、`network + ":" + payment_hash` を原子的に消費する。
- resource server は `/settle` 成功後にのみ保護対象ハンドラを実行する。

`SettleResponse.payer` は仕様上省略必須。preimage は支払者を明かさないため常に出さない。
`error_message` は入力依存の詳細を含み得るのでワイヤに出さず、`errorReason` だけを返す。

認証は v3 §6 の二段構えの内側（HMAC）。外側の Cloudflare Access サービストークンは
インフラ側の設定で、このアプリは境界の内側にあることを前提にしない。
"""

from __future__ import annotations

import os
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from x402.schemas import PaymentPayload, PaymentRequirements

from facilitator.auth import AuthError, RateLimited, RateLimiter, RequestAuthenticator
from facilitator.credit import CreditLedger
from facilitator.replay import DEFAULT_MINIMUM_RETENTION_SECONDS, RetainingSQLiteReplayStore
from facilitator.vendor.lnbtc.constants import DEFAULT_CLOCK_SKEW, MAINNET, NETWORKS
from facilitator.vendor.lnbtc.exact.facilitator import ExactLnbtcScheme

#: 認証失敗時にワイヤへ出す固定文字列。内部の失敗理由は出さない。
UNAUTHENTICATED = {"success": False, "errorReason": "unauthenticated"}


class SettleRequest(BaseModel):
    """x402 ファシリテータの /settle 本体。"""

    paymentPayload: PaymentPayload
    paymentRequirements: PaymentRequirements


class CreditRequest(BaseModel):
    """消費後に提供できなかった 1 件（JIN 固有。仕様の一部ではない）。"""

    consumptionKey: str
    resource: str
    reason: str


def _allowed_networks(*, allow_mainnet: bool) -> frozenset[str]:
    """arming guard。mainnet を明示的に許可しない限り testnet だけを受け付ける。"""
    return frozenset(NETWORKS) if allow_mainnet else frozenset(NETWORKS) - {MAINNET}


def create_app(
    *,
    store_path: str | os.PathLike[str],
    credit_path: str | os.PathLike[str] | None = None,
    hmac_keys: dict[str, str] | None = None,
    allow_mainnet: bool = False,
    clock_skew: int = DEFAULT_CLOCK_SKEW,
    minimum_retention_seconds: int = DEFAULT_MINIMUM_RETENTION_SECONDS,
    rate_limit: int = 120,
    rate_limit_window_seconds: int = 60,
    clock=time.time,
) -> FastAPI:
    store = RetainingSQLiteReplayStore(
        store_path, minimum_retention_seconds=minimum_retention_seconds, clock=clock
    )
    credits = CreditLedger(
        credit_path if credit_path is not None else f"{store_path}.credits", clock=clock
    )
    scheme = ExactLnbtcScheme(store, clock=clock, clock_skew=clock_skew)
    allowed = _allowed_networks(allow_mainnet=allow_mainnet)
    # 鍵が渡らない場合は認証なし。テストとローカル検証のみを想定し、healthz で見えるようにする。
    authenticator = (
        RequestAuthenticator(
            hmac_keys,
            clock=clock,
            rate_limiter=RateLimiter(
                limit=rate_limit, window_seconds=rate_limit_window_seconds, clock=clock
            ),
        )
        if hmac_keys
        else None
    )

    app = FastAPI(title="JIN x402 exact/lnbtc facilitator", version="0.2.0")
    app.state.store = store
    app.state.credits = credits
    app.state.allowed_networks = allowed

    async def _authenticate(request: Request) -> tuple[bytes, JSONResponse | None]:
        """本体バイト列と、認証に失敗した場合の応答を返す。"""
        body = await request.body()
        if authenticator is None:
            return body, None
        try:
            authenticator.authenticate(
                request.headers,
                method=request.method,
                path=request.url.path,
                body=body,
            )
        except RateLimited:
            return body, JSONResponse(
                status_code=429, content={"success": False, "errorReason": "rate_limited"}
            )
        except AuthError:
            return body, JSONResponse(status_code=401, content=UNAUTHENTICATED)
        return body, None

    def _parse(model: type[BaseModel], body: bytes) -> Any:
        return model.model_validate_json(body)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "scheme": scheme.scheme,
            "networks": sorted(allowed),
            "clock_skew_seconds": clock_skew,
            "minimum_retention_seconds": minimum_retention_seconds,
            "authenticated": authenticator is not None,
        }

    @app.post("/settle")
    async def settle(request: Request) -> JSONResponse:
        body, denied = await _authenticate(request)
        if denied is not None:
            return denied
        try:
            parsed = _parse(SettleRequest, body)
        except ValidationError:
            return JSONResponse(
                status_code=400,
                content={"success": False, "errorReason": "invalid_exact_lnbtc_request_binding"},
            )
        requirements = parsed.paymentRequirements
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
        result = scheme.settle(parsed.paymentPayload, requirements)
        payload: dict[str, Any] = {
            "success": result.success,
            "transaction": result.transaction,
            "network": result.network,
        }
        if not result.success:
            payload["errorReason"] = result.error_reason
        return JSONResponse(status_code=200 if result.success else 402, content=payload)

    @app.post("/credits")
    async def record_credit(request: Request) -> JSONResponse:
        """消費後にハンドラが失敗した 1 件を記録する。"""
        body, denied = await _authenticate(request)
        if denied is not None:
            return denied
        try:
            parsed = _parse(CreditRequest, body)
        except ValidationError:
            return JSONResponse(
                status_code=400, content={"success": False, "errorReason": "invalid_credit_request"}
            )
        recorded = credits.record(
            parsed.consumptionKey,
            resource=parsed.resource,
            reason=parsed.reason,
            failed_at=int(clock()),
        )
        return JSONResponse(status_code=200, content={"success": True, "recorded": recorded})

    @app.post("/credits/claim")
    async def claim_credit(request: Request) -> JSONResponse:
        """duplicate_settlement を受けた再送に対し、未提供分を1回だけ許可する。"""
        body, denied = await _authenticate(request)
        if denied is not None:
            return denied
        try:
            parsed = _parse(CreditRequest, body)
        except ValidationError:
            return JSONResponse(
                status_code=400, content={"success": False, "errorReason": "invalid_credit_request"}
            )
        granted = credits.claim(parsed.consumptionKey, resource=parsed.resource)
        return JSONResponse(status_code=200, content={"success": True, "granted": granted})

    @app.post("/verify")
    async def verify() -> JSONResponse:
        # upfront flow は /verify を使わない。実装せず、誤用を明示的に拒否する。
        return JSONResponse(
            status_code=400,
            content={"isValid": False, "invalidReason": "invalid_exact_lnbtc_payment_flow"},
        )

    return app
