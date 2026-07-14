"""ingest-svc: 取込・ページ分割・前処理（§5.1 / §5.2, DD-01/ADR-0002）。"""

from newfan_ingest.errors import IngestError
from newfan_ingest.models import IngestResult, PageImage, UploadInput
from newfan_ingest.service import IngestService
from newfan_ingest.validation import IngestLimits, detect_kind, validate_upload

__all__ = [
    "IngestService",
    "IngestResult",
    "PageImage",
    "UploadInput",
    "IngestError",
    "IngestLimits",
    "validate_upload",
    "detect_kind",
]
