"""apps.tasks.spec: shared helpers (params), moved unchanged."""
from ._common import *  # noqa: F401,F403


def _validate_inputs(raw_inputs: Any) -> list[dict[str, Any]]:
    """Validate top-level ``inputs:`` schema. Returns canonical list."""
    if raw_inputs is None:
        return []
    if not isinstance(raw_inputs, list):
        raise SpecError("'inputs' must be a list")
    if len(raw_inputs) > 16:
        raise SpecError("too many inputs (max 16)")

    seen_ids: set[str] = set()
    canonical: list[dict[str, Any]] = []
    for index, entry in enumerate(raw_inputs):
        if not isinstance(entry, dict):
            raise SpecError(f"input #{index + 1} must be a mapping")

        input_id = _as_str(entry.get("id"), f"inputs[{index}].id", max_len=60)
        if not input_id:
            raise SpecError(f"input #{index + 1} missing 'id'")
        if not _INPUT_ID_PATTERN.match(input_id):
            raise SpecError(f"input id {input_id!r} must match [A-Za-z_][A-Za-z0-9_]*")
        if input_id in seen_ids:
            raise SpecError(f"duplicate input id {input_id!r}")
        seen_ids.add(input_id)

        input_type = _as_str(entry.get("type") or "text", f"inputs[{index}].type", max_len=16).lower()
        if input_type not in _INPUT_TYPES:
            raise SpecError(
                f"input {input_id!r}: unknown type {input_type!r} — "
                f"must be one of {', '.join(sorted(_INPUT_TYPES))}"
            )

        label = _as_str(entry.get("label") or input_id, f"inputs[{index}].label", max_len=120)
        description = _as_str(entry.get("description"), f"inputs[{index}].description", max_len=255)

        choices: list[dict[str, str]] = []
        if input_type == "choice":
            raw_choices = entry.get("choices") or []
            if not isinstance(raw_choices, list) or not raw_choices:
                raise SpecError(f"input {input_id!r}: 'choices' must be a non-empty list")
            if len(raw_choices) > 32:
                raise SpecError(f"input {input_id!r}: too many choices (max 32)")
            for ci, choice in enumerate(raw_choices):
                if not isinstance(choice, dict):
                    raise SpecError(f"input {input_id!r}: choice #{ci + 1} must be a mapping")
                cv = _as_str(choice.get("value"), f"inputs[{index}].choices[{ci}].value", max_len=120)
                if not cv:
                    raise SpecError(f"input {input_id!r}: choice #{ci + 1} missing 'value'")
                cl = _as_str(choice.get("label") or cv, f"inputs[{index}].choices[{ci}].label", max_len=120)
                choices.append({"value": cv, "label": cl})

        default = entry.get("default")
        if input_type == "boolean":
            default = bool(default) if default is not None else False
        elif input_type == "number":
            if default is None:
                default = 0
            elif not isinstance(default, (int, float)) or isinstance(default, bool):
                raise SpecError(f"input {input_id!r}: 'default' must be a number")
        elif input_type == "choice":
            valid_values = {c["value"] for c in choices}
            if default is None:
                default = choices[0]["value"]
            else:
                default = _as_str(default, f"inputs[{index}].default", max_len=120)
                if default not in valid_values:
                    raise SpecError(f"input {input_id!r}: default {default!r} not in choices")
        else:  # text
            default = _as_str(default, f"inputs[{index}].default", max_len=500) if default is not None else ""

        canonical.append({
            "id": input_id,
            "type": input_type,
            "label": label,
            "description": description,
            "choices": choices,
            "default": default,
            "required": bool(entry.get("required", input_type != "boolean")),
        })

    return canonical


def _validate_tag_param(raw: Any, position: int, action_type: str) -> None:
    """Check the ``tags`` param of an add_tag/remove_tag action.

    Comma-separated, not a list: every param value has to be a primitive so
    the signed payload stays flat (see the primitive check in
    parse_and_validate).

    A value containing ``{{ inputs.x }}`` cannot be judged here — it is
    resolved per deploy. The server re-checks the reserved prefix when it
    actually applies the tags, which is what covers that case.
    """
    if not isinstance(raw, str):
        raise SpecError(
            f"action #{position} ({action_type}): 'tags' must be a "
            f"comma-separated string, not {type(raw).__name__}")
    names = [t.strip() for t in raw.split(",") if t.strip()]
    if not names:
        raise SpecError(
            f"action #{position} ({action_type}): 'tags' names no tags")
    for name in names:
        if name.startswith("agent:"):
            raise SpecError(
                f"action #{position} ({action_type}): the 'agent:' tag "
                f"namespace is reserved for tags an agent advertises about "
                f"itself")
        if len(name) > 40:
            raise SpecError(
                f"action #{position} ({action_type}): tag {name!r} is longer "
                f"than 40 characters")


