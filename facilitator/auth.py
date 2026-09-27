"""ファシリテータのリクエスト認証とレート制限（v3 §6）。

Cloudflare Access のサービストークンが外側の境界。その内側でこの HMAC を検証する。
`/settle` は不可逆な書き込み（消費キーの挿入）を行うので、境界の内側でも無認証にしない。

署名の対象に method と path を含める点は v3 §6 の「timestamp + body」への追加である。
body だけを覆うと、`/settle` 向けに署名されたリクエストを `/credits/claim` など別の
エンドポイントへそのまま投げ替えられる。呼び出し側の実装は1行増えるだけなので含めた。
採らない場合は SIGNED_TEMPLATE を変更すれば戻せる（resource server 側も揃える必要がある）。

ヘッダ形式（1 本）:

    X-JIN-Signature: t=<unix秒>,k=<鍵ID>,v1=<HMAC-SHA256 の16進>

鍵の更新は複数鍵を同時に受理して行う。鍵 ID を載せるのは、どの鍵で来たかを記録し
レート制限を呼び出し元ごとに効かせるため。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections import deque
from collections.abc import Callable, Mapping

SIGNATURE_HEADER = "x-jin-signature"

#: 許容する時刻ずれ（秒）。これを超えたら拒否する。
MAX_TIMESTAMP_SKEW_SECONDS = 30

_HEADER = re.compile(
    r"\At=(?P<t>-?\d{1,20}),k=(?P<k>[A-Za-z0-9._-]{1,64}),v1=(?P<v>[0-9a-f]{64})\Z"
)


class AuthError(Exception):
    """認証失敗。理由は内部ログ用で、ワイヤには固定文字列しか出さない。"""


class RateLimited(Exception):
    """呼び出し元ごとの上限超過。"""


def signed_message(*, timestamp: int, method: str, path: str, body: bytes) -> bytes:
    """署名対象のバイト列。区切りは改行で、可変長フィールドの混同を避ける。"""
    if not isinstance(body, bytes):
        raise TypeError("body は bytes でなければなりません")
    head = f"{timestamp}\n{method.upper()}\n{path}\n".encode()
    return head + body


def sign(secret: str, *, timestamp: int, method: str, path: str, body: bytes) -> str:
    message = signed_message(timestamp=timestamp, method=method, path=path, body=body)
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def build_header(
    key_id: str, secret: str, *, timestamp: int, method: str, path: str, body: bytes
) -> str:
    """呼び出し側（resource server / テスト）が送るヘッダ値を組み立てる。"""
    digest = sign(secret, timestamp=timestamp, method=method, path=path, body=body)
    return f"t={timestamp},k={key_id},v1={digest}"


class RateLimiter:
    """呼び出し元（鍵 ID）ごとのスライディングウィンドウ。上限超過で RateLimited。"""

    def __init__(
        self, *, limit: int, window_seconds: int, clock: Callable[[], float] = time.time
    ) -> None:
        if limit <= 0 or window_seconds <= 0:
            raise ValueError("limit と window_seconds は正でなければなりません")
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def check(self, caller: str) -> None:
        now = self._clock()
        hits = self._hits.setdefault(caller, deque())
        while hits and hits[0] <= now - self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            raise RateLimited(caller)
        hits.append(now)


class RequestAuthenticator:
    """HMAC を検証し、呼び出し元の鍵 ID を返す。比較は定数時間。"""

    def __init__(
        self,
        keys: Mapping[str, str],
        *,
        max_skew_seconds: int = MAX_TIMESTAMP_SKEW_SECONDS,
        clock: Callable[[], float] = time.time,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        if not keys:
            raise ValueError("受理する鍵が1本もありません")
        for key_id, secret in keys.items():
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", key_id):
                raise ValueError(f"鍵 ID の形式が不正です: {key_id}")
            if not isinstance(secret, str) or len(secret) < 32:
                raise ValueError(f"鍵 {key_id} が短すぎます（32 文字以上）")
        if max_skew_seconds < 0:
            raise ValueError("max_skew_seconds は非負でなければなりません")
        self._keys = dict(keys)
        self._skew = max_skew_seconds
        self._clock = clock
        self._rate_limiter = rate_limiter

    def authenticate(
        self, headers: Mapping[str, str], *, method: str, path: str, body: bytes
    ) -> str:
        raw = None
        for name, value in headers.items():
            if name.lower() == SIGNATURE_HEADER:
                raw = value
                break
        if raw is None:
            raise AuthError("signature_missing")
        parsed = _HEADER.match(raw.strip())
        if parsed is None:
            raise AuthError("signature_malformed")

        timestamp = int(parsed["t"])
        if abs(self._clock() - timestamp) > self._skew:
            raise AuthError("timestamp_out_of_window")

        key_id = parsed["k"]
        secret = self._keys.get(key_id)
        if secret is None:
            # 未知の鍵 ID でも、既知の鍵と同じ計算量を通してから落とす。
            secret = next(iter(self._keys.values()))
            expected = sign(secret, timestamp=timestamp, method=method, path=path, body=body)
            hmac.compare_digest(expected, parsed["v"])
            raise AuthError("unknown_key")

        expected = sign(secret, timestamp=timestamp, method=method, path=path, body=body)
        if not hmac.compare_digest(expected, parsed["v"]):
            raise AuthError("signature_mismatch")

        if self._rate_limiter is not None:
            self._rate_limiter.check(key_id)
        return key_id
