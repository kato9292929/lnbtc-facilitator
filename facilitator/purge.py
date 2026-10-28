"""リプレイストアの掃除を 1 回だけ行うコマンド。

    uv run python -m facilitator.purge

ホストの cron か systemd timer から 1 時間ごとに呼ぶ。保持期限（`retain_until`）を過ぎた
行しか消さないので、invoice がまだ検証を通る間の行は残る（tests で固定済み）。
読む環境変数は `LNBTC_STORE_PATH` だけ。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

from facilitator.main import ConfigError, required
from facilitator.replay import RetainingSQLiteReplayStore


def run(env: Mapping[str, str]) -> int:
    """掃除して削除件数を返す。ストアのファイルが無ければ作らずに止める。"""
    path = required(env, "LNBTC_STORE_PATH")
    if not os.path.isfile(path):
        # 誤ったパスで空のストアを作ると、本物のストアが掃除されないまま気づけない。
        raise ConfigError(f"リプレイストアのファイルがありません: {path}")
    return RetainingSQLiteReplayStore(path).purge()


def main() -> None:
    try:
        deleted = run(os.environ)
    except ConfigError as exc:
        raise SystemExit(f"掃除を中止しました: {exc}") from None
    print(f"purged {deleted}", file=sys.stdout)


if __name__ == "__main__":
    main()
