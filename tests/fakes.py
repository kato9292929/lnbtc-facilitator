"""テスト用の偽の受信側アダプタ。Lightning ノードには接続しない。

仕様が公開しているテスト専用鍵でローカル署名する（tests/test_lnbtc_facilitator.py の
`make_invoice` と同じ作り方）。本番の起動の入口（facilitator/main.py）からは使わない。
"""

from __future__ import annotations

import os
from hashlib import sha256

from bolt11 import Bolt11, encode
from bolt11.models.features import Feature, Features, FeatureState
from bolt11.models.tags import Tag, TagChar, Tags

from facilitator.vendor.lnbtc.constants import NETWORKS

#: 仕様が公開しているテスト専用鍵とそれに対応する payTo。
TEST_KEY = "0" * 63 + "1"
TEST_PAYEE = "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"

#: LND が出す invoice と同じく、支払い側が必須とする 2 つを required で立てる。
LND_LIKE_FEATURES = Features.from_feature_list(
    {
        Feature.var_onion_optin: FeatureState.required,
        Feature.payment_secret: FeatureState.required,
    }
)


class FakeReceiver:
    """`LightningReceiver` を満たす偽物。発行した invoice の preimage を覚えておき、
    `pay()` で「支払った結果として得られる preimage」を返す。

    ずれたノードを再現するため、各項目を依頼から上書きできる。
    """

    def __init__(
        self,
        *,
        clock,
        key: str = TEST_KEY,
        features: Features | None = LND_LIKE_FEATURES,
        override: dict | None = None,
        fail: bool = False,
    ) -> None:
        self._clock = clock
        self._key = key
        self._features = features
        self._override = override or {}
        self._fail = fail
        self._preimages: dict[str, str] = {}
        self.calls: list[dict] = []

    def create_invoice(
        self, *, amount_msat: int, description_hash: str, expiry_seconds: int, network: str
    ) -> str:
        self.calls.append(
            {
                "amount_msat": amount_msat,
                "description_hash": description_hash,
                "expiry_seconds": expiry_seconds,
                "network": network,
            }
        )
        if self._fail:
            raise RuntimeError("node unreachable: secret detail that must not leak")
        values = {
            "amount_msat": amount_msat,
            "description_hash": description_hash,
            "expiry_seconds": expiry_seconds,
            "currency": NETWORKS[network],
            "date": int(self._clock()),
            **self._override,
        }
        preimage = os.urandom(32).hex()
        tags = [
            Tag(TagChar.payment_hash, sha256(bytes.fromhex(preimage)).hexdigest()),
            Tag(TagChar.payment_secret, os.urandom(32).hex()),
            Tag(TagChar.expire_time, values["expiry_seconds"]),
            Tag(TagChar.description_hash, values["description_hash"]),
        ]
        if self._features is not None:
            tags.append(Tag(TagChar.features, self._features))
        invoice = encode(
            Bolt11(
                currency=values["currency"],
                amount_msat=values["amount_msat"],
                date=values["date"],
                tags=Tags(tags),
            ),
            private_key=self._key,
        )
        self._preimages[invoice] = preimage
        return invoice

    def pay(self, invoice: str) -> str:
        """支払いの代わり。発行した invoice の preimage を返す。"""
        return self._preimages[invoice]
