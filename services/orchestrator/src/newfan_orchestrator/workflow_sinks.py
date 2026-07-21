# mypy: ignore-errors
"""sink アダプタ（§16 設計 v0.2 §9 / P6）。

SQL は newfan_workflow.dbsink（dry-run と同一実装）が生成済みのものを受け取り、
ここでは**実行だけ**を行う。全行を 1 トランザクションで書く（部分書込みを残さない。
クラッシュ時は全 rollback → 再実行は台帳キー/upsert の冪等で二重にならない）。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class PgDbWriter:
    """顧客 DB（PostgreSQL）へのプリペアド書込み。

    接続は都度張って閉じる（sink は run の終端で 1 回だけ。プールを持つほど
    頻度が無く、顧客 DB へ常時接続を残さない方が行儀が良い）。
    """

    def __init__(self, *, connect_timeout: float = 10.0) -> None:
        self._timeout = connect_timeout

    def write(self, dsn: str, sql: str, rows: list[dict[str, Any]]) -> int:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=self._timeout) as conn:
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
                affected = cur.rowcount
            conn.commit()
        # executemany の rowcount は ON CONFLICT DO NOTHING で捨てた行を含まない
        # ドライバもある。負値（不明）は 0 に丸める
        return max(affected, 0)
