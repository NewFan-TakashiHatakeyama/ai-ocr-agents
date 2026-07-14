from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class CheckResult:
    """1 チェックの結果（§5.7.3）。

    - passed: ハード失敗でないか（warning は passed=True）。
    - elevates: 合格時に関連フィールドを auto-elevation 対象にするか（V-SUM/V-QTY）。
    - fields: 関連フィールド名（validation 添付・auto-elevation 対象の特定に使う）。
    """

    check_id: str
    passed: bool
    severity: Severity
    message: str
    fields: list[str] = field(default_factory=list)
    elevates: bool = False
