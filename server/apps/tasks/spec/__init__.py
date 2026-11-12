"""apps.tasks.spec, split by concern (sprint M5). Every name the old module had is
importable from here as before."""
from ._common import *  # noqa: F401,F403
from ._params import *  # noqa: F401,F403
from ._flow import *  # noqa: F401,F403
from ._inputs import *  # noqa: F401,F403
from ._detect import *  # noqa: F401,F403
from .parse import parse_and_validate, _validate_relevant, _validate_severity, _validate_boost  # noqa: F401
