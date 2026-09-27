"""msat 建て価格。USD 表示のままにせず、固定した msat を正とする。

仕様は `amount` をミリサトシの正の整数文字列に限り、`$1` や `1 USD` のような
表示を（変換パーサを登録しない限り）拒否することを要求する。したがって相場で揺れる
USD 建てを実行時に変換せず、基準レートで一度決めた msat を設定値として持つ。

`msat = ceil(USD ÷ BTCUSD × 10^11)` を 1 sat 単位に切り上げ、最低 1 sat。
基準レートと出典は docs/lnbtc-pricing.md に記録する。
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

#: 価格を固定した基準 BTC/USD（Bybit, 2026-09-27）。docs/lnbtc-pricing.md を参照。
REFERENCE_BTCUSD = Decimal("84464.00")

#: 見直しの閾値。基準値からこの割合を超えて乖離したら再計算する。
DRIFT_THRESHOLD = Decimal("0.30")

#: mainnet で 1 コールに許す上限（sats）。
MAX_SATS_PER_CALL = 1000

#: 固定した msat 価格。USD 建ては由来の記録であって、実行時の変換には使わない。
PRICES_MSAT: dict[str, int] = {
    "/api/jin/series": 12_000,
    "/api/jin/movers": 24_000,
}

#: 上の msat を導いた USD 建て（記録用）。
ORIGIN_USD: dict[str, str] = {
    "/api/jin/series": "0.01",
    "/api/jin/movers": "0.02",
}


def usd_to_msat(usd: str | Decimal, btcusd: str | Decimal = REFERENCE_BTCUSD) -> int:
    """USD を msat へ。1 sat 単位に切り上げ、最低 1 sat。

    切り上げ単位が sat なのは、BOLT11 の実運用で 1 msat 刻みの請求が扱いにくいため。
    切り上げ方向なので受取側が取りこぼさない。
    """
    rate = Decimal(btcusd)
    if rate <= 0:
        raise ValueError("BTC/USD は正でなければなりません")
    amount = Decimal(usd)
    if amount <= 0:
        raise ValueError("USD は正でなければなりません")
    exact_msat = amount / rate * Decimal(10) ** 11
    sats = (exact_msat / 1000).to_integral_value(rounding=ROUND_CEILING)
    return int(max(sats, Decimal(1))) * 1000


def price_msat(resource_path: str) -> str:
    """仕様どおりミリサトシの正の整数文字列で返す。sats や BTC では持ち回らない。"""
    if resource_path not in PRICES_MSAT:
        raise KeyError(f"価格が未定義のリソース: {resource_path}")
    return str(PRICES_MSAT[resource_path])


def exceeds_call_cap(msat: int, *, max_sats: int = MAX_SATS_PER_CALL) -> bool:
    """1 コール上限（sats）を超えるか。mainnet で設定から強制するための判定。"""
    return msat > max_sats * 1000


def needs_repricing(current_btcusd: str | Decimal, *, reference=REFERENCE_BTCUSD) -> bool:
    """基準レートから DRIFT_THRESHOLD を超えて乖離したか（月初以外の見直し条件）。"""
    rate = Decimal(current_btcusd)
    if rate <= 0:
        raise ValueError("BTC/USD は正でなければなりません")
    return abs(rate - reference) / reference > DRIFT_THRESHOLD
