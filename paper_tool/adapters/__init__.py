from .acs import ACSAdapter
from .aip import AIPAdapter
from .aaas import AAASAdapter
from .rsc import RSCAdapter
from .wiley import WileyAdapter
from .springer import SpringerAdapter
from .elsevier import ElsevierAdapter
from .ieee import IeeeAdapter
from .taylor import TaylorFrancisAdapter
from .mdpi import MdpiAdapter
from .iop import IopAdapter
from .confit import ConfitAdapter
from .aps import ApsAdapter
from .optica import OpticaAdapter
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
    TaylorFrancisAdapter,
    MdpiAdapter,
    # Confit must precede IOP: both match the 10.7567 prefix, but SSDM
    # proceedings live on pub.confit.atlas.jp, not iopscience.
    ConfitAdapter,
    IopAdapter,
    ApsAdapter,
    OpticaAdapter,
    CnkiAdapter,
]

__all__ = [
    "ACSAdapter", "AIPAdapter", "AAASAdapter", "RSCAdapter", "WileyAdapter",
    "SpringerAdapter", "ElsevierAdapter", "IeeeAdapter", "TaylorFrancisAdapter",
    "MdpiAdapter", "IopAdapter", "ConfitAdapter", "ApsAdapter", "OpticaAdapter",
    "CnkiAdapter", "ALL_ADAPTERS"
]
