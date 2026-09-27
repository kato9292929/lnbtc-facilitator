"""消費後に提供が失敗した場合のクレジット台帳（v3 §6）。

仕様は消費を保護対象ハンドラの実行「前」に完了させることを要求する。そのため
消費後に上流データの取得が失敗すると「払ったのに返ってこない」が構造的に起きる。
その1件を記録し、次回の再送で消費なしに提供できるようにする。

`/settle` の意味は変えない。仕様のテストベクタは「同じ証明を2回出したら片方は
duplicate_settlement」を要求しているので、消費済みキーに対して `/settle` が成功を
返すようにはしない。クレジットは JIN 固有の別経路（`/credits` と `/credits/claim`）で扱う。

resource server の流れ:
1. `/settle` 成功 -> ハンドラ実行。
2. ハンドラ失敗 -> `/credits` に記録（消費キー・失敗時刻・理由・未提供リソース）。
3. 同じ preimage で再送が来る -> `/settle` は duplicate_settlement を返す。
4. そこで `/credits/claim` を叩き、granted なら消費なしで提供する。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path


class CreditLedger:
    """消費キー単位のクレジット。1 件の消費に対して引き換えは 1 回だけ。"""

    def __init__(
        self, path: str | Path, *, clock: Callable[[], float] = time.time
    ) -> None:
        if str(path) in ("", ":memory:") or str(path).startswith("file:"):
            raise ValueError("クレジット台帳には永続的な SQLite ファイルが必要です")
        self.path = Path(path).resolve()
        self._clock = clock
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS x402_lightning_credits ("
                "consumption_key TEXT PRIMARY KEY, "
                "resource TEXT NOT NULL, "
                "failed_at INTEGER NOT NULL, "
                "reason TEXT NOT NULL, "
                "redeemed_at INTEGER)"
            )

    def record(self, consumption_key: str, *, resource: str, reason: str, failed_at: int) -> bool:
        """未提供を記録する。同じ消費キーで二重に積まない（True = 新規記録）。"""
        if not consumption_key or not resource or not reason:
            raise ValueError("consumption_key / resource / reason は必須です")
        with closing(sqlite3.connect(self.path)) as connection, connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO x402_lightning_credits "
                "(consumption_key, resource, failed_at, reason) VALUES (?, ?, ?, ?)",
                (consumption_key, resource, int(failed_at), reason),
            )
            return cursor.rowcount == 1

    def claim(self, consumption_key: str, *, resource: str) -> bool:
        """未引き換えのクレジットがあれば引き換えて True。

        リソースが一致しないクレジットでは引き換えない（別エンドポイントへの流用を防ぐ）。
        引き換えは原子的で、同時に来ても 1 回しか成立しない。
        """
        now = int(self._clock())
        with closing(sqlite3.connect(self.path)) as connection, connection:
            cursor = connection.execute(
                "UPDATE x402_lightning_credits SET redeemed_at = ? "
                "WHERE consumption_key = ? AND resource = ? AND redeemed_at IS NULL",
                (now, consumption_key, resource),
            )
            return cursor.rowcount == 1

    def outstanding(self) -> list[dict]:
        """未引き換えのクレジット一覧（運用確認用）。"""
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT consumption_key, resource, failed_at, reason "
                "FROM x402_lightning_credits WHERE redeemed_at IS NULL "
                "ORDER BY failed_at"
            ).fetchall()
        return [dict(row) for row in rows]
