"""
cairn — local-first episodic memory for AI agents.
The path is the retrieval.
"""
# Single source of truth for the version: the installed package metadata, which
# comes straight from pyproject. Never hard-code a second copy that can drift.
try:
    from importlib.metadata import version as _pkg_version, PackageNotFoundError
    try:
        __version__ = _pkg_version("cairn-remembers")
    except PackageNotFoundError:      # a source tree that was never pip-installed
        __version__ = "0.0.0+source"
except Exception:                     # importlib.metadata is stdlib on 3.11+
    __version__ = "0.0.0+source"

from .vault    import Vault, MicroNode
from .schedule import schedule_context, PositionRecord, golden_positions, update_compiled_hits
from .compile  import compile_session

__all__ = [
    "__version__",
    "Vault", "MicroNode",
    "schedule_context", "PositionRecord", "golden_positions", "update_compiled_hits",
    "compile_session",
]