def _validate_script_params(params: dict[str, Any], position: int) -> None:
    has_name = "script_name" in params
    has_body = "script" in params
    if has_name and has_body:
        raise SpecError(
            f"action #{position} (execute_script): give script_name or script, not both"
        )
    if not has_name and not has_body:
        raise SpecError(
            f"action #{position} (execute_script): needs script_name or script"
        )
    if "shell" in params and not has_body:
        raise SpecError(
            f"action #{position} (execute_script): shell is only used with script"
        )
    if has_body:
        shell = params.get("shell")
        if shell not in _SCRIPT_SHELLS:
            raise SpecError(
                f"action #{position} (execute_script): shell must be one of "
                f"{', '.join(_SCRIPT_SHELLS)}"
            )
        body = params["script"]
        if not isinstance(body, str) or not body:
            raise SpecError(
                f"action #{position} (execute_script): script must be a "
                f"non-empty string"
            )
        if len(body) > _SCRIPT_MAX_LEN:
            raise SpecError(
                f"action #{position} (execute_script): script is limited to "
                f"{_SCRIPT_MAX_LEN} characters"
            )
        if "${{" in body:
            raise SpecError(
                f"action #{position} (execute_script): a script body takes "
                f"inputs as $VIGIL_INPUT_<ID> environment variables, not "
                f"${{{{ … }}}} markers"
            )


def _validate_custom_install_params(params: dict[str, Any],
                                    position: int) -> None:
    """Check `app_install_custom`'s params.

    Every rule here is applied again by the agent before it downloads anything:
    a value carrying a ``${{ … }}`` marker is resolved per deploy, and an
    installer from a URL the server never saw is the highest-risk thing an
    ``app_*`` action can do.
    """
    where = f"action #{position} (app_install_custom)"

    def _pending(value: Any) -> bool:
        # A non-string is judged only by the per-param type check: it resolves
        # to something the agent re-validates, so it is "pending" for the
        # string rules but must not stand in for an absent param.
        return isinstance(value, str) and "${{" in value

    def _text(name: str) -> Any:
        raw = params.get(name)
        if raw is not None and not isinstance(raw, str):
            raise SpecError(f"{where}: '{name}' must be a string")
        return raw

    url = _text("url")
    if url is None:
        return
    if _pending(url):
        # The URL is the only source of a kind the task did not name, and the
        # agent re-checks it before downloading, so an unresolved one is the
        # one case the rules below have nothing to go on.
        return
    if not url.startswith(_CUSTOM_URL_SCHEME):
        raise SpecError(
            f"{where}: 'url' must start with https:// — the agent will not "
            f"download an installer over a plain connection. Got {url!r}"
        )
    if len(url) > _CUSTOM_URL_MAX_LEN:
        raise SpecError(
            f"{where}: 'url' is limited to {_CUSTOM_URL_MAX_LEN} characters"
        )
    if any(ch.isspace() for ch in url):
        raise SpecError(
            f"{where}: 'url' must not contain whitespace. Got {url!r}"
        )

    sha = _text("sha256")
    if sha is not None and not _pending(sha) \
            and not _CUSTOM_SHA256_RE.fullmatch(sha.lower()):
        raise SpecError(
            f"{where}: 'sha256' must be 64 hexadecimal characters — the digest "
            f"the downloaded installer has to match. Got {sha!r}"
        )

    kind = _text("kind") if "kind" in params else None
    if kind is not None and not _pending(kind) and kind not in _CUSTOM_KINDS:
        raise SpecError(
            f"{where}: 'kind' must be one of {', '.join(_CUSTOM_KINDS)}, "
            f"got {kind!r}"
        )
    # The kind decides which command runs the file. A task may name it (it has
    # to for an extensionless download URL); otherwise the URL's extension does.
    # A named kind that contradicts the extension is refused rather than trusted.
    kind_from_url = _custom_kind_from_url(url)
    if kind is not None and not _pending(kind):
        if kind_from_url is not None and kind_from_url != kind:
            raise SpecError(
                f"{where}: 'kind' is {kind!r} but the URL ends in .{kind_from_url} "
                f"— the file is run by the command for its kind, so they must agree")
        known_kind = kind
    elif kind is None:
        if kind_from_url is None:
            raise SpecError(
                f"{where}: the URL has no .msi, .exe, .deb or .rpm extension — "
                f"say kind: msi, exe, deb or rpm")
        known_kind = kind_from_url
    else:
        known_kind = None   # resolved per deploy; the agent re-checks

    app = params.get("app")
    if app is not None and not _pending(app) \
            and (not isinstance(app, str) or not _APP_ID_RE.fullmatch(app)):
        raise SpecError(
            f"{where}: 'app' must be an inventory id — letters, digits and "
            f". _ + : @ / - ~ , no spaces and not starting with -. Got {app!r}"
        )

    args = _text("args")
    if not args or _pending(args):
        return
    # A switch list means something only to the one kind that is run directly.
    if known_kind == "exe":
        if not _CUSTOM_ARGS_RE.fullmatch(args):
            raise SpecError(
                f"{where}: 'args' must be a silent-install switch list — "
                f"letters, digits and space / = _ . : , @ + \" - with no shell "
                f"metacharacters. Got {args!r}"
            )
    elif known_kind is not None:
        raise SpecError(
            f"{where}: 'args' is only used with kind: exe — msiexec, apt and "
            "dnf take no extra arguments here"
        )


