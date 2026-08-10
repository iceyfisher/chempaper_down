from .acs import ACSAdapter
from .rsc import RSCAdapter
from .wiley import WileyAdapter
from .springer import SpringerAdapter
from .elsevier import ElsevierAdapter

ALL_ADAPTERS = [ACSAdapter, RSCAdapter, WileyAdapter, SpringerAdapter, ElsevierAdapter]

__all__ = [
    "ACSAdapter", "RSCAdapter", "WileyAdapter", "SpringerAdapter",
    "ElsevierAdapter", "ALL_ADAPTERS"
]
