"""Settings for a build with the commercial directory removed.

Used only by tests_agpl_only. AGPL grants downstream users the right to
delete apps_business/; this keeps that build honest.
"""
from .settings import *  # noqa: F401,F403

INSTALLED_APPS = [a for a in INSTALLED_APPS if not a.startswith("apps_business")]  # noqa: F405
