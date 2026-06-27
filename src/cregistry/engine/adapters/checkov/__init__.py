"""Checkov engine adapter for the constraint registry.

Public surface
--------------
CheckovAdapter
    ``EngineAdapter`` implementation for Checkov.  Register in config as::

        engines:
          - name: checkov
            adapter: "cregistry.engine.adapters.checkov:CheckovAdapter"
            options: {min_level: warning}

import_catalog
    Standalone catalog-importer function (CONTRACTS §6).  NOT part of the
    adapter interface; invoked by tooling to seed the registry with stubs.
"""

from .adapter import CheckovAdapter
from .importer import import_catalog

__all__ = ["CheckovAdapter", "import_catalog"]
