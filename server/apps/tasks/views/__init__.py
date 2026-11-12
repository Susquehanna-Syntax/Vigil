"""apps.tasks.views, split by concern (sprint M5). Every name the old module had is
importable from here as before."""
from ._common import *  # noqa: F401,F403
from ._ingest import *  # noqa: F401,F403
from ._sequence import *  # noqa: F401,F403
from ._catalog import *  # noqa: F401,F403
from .agent import task_result  # noqa: F401
from .definitions import editor_templates, definition_list, definition_detail, definition_validate, definition_fork, definition_refusals, definition_archive, action_registry, task_detail  # noqa: F401
from .deploy import definition_deploy  # noqa: F401
from .runs import task_history, run_history, run_detail, run_hunt_results  # noqa: F401
from .rollouts import rollout_collection, rollout_detail, rollout_halt, rollout_batch_halt, rollout_resume, wave_collection, wave_detail, rollout_skip_validation, wave_group_collection, rollout_wave_hosts  # noqa: F401
from .catalog import community_templates, community_fork_plan, community_fork  # noqa: F401
