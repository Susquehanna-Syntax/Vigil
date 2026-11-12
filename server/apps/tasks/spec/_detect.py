"""apps.tasks.spec: shared helpers (detect), moved unchanged."""
from ._common import *  # noqa: F401,F403
from ._params import *  # noqa: F401,F403
from ._flow import *  # noqa: F401,F403
from ._inputs import *  # noqa: F401,F403


def _validate_cves(value: Any) -> list[str]:
    """Optional ``cves`` list — at most 32 unique, normalized CVE ids."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise SpecError("'cves' must be a list")
    if len(value) > 32:
        raise SpecError("'cves' may hold at most 32 entries")
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        cve = str(item).strip().upper()
        if not _CVE_PATTERN.match(cve):
            raise SpecError(f"'cves' entry '{cve}' is not a valid CVE id")
        if cve in seen:
            raise SpecError(f"'cves' entry '{cve}' is duplicated")
        seen.add(cve)
        out.append(cve)
    return out


def _validate_references(value: Any) -> list[str]:
    """Optional ``references`` list — absolute http(s) URLs only."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise SpecError("'references' must be a list")
    if len(value) > 16:
        raise SpecError("'references' may hold at most 16 entries")
    out: list[str] = []
    for item in value:
        ref = str(item).strip()
        if len(ref) > 500:
            raise SpecError("'references' entries must be at most 500 characters")
        parsed = urllib.parse.urlparse(ref)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise SpecError(
                f"'references' entry '{ref}' must be an absolute http(s) URL"
            )
        out.append(ref)
    return out


def _validate_platforms(value: Any) -> list[str]:
    """Optional ``platforms`` list — a subset of the known OS families."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise SpecError("'platforms' must be a list")
    if len(value) > 3:
        raise SpecError("'platforms' may hold at most 3 entries")
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        platform = str(item).strip().lower()
        if platform not in _VALID_PLATFORMS:
            raise SpecError(
                f"'platforms' entry '{platform}' is not a known platform "
                f"({' or '.join(sorted(_VALID_PLATFORMS))})"
            )
        if platform in seen:
            raise SpecError(f"'platforms' entry '{platform}' is duplicated")
        seen.add(platform)
        out.append(platform)
    return out


def _deploy_params(spec: dict, steps_payload: list[dict]) -> dict:
    """The signed task params: steps + variables, plus the resolved
    ``relevant:`` tree when the definition declares one (phase 04 makes
    the agent evaluate it) and the branch flow tree when the definition
    branches (phase 06 makes the agent follow it)."""
    params = {"steps": steps_payload,
              "variables": spec.get("resolved_inputs") or {}}
    if spec.get("relevant"):
        params["relevant"] = spec["relevant"]
    if spec.get("boost"):
        params["boost"] = [item["probe"] for item in spec["boost"]]
    if spec.get("flow") is not None:
        params["flow"] = spec["flow"]
    # Which tasks a branch's ``use:`` copied in, and which version of each —
    # signed with the steps, so a run shows exactly what shipped.
    if spec.get("uses_copied"):
        params["uses"] = spec["uses_copied"]
    return params


def _validate_target_tags(raw: Any) -> list[str]:
    """Validate the optional top-level ``target_tags:`` list.

    Schema::

        target_tags:
          - os:linux         # auto-tag namespace
          - pkg:apt
          - prod             # plain user tag — also accepted

    The deploy endpoint enforces this as an OR filter — a host is
    eligible if it carries any one of these tags (in its merged
    ``Host.tags`` list). Returns an empty list when no filter is set.
    """
    if raw is None or raw == []:
        return []
    if not isinstance(raw, list):
        raise SpecError("'target_tags' must be a list of strings")
    if len(raw) > 32:
        raise SpecError("too many target_tags (max 32)")
    canonical: list[str] = []
    seen: set[str] = set()
    for index, tag in enumerate(raw):
        s = _as_str(tag, f"target_tags[{index}]", max_len=64).lower()
        if not s:
            raise SpecError(f"target_tags[{index}] is empty")
        if s in seen:
            continue  # dedupe silently
        seen.add(s)
        canonical.append(s)
    return canonical


def validate_params_override(spec: dict, override) -> str | None:
    """Validate a per-use params override against a parsed task spec.

    Playbook steps and automations may override action params without editing
    the shared definition. ``override`` maps stringified action indexes to
    ``{param: value}`` dicts; params must be declared (required or optional)
    by that action's registry entry and values must be scalars. Returns an
    error string, or None when valid.
    """
    if not override:
        return None
    if not isinstance(override, dict):
        return "params_override must be an object"
    actions = spec.get("actions") or []
    for key, params in override.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            return f"no action at index {key!r}"
        if not 0 <= index < len(actions):
            return f"no action at index {key}"
        if not isinstance(params, dict) or not all(
            isinstance(v, (str, int, float, bool)) for v in params.values()
        ):
            return f"params for action {index} must be a mapping of name to value"
        action_type = actions[index].get("type", "")
        entry = ACTION_REGISTRY.get(action_type)
        if entry is None:
            # Spec validation at definition save is the authority on action
            # types; don't second-guess it here.
            continue
        declared = set(entry["required"]) | set(entry["optional"])
        for name in params:
            if name not in declared:
                return f"{name!r} is not a parameter of {action_type}"
    return None


__all__ = ["_deploy_params", "_validate_cves", "_validate_platforms", "_validate_references", "_validate_target_tags", "validate_params_override"]
