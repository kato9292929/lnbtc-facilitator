"""invoice 発行の口（`POST /invoice`）の検証。構成図の ②。

ファシリテータはリクエストハッシュを計算しない。resource server（JIN・OSD）が実行される
リクエストから計算したハッシュを受け取り、それを description hash に載せた invoice を
受信ノード（`LightningReceiver`）に発行させるだけ。

発行された invoice はそのまま返さず、decode し直して依頼どおりか確かめる。ノードの
設定ずれ（通貨・署名鍵・時計など）を 402 に載せて外へ出さないため。

BOLT11 の feature フィールド（`9`）について。仕様の初期フィクスチャは `9` を省いており、
`/settle` 側（vendoring した検証）もそれを受理する。一方 LDK などの支払い側の実装は
`var_onion_optin` と `payment_secret` を欠いた invoice を invalid features として拒否する。
`/settle` の検証は仕様どおりのまま緩めも締めもせず、自分のノードが出す invoice については
ここで両方の feature を要求する。支払えない invoice を 402 に載せないための発行側の方針で、
仕様の要件ではない。
"""

from __future__ import annotations

import re

from bolt11 import Bolt11

from facilitator.vendor.lnbtc.constants import NETWORKS, LightningValidationError
from facilitator.vendor.lnbtc.validation import decode_invoice

_DESCRIPTION_HASH = re.compile(r"[0-9a-f]{64}")

#: 自分のノードが出す invoice に要求する feature。支払い側（LDK など）が必須とするもの。
REQUIRED_FEATURES = ("var_onion_optin", "payment_secret")

#: `/invoice` で受け付ける expiry の上限（秒）。BOLT11 で expiry を省いたときの既定値と同じ。
#: 消費キーの保持期限は invoice の期限から決まるので、上限が無いと呼び出し元の指定で
#: リプレイストアの行をいくらでも長く残させられる。1 回の支払いに使う invoice には足りる。
MAX_EXPIRY_SECONDS = 3600


class IssuanceMismatch(Exception):
    """ノードが依頼と異なる invoice を返した。理由は内部用で、ワイヤには出さない。"""


def is_description_hash(value: object) -> bool:
    return isinstance(value, str) and _DESCRIPTION_HASH.fullmatch(value) is not None


def check_issued_invoice(
    invoice: str,
    *,
    amount_msat: int,
    description_hash: str,
    expiry_seconds: int,
    network: str,
    pay_to: str,
    now: float,
    clock_skew: int,
) -> Bolt11:
    """発行された invoice が依頼どおりかを確かめる。食い違えば IssuanceMismatch。

    `/settle` が後で通す検証（`validate_invoice`）と同じ項目に加え、作成時刻が
    ファシリテータの時計から skew 以内であることと、feature を確かめる。
    """
    try:
        decoded = decode_invoice(invoice)
    except LightningValidationError as exc:
        raise IssuanceMismatch(str(exc)) from exc
    checks = (
        (decoded.description_hash == description_hash, "description_hash"),
        (decoded.amount_msat == amount_msat, "amount"),
        (decoded.expiry == expiry_seconds, "expiry"),
        (decoded.currency == NETWORKS[network], "currency"),
        (decoded.payee == pay_to, "payee"),
        (abs(decoded.date - now) <= clock_skew, "date"),
    )
    for ok, reason in checks:
        if not ok:
            raise IssuanceMismatch(reason)
    features = decoded.features
    for name in REQUIRED_FEATURES:
        if features is None or features.has_feature(name) is None:
            raise IssuanceMismatch(f"features:{name}")
    return decoded
