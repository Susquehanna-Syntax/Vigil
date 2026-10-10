"""The task-language features this agent understands.

The server refuses to dispatch tasks that need a feature this list does not
name — an agent that cannot read a construct would silently misread it (a
pre-phase-04 agent would ignore ``relevant:`` and run the fix on every host
it is told to).
"""

# signed_v2 (SEC-4): the server signs when it sent a task and to which agent,
# and this agent refuses any task that is not signed that way.
# stack_source (2026.14.1): stack_deploy places a Git stack's source folder
# from a ticketed, sha256-checked archive.
FEATURES = ("relevant", "branches", "boost", "signed_v2", "stack_source")
