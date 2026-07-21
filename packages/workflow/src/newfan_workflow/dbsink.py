"""sink.db_write の SQL 生成（DD-12 / §16 設計 v0.2 §9 / P6）。

DD-12 の制約をここで実装する:
- 実行 SQL はプリペアド生成のみ（値は必ずプレースホルダ。文字列連結しない）
- INSERT / UPSERT 限定（UPDATE/DELETE の SQL はそもそも生成できない）
- 書込み先は connection の allowed_tables に**完全一致**で限定（照合が先、クォートは後）
- 1 run あたりの書込み行数上限（既定 1,000）

dry-run（POST /workflows/{id}/dry-run）と実実行（orchestrator の db_write ノード)は
**この同じ関数**で SQL を作る。「プレビュー = 実 SQL」（DoD）は実装の同一性で保証する。

識別子のクォートは自前で行う。table/列名はモデル検証（models._TABLE_PATTERN）と
本モジュールの再検証で `[A-Za-z_][A-Za-z0-9_]*` に限定済みのため、`"名前"` で安全に
引用できる（psycopg.sql.Identifier と同一の出力。psycopg を純パッケージへ持ち込まない）。

冪等（§6.4）: upsert は自然冪等。insert は書込み台帳キー列 `nf_write_key`
（workflow_run_id:node_id:行番号）を付与し `ON CONFLICT (nf_write_key) DO NOTHING` を
付ける。**conflict target を台帳キーに限定する**のが重要で、無指定の ON CONFLICT は
業務キー（例: invoice_no UNIQUE）の重複まで黙って捨て、無音のデータ欠落になる
（レビューで実 PG 再現）。対象テーブルには `nf_write_key TEXT UNIQUE` が必須
（導入手順書に明記。無いと ON CONFLICT 指定が実行時エラーになり、静かに壊れない）。
"""

from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import quote

from newfan_workflow.models import DbWriteConfig

MAX_ROWS_PER_RUN = 1_000
LEDGER_COLUMN = "nf_write_key"

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DbSinkError(ValueError):
    """DD-12 違反（allowed_tables 外・識別子不正・行数超過など）。"""


def _quote(ident: str) -> str:
    if not _IDENT_RE.fullmatch(ident):
        raise DbSinkError(f"識別子ではありません: {ident!r}")
    return f'"{ident}"'


def _quote_table(table: str) -> str:
    return ".".join(_quote(p) for p in table.split("."))


def check_allowed_table(table: str, allowed_tables: Optional[list[str]]) -> None:
    """allowed_tables との**完全一致**照合（DD-12。クォートより先に行う）。"""
    if not allowed_tables or table not in allowed_tables:
        raise DbSinkError(
            f"接続の allowed_tables に無いテーブルです: {table}"
            f"（許可: {sorted(allowed_tables or [])}）"
        )


def check_row_limit(count: int) -> None:
    if count > MAX_ROWS_PER_RUN:
        raise DbSinkError(
            f"1 run の書込み行数上限を超えています: {count} > {MAX_ROWS_PER_RUN}"
        )


def build_db_write_sql(cfg: DbWriteConfig, columns: list[str]) -> str:
    """プリペアド INSERT/UPSERT を生成する。値は %(列名)s の名前付きプレースホルダ。

    columns は書込む列（insert モードでは台帳列 LEDGER_COLUMN を呼び出し側が
    含めて渡す）。upsert の keys が columns に無い場合はエラー（ON CONFLICT の
    対象列が値に無いと必ず失敗するため、実行前に落とす）。
    """
    if not columns:
        raise DbSinkError("書込む列がありません（マッピングが空）")
    if len(set(columns)) != len(columns):
        raise DbSinkError(f"列が重複しています: {columns}")
    cols = [c for c in columns]  # _quote が識別子を再検証する
    col_sql = ", ".join(_quote(c) for c in cols)
    val_sql = ", ".join(f"%({c})s" for c in cols)
    table_sql = _quote_table(cfg.table)

    if cfg.mode == "insert":
        # 台帳キーの一意制約に当たった行**だけ**を黙って捨てる＝再実行で二重挿入しない。
        # target を絞らない ON CONFLICT は業務キー重複まで握りつぶすため禁止（DD-12）
        if LEDGER_COLUMN not in cols:
            raise DbSinkError(f"insert には台帳キー列 {LEDGER_COLUMN} が必要です")
        return (
            f"INSERT INTO {table_sql} ({col_sql}) VALUES ({val_sql})"
            f" ON CONFLICT ({_quote(LEDGER_COLUMN)}) DO NOTHING"
        )

    # upsert
    missing = [k for k in cfg.keys if k not in cols]
    if missing:
        raise DbSinkError(f"upsert のキー列が書込み列にありません: {missing}")
    key_sql = ", ".join(_quote(k) for k in cfg.keys)
    updates = [c for c in cols if c not in cfg.keys]
    if not updates:
        # 全列がキー＝更新するものが無い。存在すれば何もしない
        return (
            f"INSERT INTO {table_sql} ({col_sql}) VALUES ({val_sql})"
            f" ON CONFLICT ({key_sql}) DO NOTHING"
        )
    set_sql = ", ".join(f"{_quote(c)} = EXCLUDED.{_quote(c)}" for c in updates)
    return (
        f"INSERT INTO {table_sql} ({col_sql}) VALUES ({val_sql})"
        f" ON CONFLICT ({key_sql}) DO UPDATE SET {set_sql}"
    )


def build_dsn(config: dict[str, Any], secret: Optional[str]) -> str:
    """connections.config + Secrets Manager の秘密から接続 DSN を組む。

    秘密が `postgresql://` で始まる場合は完全な DSN として採用（運用が最も単純）。
    そうでなければパスワードとして config の host/port/dbname/user と組み合わせる。
    config に秘密は入れない（§16.5。password キーは拒否する）。
    """
    if "password" in config or "secret" in config:
        raise DbSinkError("connections.config に秘密を入れてはいけません（secret_ref を使う）")
    if secret and secret.startswith(("postgresql://", "postgres://")):
        return secret
    if secret and secret.startswith("postgresql+psycopg://"):
        return secret.replace("+psycopg", "", 1)  # SQLAlchemy 形式も受ける
    host = config.get("host")
    dbname = config.get("dbname")
    user = config.get("user")
    if not (host and dbname and user):
        raise DbSinkError("config に host/dbname/user が必要です（または secret_ref に完全な DSN）")
    port = config.get("port", 5432)
    # user/password は percent-encode 必須。Secrets Manager の自動生成パスワードは
    # @ / % 等の記号を普通に含み、生連結だと libpq の URI 解釈が壊れる（レビューで実測）
    auth = quote(str(user), safe="")
    if secret:
        auth += ":" + quote(secret, safe="")
    return f"postgresql://{auth}@{host}:{port}/{quote(str(dbname), safe='')}"
