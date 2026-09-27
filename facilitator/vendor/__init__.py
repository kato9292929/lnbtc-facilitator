"""x402 本体の未マージ実装を vendoring した領域。

`lnbtc/` は x402-foundation/x402 の PR #1873（`refs/pull/1873/head` = 2d85314）の
`python/x402/mechanisms/lnbtc` をそのまま取り込んだもの。2026-09-27 時点で未マージのため
pypi の `x402` パッケージには含まれない。SDK 本体（`x402.schemas` / `x402.interfaces`）は
pypi の x402==2.24.0 を使うため、相対 import だけを絶対 import に書き換えている。
それ以外の変更は加えない。上流がマージされたらこの領域は削除して pypi 版に切り替える。
"""
