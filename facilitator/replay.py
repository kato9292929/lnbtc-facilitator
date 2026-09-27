"""リプレイストアの保持期限と掃除（vendoring 元に無い部分）。

PR #1873 の `SQLiteReplayStore` は `retain_until` を記録するが、行を消す処理を持たない。
仕様は「`invoice_end + skew` の少なくとも 1 時間後まで保持」「invoice がまだ検証を通る間は
削除してはならない」を要求するため、期限の下限と掃除をこちらで足す。

vendoring 元のファイルは書き換えない（上流がマージされたとき差し替えられるようにする）。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from facilitator.vendor.lnbtc.replay_store import SQLiteReplayStore

#: v2 指示書 §2 の固定保持期間。仕様下限（invoice_end + skew + 1h）を下回らせないため、
#: 実際の保持期限は max(この値, vendoring 側が渡してくる期限) を採る。
DEFAULT_MINIMUM_RETENTION_SECONDS = 24 * 3600


class RetainingSQLiteReplayStore(SQLiteReplayStore):
    """消費キーの挿入に保持期限の下限を与え、期限切れ行だけを掃除できるようにする。"""

    def __init__(
        self,
        path: str | Path,
        *,
        minimum_retention_seconds: int = DEFAULT_MINIMUM_RETENTION_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(minimum_retention_seconds, int) or minimum_retention_seconds < 0:
            raise ValueError("minimum_retention_seconds は非負の整数でなければなりません")
        super().__init__(path)
        self._minimum = minimum_retention_seconds
        self._clock = clock

    def consume(self, key: str, retain_until: int) -> bool:
        """挿入できたら True（= 消費成立）。既にあれば False（= duplicate）。

        保持期限は仕様側の要求（retain_until）と運用上の固定期間の大きいほうを採る。
        固定期間だけにすると maxTimeoutSeconds が長い場合に仕様下限を割る。
        """
        floor = int(self._clock()) + self._minimum
        return super().consume(key, max(int(retain_until), floor))

    def purge(self, now: float | None = None) -> int:
        """保持期限を過ぎた行だけを削除し、削除件数を返す。

        `retain_until <= now` の行しか消さないので、invoice がまだ検証を通る間は残る。
        """
        cutoff = int(self._clock() if now is None else now)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM x402_lightning_consumed WHERE retain_until <= ?", (cutoff,)
            )
            return cursor.rowcount

    def retain_until(self, key: str) -> int | None:
        """記録されている保持期限。未消費なら None。"""
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                "SELECT retain_until FROM x402_lightning_consumed WHERE key = ?", (key,)
            ).fetchone()
        return None if row is None else int(row[0])

    def count(self) -> int:
        with closing(sqlite3.connect(self.path)) as connection:
            return int(
                connection.execute("SELECT COUNT(*) FROM x402_lightning_consumed").fetchone()[0]
            )
