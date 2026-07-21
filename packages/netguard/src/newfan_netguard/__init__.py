"""外向き HTTP の SSRF ガード（§11）。

gateway（Webhook 配信先の登録時）と export（送信直前）の両方が使う。
gateway が export を import すると層が逆転する（API → ワーカーの実装）ため、
両者が依存できる場所に置く。

登録時と送信時の二段で見る:
  - 登録時: 利用者に即座に「その URL は登録できない」と返せる
  - 送信時: DNS は後から書き換わる（DNS rebinding）ため、送る瞬間にも確かめる
"""

from newfan_netguard.url import Resolver, default_resolver, is_blocked_url

__all__ = ["is_blocked_url", "Resolver", "default_resolver"]