def _custom_kind_from_url(url: str) -> str | None:
    """The kind a URL's path extension implies, or None for nothing it implies.

    The query and fragment are cut first: a signed artifact URL routinely ends
    ``…/agent.deb?Signature=…``, and the extension is the file's, not the
    query string's.
    """
    tail = url.split("#", 1)[0].split("?", 1)[0].rsplit("/", 1)[-1]
    if "." not in tail:
        return None
    ext = tail.rsplit(".", 1)[-1].lower()
    return ext if ext in _CUSTOM_KINDS else None


def _validate_app_params(params: dict[str, Any], position: int,
                         action_type: str) -> None:
    """Check the ``app`` / ``source`` / ``version`` params of an ``app_*`` action.

    A value containing ``${{ … }}`` cannot be judged here — it is resolved per
    deploy. The agent re-checks every one of these rules before it runs a
    command, which is what covers that case.

    ``app_upgrade`` with no ``app`` is not a missing param: it means *every
    outdated app*, which is the whole point of the no-``app`` form.
    """
    where = f"action #{position} ({action_type})"

    def _pending(value: Any) -> bool:
        # A value carrying a ${{ … }} marker is resolved per deploy, so there
        # is nothing to check here.
        return not isinstance(value, str) or "${{" in value

    app = params.get("app")
    if (action_type == "app_uninstall" and params.get("source") == "registry"
            and isinstance(app, str) and not _pending(app)):
        # A registry Uninstall key name — only ever compared with registry rows
        # on the agent, never put on a command line — may contain spaces.
        if not _REGISTRY_KEY_RE.fullmatch(app):
            raise SpecError(f"{where}: 'app' must be a registry key name "
                            f"(no backslash or control characters). Got {app!r}")
    elif app is not None and not _pending(app) and not _APP_ID_RE.fullmatch(app):
        raise SpecError(
            f"{where}: 'app' must be an inventory id — letters, digits and "
            f". _ + : @ / - ~ , no spaces and not starting with -. Got {app!r}"
        )

    source = params.get("source")
    if source is not None and not _pending(source) \
            and source not in _APP_SOURCE_CHOICES:
        raise SpecError(
            f"{where}: 'source' must be one of "
            f"{', '.join(sorted(_APP_SOURCE_CHOICES))}, got {source!r}"
        )

    version = params.get("version")
    if action_type == "app_pin":
        unpin = params.get("unpin")
        # An unpin that carries a version is refused whatever that version
        # looks like, including a value still waiting to be resolved.
        if unpin is True:
            if version is not None:
                raise SpecError(f"{where}: unpin takes no version")
        elif unpin is not None and not _pending(unpin):
            raise SpecError(
                f"{where}: 'unpin' must be a boolean. Got {unpin!r}"
            )
    if version is None or _pending(version):
        return
    if not _APP_VERSION_RE.fullmatch(version):
        raise SpecError(
            f"{where}: 'version' must be letters, digits and "
            f". _ + : ~ - with no spaces. Got {version!r}"
        )
    if source in _APP_SOURCES_WITHOUT_VERSION:
        raise SpecError(
            f"{where}: version pinning is not supported for {source}"
        )


