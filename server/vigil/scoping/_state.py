"""The active site scope: the one ContextVar every other part of vigil.scoping
reads. Held here so ``sites``, ``context`` and ``managers`` can share it without
importing one another; ``vigil.scoping`` re-exports it as before.
"""
import contextvars

_scope: contextvars.ContextVar = contextvars.ContextVar("vigil_site_scope", default=None)
