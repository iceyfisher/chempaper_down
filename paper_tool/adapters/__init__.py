from .acs import ACSAdapter
from .aip import AIPAdapter
from .aaas import AAASAdapter
from .rsc import RSCAdapter
from .wiley import WileyAdapter
from .springer import SpringerAdapter
from .elsevier import ElsevierAdapter
from .ieee import IeeeAdapter
from .cnki import CnkiAdapter

# CNKI stays last: it is the fallback adapter that claims every DOI the
# dedicated publisher adapters do not match.
ALL_ADAPTERS = [
    ACSAdapter,
    AIPAdapter,
    AAASAdapter,
    RSCAdapter,
    WileyAdapter,
    SpringerAdapter,
    ElsevierAdapter,
    IeeeAdapter,
    CnkiAdapter,
]

__all__ = [
    "ACSAdapter", "AIPAdapter", "AAASAdapter", "RSCAdapter", "WileyAdapter",
    "SpringerAdapter", "ElsevierAdapter", "IeeeAdapter", "CnkiAdapter", "ALL_ADAPTERS"
]
