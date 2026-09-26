"""The task-language features this agent understands.

The server refuses to dispatch tasks that need a feature this list does not
name — an agent that cannot read a construct would silently misread it (a
pre-phase-04 agent would ignore ``relevant:`` and run the fix on every host
it is told to).
"""

FEATURES = ("relevant",)
