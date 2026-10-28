"""起動の入口。環境変数から設定を読み、`create_app()` に渡して uvicorn で動かす。

    uv run python -m facilitator.main

`create_app()` は HMAC 鍵が無いと認証なしで動くが、それはテスト用に限る。ここでは鍵が
1 本も無ければ起動を止める。

uvicorn は 1 プロセスで動かす。レート制限がプロセス内のメモリにあるため、複数ワーカーに
すると上限がワーカー数倍に緩む。

受信ノードは LND の REST（Voltage を想定、facilitator/lnd.py）。`LND_REST_URL` を
設定しなければアダプタを作らず、`/invoice` は 503 を返す。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from facilitator.pricing import MAX_SATS_PER_CALL
from facilitator.vendor.lnbtc.constants import DEFAULT_CLOCK_SKEW, MAINNET, TESTNET

#: `LND_NETWORK` の値と CAIP-2 の network。ノードは 1 つの network にしか属さない。
LND_NETWORKS = {"mainnet": MAINNET, "testnet": TESTNET}

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_RATE_LIMIT = 120
DEFAULT_RATE_LIMIT_WINDOW_SECONDS = 60


class ConfigError(Exception):
    """設定の誤り。起動を止める。秘密の値はメッセージに含めない。"""


@dataclass(frozen=True)
class Settings:
    store_path: str
    credit_path: str
    hmac_keys: dict[str, str] = field(repr=False)
    allow_mainnet: bool = False
    clock_skew: int = DEFAULT_CLOCK_SKEW
    max_sats_per_call: int = MAX_SATS_PER_CALL
    rate_limit: int = DEFAULT_RATE_LIMIT
    rate_limit_window_seconds: int = DEFAULT_RATE_LIMIT_WINDOW_SECONDS
    pay_to: str | None = None
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    lnd_rest_url: str | None = None
    lnd_macaroon_path: str | None = None
    lnd_tls_cert_path: str | None = None
    lnd_network: str | None = None

    def app_kwargs(self) -> dict[str, Any]:
        return {
            "store_path": self.store_path,
            "credit_path": self.credit_path,
            "hmac_keys": self.hmac_keys,
            "allow_mainnet": self.allow_mainnet,
            "clock_skew": self.clock_skew,
            "max_sats_per_call": self.max_sats_per_call,
            "rate_limit": self.rate_limit,
            "rate_limit_window_seconds": self.rate_limit_window_seconds,
            "pay_to": self.pay_to,
        }


def required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} が設定されていません")
    return value


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw not in ("true", "false"):
        # "1" や "yes" を黙って false 扱いにすると mainnet の切り替えを読み違える。
        raise ConfigError(f"{name} は true か false で指定してください")
    return raw == "true"


def _int(env: Mapping[str, str], name: str, default: int, *, minimum: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    if not raw.isdigit() or int(raw) < minimum:
        raise ConfigError(f"{name} は {minimum} 以上の整数で指定してください")
    return int(raw)


def parse_hmac_keys(raw: str) -> dict[str, str]:
    """`鍵ID:秘密,鍵ID:秘密` を読む。秘密に `:` が含まれてもよい（最初の `:` で分ける）。"""
    keys: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        key_id, sep, secret = entry.partition(":")
        if not sep or not key_id or not secret:
            raise ConfigError("LNBTC_HMAC_KEYS の形式が不正です（鍵ID:秘密,鍵ID:秘密）")
        if key_id in keys:
            raise ConfigError(f"LNBTC_HMAC_KEYS に鍵 ID {key_id} が重複しています")
        keys[key_id] = secret
    if not keys:
        raise ConfigError("LNBTC_HMAC_KEYS に鍵が 1 本もありません")
    return keys


def load_settings(env: Mapping[str, str]) -> Settings:
    store_path = required(env, "LNBTC_STORE_PATH")
    credit_path = required(env, "LNBTC_CREDIT_PATH")
    if os.path.abspath(store_path) == os.path.abspath(credit_path):
        raise ConfigError("LNBTC_STORE_PATH と LNBTC_CREDIT_PATH は別のファイルにしてください")
    hmac_keys = parse_hmac_keys(env.get("LNBTC_HMAC_KEYS", ""))
    pay_to = env.get("LNBTC_PAY_TO", "").strip() or None
    if pay_to is not None and not re.fullmatch(r"0[23][0-9a-f]{64}", pay_to):
        raise ConfigError("LNBTC_PAY_TO は 66 桁・小文字 16 進の圧縮公開鍵で指定してください")
    lnd_rest_url = env.get("LND_REST_URL", "").strip() or None
    lnd_macaroon_path = lnd_network = None
    if lnd_rest_url is not None:
        if not lnd_rest_url.startswith("https://"):
            raise ConfigError("LND_REST_URL は https:// で指定してください")
        lnd_macaroon_path = required(env, "LND_MACAROON_PATH")
        network_name = required(env, "LND_NETWORK").lower()
        if network_name not in LND_NETWORKS:
            raise ConfigError("LND_NETWORK は mainnet か testnet で指定してください")
        lnd_network = LND_NETWORKS[network_name]
        if pay_to is None:
            # 照合に使う署名鍵が無いと、発行した invoice を確かめられない。
            raise ConfigError("LND_REST_URL を使うときは LNBTC_PAY_TO も設定してください")
    return Settings(
        store_path=store_path,
        credit_path=credit_path,
        hmac_keys=hmac_keys,
        allow_mainnet=_bool(env, "LNBTC_ALLOW_MAINNET", False),
        clock_skew=_int(env, "LNBTC_SKEW_SECONDS", DEFAULT_CLOCK_SKEW, minimum=0),
        max_sats_per_call=_int(env, "LNBTC_MAX_SATS_PER_CALL", MAX_SATS_PER_CALL, minimum=1),
        rate_limit=_int(env, "LNBTC_RATE_LIMIT", DEFAULT_RATE_LIMIT, minimum=1),
        rate_limit_window_seconds=_int(
            env,
            "LNBTC_RATE_LIMIT_WINDOW_SECONDS",
            DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
            minimum=1,
        ),
        pay_to=pay_to,
        host=env.get("LNBTC_HOST", "").strip() or DEFAULT_HOST,
        port=_int(env, "LNBTC_PORT", DEFAULT_PORT, minimum=1),
        lnd_rest_url=lnd_rest_url,
        lnd_macaroon_path=lnd_macaroon_path,
        lnd_tls_cert_path=env.get("LND_TLS_CERT_PATH", "").strip() or None,
        lnd_network=lnd_network,
    )


def build_receiver(settings: Settings):
    """LND の REST のアダプタを作る。`LND_REST_URL` が無ければ None。"""
    if settings.lnd_rest_url is None:
        return None
    from facilitator.lnd import LndRestReceiver, load_macaroon_hex

    try:
        macaroon = load_macaroon_hex(settings.lnd_macaroon_path)
        return LndRestReceiver(
            settings.lnd_rest_url,
            macaroon,
            network=settings.lnd_network,
            tls_cert_path=settings.lnd_tls_cert_path,
        )
    except (OSError, ValueError) as exc:
        # 例外の文面にはパスしか出ない。macaroon の中身は出さない。
        raise ConfigError(f"LND の設定を読めません: {type(exc).__name__}") from exc


def build_app(env: Mapping[str, str]):
    """設定を読んでアプリを作る。鍵の長さなど create_app 側の検査も起動時に効く。"""
    from facilitator.settle import create_app

    settings = load_settings(env)
    receiver = build_receiver(settings)
    try:
        return create_app(**settings.app_kwargs(), receiver=receiver), settings
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def main() -> None:
    import uvicorn

    try:
        app, settings = build_app(os.environ)
    except ConfigError as exc:
        raise SystemExit(f"起動を中止しました: {exc}") from None
    # アプリのオブジェクトを渡すので 1 プロセスで動く（workers は指定しない）。
    uvicorn.run(app, host=settings.host, port=settings.port, proxy_headers=False)


if __name__ == "__main__":
    main()