def _validate_stack_params(params: dict[str, Any], position: int, action_type: str) -> None:
    """``stack_deploy`` / ``stack_remove`` (M11): a compose project name, an
    absolute working directory with no '..', a compose file that passes the
    same checks the stack store applies, and an env ticket that is an id —
    never the secrets themselves."""
    from apps.stacks.validation import NAME_RE, StackError, validate_compose

    where = f"action #{position} ({action_type})"
    project = params.get("project")
    if not isinstance(project, str) or not NAME_RE.match(project):
        raise SpecError(f"{where}: 'project' must be a compose project name")
    if action_type == "stack_read":
        ticket = params.get("adopt_ticket")
        if not (isinstance(ticket, str) and _UUID_RE.match(ticket)):
            raise SpecError(f"{where}: 'adopt_ticket' must be a ticket id")
        return
    compose_file = params.get("compose_file")
    if compose_file is not None and not (isinstance(compose_file, str)
                                         and _COMPOSE_FILE_RE.match(compose_file)):
        raise SpecError(f"{where}: 'compose_file' must be a plain .yaml / .yml file name")
    workdir = params.get("working_dir")
    if (not isinstance(workdir, str) or not _STACK_DIR_RE.match(workdir)
            or ".." in workdir.split("/")):
        raise SpecError(f"{where}: 'working_dir' must be an absolute path without '..'")
    if action_type == "stack_deploy":
        try:
            validate_compose(str(params.get("compose") or ""))
        except StackError as exc:
            raise SpecError(f"{where}: {exc}") from exc
        ticket = params.get("env_ticket")
        if ticket not in (None, "") and not (isinstance(ticket, str) and _UUID_RE.match(ticket)):
            raise SpecError(f"{where}: 'env_ticket' must be a ticket id")
        # A Git stack's source folder: a one-time ticket, the sha256 the
        # archive must have (the agent refuses anything else), its commit.
        source = [params.get(k) for k in ("source_ticket", "source_sha256", "source_commit")]
        if any(v not in (None, "") for v in source):
            ticket, sha, commit = source
            if not (isinstance(ticket, str) and _UUID_RE.match(ticket)):
                raise SpecError(f"{where}: 'source_ticket' must be a ticket id")
            if not (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha)):
                raise SpecError(f"{where}: 'source_sha256' must be 64 hex characters")
            if not (isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit)):
                raise SpecError(f"{where}: 'source_commit' must be a commit id")
        if params.get("build") not in (None, True, False):
            raise SpecError(f"{where}: 'build' must be true or false")
    else:
        delete = params.get("delete_files")
        if delete is not None and not isinstance(delete, bool):
            raise SpecError(f"{where}: 'delete_files' must be true or false")


def _validate_prune_params(params: dict[str, Any], position: int) -> None:
    """``image_prune`` / ``volume_prune``: ``all`` is a true/false."""
    _validate_flag_param(params, "all", position)


def _validate_flag_param(params: dict[str, Any], key: str, position: int) -> None:
    """A true/false param: a bool, the string "true"/"false", or an input reference."""
    flag = params.get(key)
    if (flag is not None and not isinstance(flag, bool) and flag not in ("true", "false")
            and not (isinstance(flag, str) and "${{" in flag)):
        raise SpecError(f"action #{position}: '{key}' must be true or false")


def _validate_app_ensure_params(params: dict[str, Any], position: int) -> None:
    """Check ``app_ensure``: a state, and a version exactly when it is pinned.

    The app / source / version characters are the other ``app_*`` actions'
    rules. A pinned state on a source that cannot install a version is refused
    by those same rules; a version on any other state is refused here.
    """
    where = f"action #{position} (app_ensure)"
    state = params.get("state")
    pending = isinstance(state, str) and "${{" in state
    if not pending and state not in _APP_ENSURE_STATES:
        raise SpecError(f"{where}: 'state' must be one of "
                        f"{', '.join(sorted(_APP_ENSURE_STATES))}, got {state!r}")
    version = params.get("version")
    if not pending:
        if state == "pinned" and version in (None, ""):
            raise SpecError(f"{where}: a pinned app needs a 'version'")
        if state != "pinned" and version not in (None, ""):
            raise SpecError(f"{where}: only state: pinned takes a 'version'")
    _validate_app_params(params, position, "app_ensure")


__all__ = ["_custom_kind_from_url", "_validate_app_ensure_params", "_validate_app_params", "_validate_custom_install_params", "_validate_flag_param", "_validate_inputs", "_validate_prune_params", "_validate_script_params", "_validate_stack_params", "_validate_tag_param"]
