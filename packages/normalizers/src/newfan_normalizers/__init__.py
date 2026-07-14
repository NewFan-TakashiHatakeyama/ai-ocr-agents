"""正規化器レジストリ（§5.6）。"""

from newfan_normalizers.registry import REGISTRY, Normalizer, normalize
from newfan_normalizers.result import NormalizationResult, NormContext

__all__ = [
    "normalize",
    "REGISTRY",
    "Normalizer",
    "NormalizationResult",
    "NormContext",
]
