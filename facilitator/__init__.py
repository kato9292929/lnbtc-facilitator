"""x402 exact/lnbtc のファシリテータ（別サービス・単一ホスト）。

JIN の配信経路は Vercel 上の Next.js で、ファイルシステムが揮発し複数インスタンスが並列に
立つため、リプレイストアを resource server 側に置けない。仕様も検証と消費をファシリテータの
`/settle` 側に置いている。したがって Lightning ノード資格情報とリプレイストアは
このサービスだけが持つ。

`upfront` flow では `/verify` を呼んではならないため、`/verify` は実装しない。
"""
