"""apps.tasks.spec: parse views (split from the old single module, unchanged)."""
from ._common import *  # noqa: F401,F403
from ._params import *  # noqa: F401,F403
from ._flow import *  # noqa: F401,F403
from ._inputs import *  # noqa: F401,F403
from ._detect import *  # noqa: F401,F403


def parse_and_validate(yaml_source: str) -> dict[str, Any]:
    """Parse YAML, validate structure, return a canonical ``parsed_spec`` dict.

    The returned dict is stable: it always contains ``name``, ``description``,
    ``relevance``, ``risk``, ``actions`` (list of ``{id, type, params}``), and
    ``derived_risk`` (the max risk across all actions). The caller should
    store this alongside the raw YAML source.
    """
    if not yaml_source or not yaml_source.strip():
        raise SpecError("YAML is empty")

    try:
        raw = yaml.safe_load(yaml_source)
    except yaml.YAMLError as exc:
        raise SpecError(f"Invalid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise SpecError("Top-level YAML must be a mapping")

    name = _as_str(raw.get("name"), "name", max_len=120)
    if not name:
        raise SpecError("'name' is required")

    # ``author`` and ``created`` are optional locally but auto-injected by the
    # "Submit to Community" flow so every YAML that lands on the community
    # repo is self-describing (who wrote it, when). When present, ``created``
    # must be an ISO-8601 calendar date (YYYY-MM-DD); ``author`` is any short
    # string. Both flow through to ``parsed_spec`` so the UI can render them.
    author = _as_str(raw.get("author"), "author", max_len=80)
    # PyYAML auto-parses ISO dates into ``datetime.date`` objects; accept
    # both forms so authors can write either ``created: 2026-05-15`` (parsed
    # as a date) or ``created: "2026-05-15"`` (parsed as a string).
    raw_created = raw.get("created")
    if hasattr(raw_created, "isoformat") and not isinstance(raw_created, str):
        raw_created = raw_created.isoformat()
    created = _as_str(raw_created, "created", max_len=10)
    if created and not _ISO_DATE_PATTERN.match(created):
        raise SpecError("'created' must be an ISO-8601 date (YYYY-MM-DD)")

    # ``uid`` is this task's identity in the community catalog, independent of
    # its name and its filename. A playbook references its steps by uid, which
    # is what lets the reference survive a rename and stops two tasks that
    # happen to share a name from being confused for each other. Optional:
    # everything written before uids existed has none, and a task that is never
    # shared never needs one.
    from vigil.contentyaml import ContentYamlError, parse_uid

    try:
        uid = parse_uid(raw, "task")
    except ContentYamlError as exc:
        raise SpecError(str(exc)) from exc

    # Community-oriented metadata. None of it affects execution or risk; it
    # exists so the Community tab and the vuln views can cross-reference a
    # task with the advisories and platforms it targets.
    cves = _validate_cves(raw.get("cves"))
    references = _validate_references(raw.get("references"))
    platforms = _validate_platforms(raw.get("platforms"))

    risk = _as_str(raw.get("risk") or "standard", "risk", max_len=16).lower()
    if risk not in _VALID_RISK:
        raise SpecError(f"'risk' must be one of: {', '.join(sorted(_VALID_RISK))}")

    declared_inputs = _validate_inputs(raw.get("inputs"))
    declared_input_ids = {inp["id"] for inp in declared_inputs}

    actions_raw = raw.get("actions")
    if not isinstance(actions_raw, list) or not actions_raw:
        raise SpecError("'actions' must be a non-empty list")

    # Flatten if/then/else branches and use: references into an ordered list
    # of leaf steps; the flow tree is validated and returned with the parsed
    # spec.
    flat_actions, flow, uses = _flatten_actions(actions_raw)

    parsed_actions: list[dict[str, Any]] = []
    derived_risk_level = 0
    seen_ids: set[str] = set()
    warnings: list[str] = []
    description = _as_str(raw.get("description"), "description", max_len=2000)
    relevance = _as_str(raw.get("relevance"), "relevance", max_len=255)
    if relevance:
        # Free-text `relevance:` is deprecated — it is folded into the
        # description so it keeps rendering, and the parsed spec reports it
        # empty so nothing new writes the field. The no-duplicate guard keeps
        # re-parsing a folded task from appending the line a second time.
        warnings.append(
            "relevance: is free text and is now part of the description — "
            "use relevant: to decide where a task applies"
        )
        suffix = f"\n\nRelevant to: {relevance}"
        if not description.endswith(suffix):
            description = (description + suffix) if description else suffix.lstrip("\n")
        relevance = ""

    # Branch conditions are validated here, once every step's type is known,
    # against the steps that come earlier in document order. A ``use:`` node
    # contributes no condition and no leaf — its steps are spliced in at
    # deploy, so its spliced-in branch conditions are checked in the composite.
    branch_conditions: dict[str, str] = {}

    def _validate_branch_nodes(nodes: list) -> None:
        for node in nodes:
            if "id" in node:
                branch_conditions[node["id"]] = node["if"]
                _validate_branch_nodes(node["then"])
                _validate_branch_nodes(node["else"])

    if flow is not None:
        _validate_branch_nodes(flow)
    # Raw step mapping → its parsed id, filled in by the loop below, so the
    # flow can name steps by id (the steps themselves are signed once, in
    # params["steps"], with their inputs resolved).
    leaf_ids: dict[int, str] = {}

    for index, entry in enumerate(flat_actions):
        if not isinstance(entry, dict):
            raise SpecError(f"action #{index + 1} must be a mapping")

        action_type = _as_str(entry.get("type"), f"actions[{index}].type", max_len=64)
        if not action_type:
            raise SpecError(f"action #{index + 1} missing 'type'")
        action_type = LEGACY_ACTION_ALIASES.get(action_type, action_type)
        entry["type"] = action_type
        if action_type not in ACTION_REGISTRY:
            raise SpecError(
                f"action #{index + 1}: unknown type {action_type!r} — "
                f"must be one of {', '.join(sorted(ACTION_REGISTRY))}"
            )
        if action_type == "playbook" and entry.get("branch") is not None:
            raise SpecError(
                f"action #{index + 1}: type: playbook is not supported inside "
                f"branches (branch {entry['branch']})"
            )

        action_id = _as_str(entry.get("id") or f"step{index + 1}", f"actions[{index}].id", max_len=60)
        if action_id in seen_ids:
            raise SpecError(f"duplicate action id {action_id!r}")
        seen_ids.add(action_id)
        leaf_ids[id(entry)] = action_id

        # Steps before this one — the only ones a reference may name.
        earlier = {a["id"]: a["type"] for a in parsed_actions}

        # A branch whose first leaf is this step: validate its condition
        # against the steps earlier in document order, before this step's
        # own id is added.
        branch_path = entry.get("branch") or ""
        # Outermost first: a nested branch's first leaf is also the first
        # leaf of every branch around it that has not been checked yet.
        for bid in [seg for seg in branch_path.split(".") if seg in branch_conditions]:
            cond = branch_conditions.pop(bid)
            where = f"branch {bid}"
            try:
                tree = expression_parse(cond)
            except ExprError as exc:
                raise SpecError(f"{where}: if expression rejected: {exc}") from exc
            for step_id, field in sorted(referenced_steps(tree), key=str):
                _check_step_ref(step_id, field, earlier, where)
            _check_ordering_types(cond, tree, earlier, where)
            _check_status_values(tree, where)

        params = entry.get("params") or {}
        if not isinstance(params, dict):
            raise SpecError(f"action #{index + 1}: 'params' must be a mapping")

        spec = ACTION_REGISTRY[action_type]
        for required in spec["required"]:
            if required not in params:
                raise SpecError(
                    f"action #{index + 1} ({action_type}) missing required param {required!r}"
                )

        if action_type in ("add_tag", "remove_tag"):
            _validate_tag_param(params.get("tags", ""), index + 1, action_type)
        elif action_type == "playbook":
            params["inputs"] = _validate_action_inputs(params.get("inputs"))
        elif action_type in ("app_install", "app_upgrade", "app_uninstall",
                             "app_pin"):
            _validate_app_params(params, index + 1, action_type)
        elif action_type in ("stack_deploy", "stack_remove", "stack_read"):
            _validate_stack_params(params, index + 1, action_type)
        elif action_type == "app_ensure":
            _validate_app_ensure_params(params, index + 1)
        elif action_type in ("image_prune", "volume_prune"):
            _validate_prune_params(params, index + 1)
        elif action_type == "docker_compose_up":
            _validate_flag_param(params, "build", index + 1)
        elif action_type == "app_install_custom":
            _validate_custom_install_params(params, index + 1)
        elif action_type == "execute_script":
            _validate_script_params(params, index + 1)
        elif action_type == "hunt_file" and not (
                params.get("name") or params.get("sha256")):
            raise SpecError(
                f"action #{index + 1} (hunt_file): needs name or sha256 — "
                "it cannot search without at least one of the two"
            )
        elif action_type == "hunt_process" and not (
                params.get("name") or params.get("cmdline") or params.get("user")):
            raise SpecError(
                f"action #{index + 1} (hunt_process): needs name, cmdline or user — "
                "it cannot search without at least one of the three"
            )
        elif action_type == "hunt_port" and not (
                params.get("port") or params.get("process")):
            raise SpecError(
                f"action #{index + 1} (hunt_port): needs port or process — "
                "it cannot search without at least one of the two"
            )
        elif action_type == "hunt_content":
            _return_raw = params.get("return")
            if _return_raw is not None and str(_return_raw).lower() not in (
                    "match", "lines", "text"):
                raise SpecError(
                    f"action #{index + 1} (hunt_content): return must be one "
                    f"of match, lines or text, got {str(_return_raw)!r}"
                )
            try:
                re.compile(str(params["pattern"]))
            except re.error as exc:
                raise SpecError(
                    f"action #{index + 1} (hunt_content): invalid pattern — "
                    f"{exc}"
                ) from exc

        allowed = set(spec["required"]) | set(spec["optional"])
        extra = set(params) - allowed
        if extra:
            raise SpecError(
                f"action #{index + 1} ({action_type}) has unknown params: {sorted(extra)}"
            )

        # All param values must be primitives for safe signing. A playbook
        # action's `inputs` is the one structured param: it is consumed
        # server-side when the playbook is inlined and never signed as a step.
        for pk, pv in params.items():
            structured = action_type == "playbook" and pk == "inputs"
            if not structured and not isinstance(pv, (str, int, float, bool)):
                raise SpecError(
                    f"action #{index + 1}: param {pk!r} must be a primitive value"
                )
            # If the value references ${{ inputs.x }}, the input must exist.
            # The inline script body is an exception: bare braces are literal
            # shell/PowerShell text, so its contents are never ref-checked.
            if not (action_type == "execute_script" and pk == "script"):
                _check_variable_refs(
                    pv, declared_input_ids, f"action #{index + 1} param {pk!r}",
                    warnings, earlier,
                )

        # Optional `when:` predicate. Validated for syntactic safety
        # here; the agent evaluates it at runtime against its own
        # platform context. Bad syntax fails the save, not the deploy.
        # A named end state the author gives this step ("Patched", "Healthy"):
        # a host whose run last succeeded on a labelled step ends there.
        outcome = _as_str(entry.get("outcome"), f"actions[{index}].outcome", max_len=40)

        when_raw = entry.get("when")
        when_expr = ""
        if when_raw is not None:
            when_expr = _as_str(when_raw, f"actions[{index}].when", max_len=500)
            if when_expr:
                try:
                    expression_parse(when_expr)
                except ExprError as exc:
                    raise SpecError(
                        f"action #{index + 1}: when expression rejected: {exc}"
                    ) from exc
                # An inputs.* reference that was never declared evaluates to
                # None at runtime, and None == "yes" is simply false — so the
                # step would skip silently on every run, forever. Fail the
                # save instead.
                unknown = referenced_inputs(when_expr) - declared_input_ids
                if unknown:
                    raise SpecError(
                        f"action #{index + 1}: when expression references "
                        f"undeclared input(s) {sorted(unknown)} — declare them "
                        f"under inputs:, or the step will silently never run"
                    )
                try:
                    step_refs = referenced_steps(when_expr)
                except ExprError as exc:
                    raise SpecError(
                        f"action #{index + 1}: when expression rejected: {exc}"
                    ) from exc
                for step_id, field in sorted(step_refs, key=str):
                    _check_step_ref(step_id, field, earlier, f"action #{index + 1} when")
                when_tree = expression_parse(when_expr)
                _check_ordering_types(when_expr, when_tree, earlier, f"action #{index + 1} when")
                _check_status_values(when_tree, f"action #{index + 1} when")

        # Optional per-step timeout, in seconds. The 1..3600 range mirrors
        # the agent's own validation — a limit the server accepts but the
        # agent refuses is just a confusing failure one hop later.
        timeout_raw = entry.get("timeout")
        step_timeout = None
        if timeout_raw is not None:
            if isinstance(timeout_raw, bool) or not isinstance(timeout_raw, int):
                raise SpecError(
                    f"action #{index + 1}: timeout must be a whole number of "
                    f"seconds")
            if not 1 <= timeout_raw <= _MAX_STEP_TIMEOUT:
                raise SpecError(
                    f"action #{index + 1}: timeout must be between 1 and "
                    f"{_MAX_STEP_TIMEOUT} seconds")
            step_timeout = timeout_raw

        # stays_open is a hunt-only param; it never reaches the agent.
        if action_type.startswith("hunt_") and params.get("stays_open") is not None:
            stays_open_seconds(
                params["stays_open"], f"action #{index + 1} ({action_type}) stays_open"
            )

        parsed_actions.append({
            "id": action_id,
            "type": action_type,
            "label": spec["label"],
            "params": params,
            "risk": spec["risk"],
            "when": when_expr,
            "timeout": step_timeout,
            "outcome": outcome or None,
            "outputs": sorted(action_outputs(action_type)),
            "branch": entry.get("branch"),
        })
        if action_type == "execute_script" and "script" in params:
            parsed_actions[-1]["script_sha256"] = script_hash(params["script"])

        derived_risk_level = max(derived_risk_level, _RISK_ORDER[spec["risk"]])

        # hunt_content with `return: text` carries matched text out of the
        # host, so the step itself is high risk regardless of what the rest
        # of the task declares.
        if action_type == "hunt_content" and (
                str(params.get("return") or "lines").lower() == "text"):
            derived_risk_level = max(derived_risk_level, _RISK_ORDER["high"])

    # Effective risk is max(declared risk, derived from actions,
    # derived from relevant: probes) — users cannot declare a lower risk
    # than the actions and probes actually warrant.
    relevant = _validate_relevant(raw.get("relevant"), declared_inputs)
    relevant_risk_level = 0
    if relevant is not None:
        def _probe_levels(node: dict) -> int:
            level = 0
            for entry in node["items"]:
                if "probe" in entry:
                    level = max(level, entry["risk"])
                else:
                    level = max(level, _probe_levels(entry))
            return level
        relevant_risk_level = _probe_levels(relevant)
    # Detection tasks (M10): a severity for the finding a match raises, and
    # boost: probes that never decide applicability but raise confidence when
    # they match too. Boost probes run on the host like relevant: probes, so
    # their risk counts the same way.
    severity = _validate_severity(raw.get("severity"))
    boost = _validate_boost(raw.get("boost"), declared_inputs)
    boost_risk_level = max((b["risk"] for b in boost), default=0)
    effective_risk_level = max(_RISK_ORDER[risk], derived_risk_level,
                               relevant_risk_level, boost_risk_level)
    effective_risk = next(k for k, v in _RISK_ORDER.items() if v == effective_risk_level)

    schedule = _validate_schedule(raw.get("schedule"))
    on_failure = _validate_on_failure(raw.get("on_failure"))
    success_criteria = _validate_success_criteria(raw.get("success_criteria"))

    # Optional `collect:` directive — turns a task into an inventory data
    # collector. Validated lightly here; the result handler reads it.
    raw_collect = raw.get("collect")
    collect: dict[str, Any] | None = None
    if raw_collect is not None:
        if not isinstance(raw_collect, dict):
            raise SpecError("'collect' must be a mapping")
        column = _as_str(raw_collect.get("column"), "collect.column", max_len=80)
        if not column:
            raise SpecError("collect.column is required")
        parse_mode = _as_str(
            raw_collect.get("parse") or "output_line_1", "collect.parse", max_len=40
        ).lower()
        if parse_mode not in {"output_line_1", "output_full", "output_trim"}:
            raise SpecError(
                "collect.parse must be one of: output_line_1, output_full, output_trim"
            )
        collect = {"column": column, "parse": parse_mode}

    target_tags = _validate_target_tags(raw.get("target_tags"))

    return {
        "name": name,
        "description": description,
        "relevance": relevance,
        "uid": uid,
        "author": author,
        "created": created,
        "cves": cves,
        "references": references,
        "platforms": platforms,
        "risk": effective_risk,
        "declared_risk": risk,
        "actions": parsed_actions,
        "uses": uses,
        "flow": _flow_by_id(flow, leaf_ids) if flow is not None else None,
        "inputs": declared_inputs,
        "relevant": relevant,
        "severity": severity,
        "boost": boost,
        "schedule": schedule,
        "on_failure": on_failure,
        "success_criteria": success_criteria,
        "collect": collect,
        "target_tags": target_tags,
        "warnings": warnings,
    }


def _validate_relevant(raw: Any, declared_inputs: list[dict[str, Any]]) -> Any:
    """Validate the optional top-level ``relevant:`` tree; return its
    normalised form (None when absent).

    Grammar: a *node* is a mapping of one or more of ``all`` / ``any`` /
    ``not`` to non-empty lists of *items* (several keys mean all of them
    must hold); an *item* is either a *probe* — a mapping with exactly one
    key, a registry action starting with ``hunt_``, whose value is the
    hunt's params — or a nested *node*.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SpecError("'relevant' must be a mapping")

    probe_count = 0
    probe_risk_level = 0

    def _probe(where: str, key: str, params: Any) -> int:
        """Validate one probe through the real action path; return its risk level."""
        nonlocal probe_count, probe_risk_level
        if not isinstance(params, dict):
            raise SpecError(f"{where}: probe params must be a mapping")
        probe_spec = {
            "name": "relevant probe",
            # Declared low so the result's risk is the hunt's own, not the
            # "standard" a spec without a risk line defaults to.
            "risk": "low",
            "inputs": declared_inputs,
            "actions": [{"id": "probe", "type": key, "params": params}],
        }
        try:
            result = parse_and_validate(yaml.safe_dump(probe_spec))
        except SpecError as exc:
            raise SpecError(f"{where}: {exc}") from exc
        probe_count += 1
        if probe_count > _MAX_RELEVANT_PROBES:
            raise SpecError(f"{where}: too many probes (max {_MAX_RELEVANT_PROBES})")
        level = _RISK_ORDER[result["risk"]]
        probe_risk_level = max(probe_risk_level, level)
        return level

    def _node(where: str, value: Any, depth: int) -> dict[str, Any]:
        if depth > _MAX_RELEVANT_DEPTH:
            raise SpecError(
                f"{where}: too deep — at most {_MAX_RELEVANT_DEPTH} levels of "
                f"all/any/not"
            )
        if not isinstance(value, dict):
            raise SpecError(f"{where}: must be a mapping of all/any/not to lists")
        if not value:
            raise SpecError(f"{where}: must name at least one of all, any, not")
        unknown = set(value) - set(_RELEVANT_OPS)
        if unknown:
            raise SpecError(f"{where}: unknown key {min(unknown)!r}")
        nodes = []
        for op in _RELEVANT_OPS:
            if op not in value:
                continue
            raw_items = value[op]
            if not isinstance(raw_items, list) or not raw_items:
                raise SpecError(f"{where}.{op}: must be a non-empty list")
            parsed_items = [
                _item(f"{where}.{op}[{i}]", item, depth)
                for i, item in enumerate(raw_items)
            ]
            nodes.append({"op": op, "items": parsed_items})
        return nodes[0] if len(nodes) == 1 else {"op": "all", "items": nodes}

    def _item(where: str, item: Any, depth: int) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise SpecError(f"{where}: item must be a mapping")
        if any(k in item for k in _RELEVANT_OPS):
            return _node(where, item, depth + 1)
        if len(item) != 1:
            raise SpecError(f"{where}: a probe has exactly one key — a hunt_ action")
        key = next(iter(item))
        if not key.startswith("hunt_") or key not in ACTION_REGISTRY:
            raise SpecError(f"{where}: unknown key {key!r}")
        level = _probe(f"{where}.{key}", key, item[key])
        return {"probe": {"id": f"relevant-{probe_count}", "type": key,
                          "params": item[key]}, "risk": level}

    return _node("relevant", raw, 1)


def _validate_severity(raw: Any) -> str:
    """The optional ``severity:`` of a detection task — what a match is worth."""
    if raw is None:
        return ""
    value = _as_str(raw, "severity", max_len=16).lower()
    if value not in _DETECTION_SEVERITIES:
        raise SpecError(f"'severity' must be one of: {', '.join(_DETECTION_SEVERITIES)}")
    return value


def _validate_boost(raw: Any, declared_inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The optional ``boost:`` list: hunt probes that do not decide whether a
    task applies, only how sure a match is. Same probe grammar as
    ``relevant:`` items (one hunt_ key each, no nesting); returns
    ``[{"probe": {"id": "boost-<n>", "type", "params"}, "risk": level}]``."""
    if raw is None:
        return []
    if not isinstance(raw, list) or not raw:
        raise SpecError("'boost' must be a non-empty list of hunt probes")
    if len(raw) > _MAX_BOOST_PROBES:
        raise SpecError(f"'boost' has too many probes (max {_MAX_BOOST_PROBES})")
    for n, item in enumerate(raw):
        if isinstance(item, dict) and any(k in item for k in _RELEVANT_OPS):
            raise SpecError(f"boost[{n}]: boost probes cannot be grouped with all/any/not")
    try:
        tree = _validate_relevant({"any": raw}, declared_inputs)
    except SpecError as exc:
        raise SpecError(str(exc).replace("relevant.any", "boost", 1)) from exc
    items = tree["items"]
    for n, item in enumerate(items, start=1):
        item["probe"]["id"] = f"boost-{n}"
    return items
