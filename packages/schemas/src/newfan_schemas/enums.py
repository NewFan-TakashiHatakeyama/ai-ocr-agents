from __future__ import annotations

from enum import Enum


class SpanSource(str, Enum):
    OCR = "ocr"
    VL = "vl"


class ReviewStatus(str, Enum):
    AUTO = "auto"
    PENDING = "pending"
    CORRECTED = "corrected"
    APPROVED = "approved"


class RunStatus(str, Enum):
    PROCESSING = "processing"
    NEEDS_REVIEW = "needs_review"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class DocStatus(str, Enum):
    UPLOADED = "uploaded"
    QUEUED = "queued"
    PROCESSING = "processing"
    NEEDS_REVIEW = "needs_review"
    IN_REVIEW = "in_review"
    CONFIRMED = "confirmed"
    EXPORTED = "exported"
    FAILED = "failed"


class FieldType(str, Enum):
    """§5.6 正規化器レジストリのキー。"""

    STRING = "string"
    DATE = "date"
    MONEY_JPY = "money_jpy"
    NUMBER = "number"
    TAX_RATE_JP = "tax_rate_jp"
    JP_INVOICE_REG_NO = "jp_invoice_reg_no"
    JP_BANK_ACCOUNT = "jp_bank_account"
    TABLE = "table"
