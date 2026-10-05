"""Container actions: lifecycle, image pull, recreate, update, compose, logs.

Moved out of executor.py (which was 2,150 lines). Helpers that tests patch by
name on ``executor`` (``_run``, ``_engine``, ``_docker_inspect``, ``_pull``,
``_image_id``, ``_container_image_id``, ``_recreate_container``,
``_validate_path``) are called through ``ex.`` so those patches keep applying;
executor re-exports every name defined here.

Single containers go through the Engine API (vigil_agent/engine.py, M11) —
the same protocol for Docker and Podman. Stacks go through the compose CLI
(``docker compose``, or ``podman compose`` where there is no docker CLI)
pointed at the same socket with ``DOCKER_HOST``.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import collector
from .. import executor as ex
from ..config import AgentConfig
from ..executor import ActionOutput, _SAFE_IMAGE, _container_running, _validate_name


def _engine():
    """The engine client every container action uses (tests patch ex._engine)."""
    return ex._engine()


def _lifecycle(params: dict, verb: str) -> ActionOutput:
    name = _validate_name(
        params.get("container_name") or params.get("container_id", ""),
        "container name/id",
    )
    # 304 = already in that state: the action's outcome holds either way.
    _engine().post(f"/containers/{name}/{verb}", ok=(204, 304),
                   query={"t": 10} if verb in ("stop", "restart") else None)
    collector.request_docker_recheck()
    return ActionOutput(f"{verb} {name}: done", {"running": _container_running(name)})


def _restart_container(params: dict, _config: AgentConfig) -> str:
    return _lifecycle(params, "restart")


def _stop_container(params: dict, _config: AgentConfig) -> str:
    return _lifecycle(params, "stop")


def _start_container(params: dict, _config: AgentConfig) -> str:
    return _lifecycle(params, "start")


def _split_ref(image: str) -> tuple[str, str]:
    """(fromImage, tag) for the Engine API's image create — a digest stays in
    the name, a tag is split off after the last colon past the last slash."""
    if "@" in image:
        return image, ""
    head, _, tail = image.rpartition(":")
    if head and "/" not in tail:
        return head, tail
    return image, "latest"


def registry_of(image: str) -> str:
    """The registry an image reference names — docker.io when it names none
    (the first path part is a registry only if it has a dot, a port or is
    localhost, which is how the engine itself reads it)."""
    first, sep, _rest = image.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        return first.lower()
    return "docker.io"


def _registry_auth(image: str) -> str | None:
    """X-Registry-Auth for *image*, when the server holds a login for its
    registry on this host; None to pull anonymously."""
    import base64

    from .. import client
    from ..config import load_config

    try:
        config = load_config()
    except Exception:  # noqa: BLE001 — no config, no credentials
        return None
    login = client.fetch_registry_auth(config, registry_of(image))
    if not login:
        return None
    blob = json.dumps({"username": login["username"], "password": login["password"],
                       "serveraddress": login.get("serveraddress") or registry_of(image)})
    return base64.urlsafe_b64encode(blob.encode()).decode()


def _pull(image: str, auth: str | None = None) -> str:
    """Pull *image* through the engine; returns its progress text. The engine
    answers 200 and puts a failure in the stream, so the stream is read."""
    repo, tag = _split_ref(image)
    if auth is None:
        auth = ex._registry_auth(image)
    headers = {"X-Registry-Auth": auth} if auth else None
    status, body, _ = _engine().raw("POST", "/images/create",
                                    query={"fromImage": repo, "tag": tag or None},
                                    headers=headers, timeout=600)
    lines = []
    for raw in body.decode("utf-8", "replace").splitlines():
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if event.get("error"):
            raise RuntimeError(f"pull {image}: {event['error']}")
        if event.get("status") and not event.get("progressDetail"):
            lines.append(f"{event.get('id', '')} {event['status']}".strip())
    if status != 200:
        raise RuntimeError(f"pull {image}: engine answered {status}")
    return "\n".join(lines[-20:])


def _image_id(ref: str) -> str:
    return str((_engine().get(f"/images/{ref}/json") or {}).get("Id") or "")


def _container_image_id(name: str) -> str:
    return str((_engine().get(f"/containers/{name}/json") or {}).get("Image") or "")


def _pull_image(params: dict, _config: AgentConfig) -> str:
    image = params.get("image", "")
    if not _SAFE_IMAGE.match(image):
        raise ValueError(f"Invalid image name: {image!r}")
    output = ex._pull(image)
    collector.request_docker_recheck()
    return ActionOutput(output, {"image_id": ex._image_id(image)})


def _check_docker_updates(_params: dict, _config: AgentConfig) -> str:
    """Force an immediate Docker Hub digest re-check.

    Runs the same check the agent performs on its ``docker_check_interval``
    schedule and returns a per-container summary. The recheck flag is also
    set so the main loop refreshes its cached metrics and ships them on the
    next check-in — firing or resolving outdated-image alerts within about
    a minute instead of waiting out the interval.
    """
    metrics = collector.collect_docker_updates()
    collector.request_docker_recheck()

    lines = []
    outdated = 0
    for metric in metrics:
        if metric.get("metric") != "image_outdated":
            continue
        labels = metric.get("labels") or {}
        if metric.get("value"):
            outdated += 1
            state = "OUTDATED"
        else:
            state = "up to date"
        lines.append(f"  {labels.get('container_name')}: {labels.get('image')} — {state}")

    if not lines:
        return ActionOutput(
            "No Docker Hub-tagged containers to check "
            "(Docker unavailable, nothing running, or only local/private images)",
            {"checked": 0, "outdated": 0},
        )
    header = f"Checked {len(lines)} container(s): {outdated} outdated"
    return ActionOutput(
        "\n".join([header, *lines]),
        {"checked": len(lines), "outdated": outdated},
    )


_COMPOSE_PROJECT_LABEL = "com.docker.compose.project"


def _docker_inspect(ref: str, *, kind: str = "container") -> dict:
    """The engine's inspect object for a container or an image."""
    path = f"/images/{ref}/json" if kind == "image" else f"/containers/{ref}/json"
    data = _engine().get(path)
    if not data:
        raise RuntimeError(f"inspect returned nothing for {ref!r}")
    return data


def _recreate_body(spec: dict, old_image: dict, new_image_ref: str) -> dict:
    """The Engine API create body that reproduces *spec* on a new image.

    Only configuration the *user* supplied is carried over — env vars,
    labels, command and entrypoint are diffed against the original image's
    defaults so the new image's own defaults still apply (the approach
    watchtower takes). Exotic host configs (tmpfs, GPUs, log drivers,
    resource limits) are not reproduced; those setups belong in compose.
    """
    cfg = spec.get("Config") or {}
    host = spec.get("HostConfig") or {}
    img_cfg = old_image.get("Config") or {}
    body: dict = {"Image": new_image_ref}

    image_env = set(img_cfg.get("Env") or [])
    env = [e for e in cfg.get("Env") or [] if e not in image_env]
    if env:
        body["Env"] = env
    image_labels = img_cfg.get("Labels") or {}
    labels = {k: v for k, v in (cfg.get("Labels") or {}).items() if image_labels.get(k) != v}
    if labels:
        body["Labels"] = labels
    if cfg.get("User"):
        body["User"] = cfg["User"]
    if cfg.get("ExposedPorts"):
        body["ExposedPorts"] = cfg["ExposedPorts"]

    entrypoint = cfg.get("Entrypoint")
    if isinstance(entrypoint, str):
        entrypoint = [entrypoint]
    command = cfg.get("Cmd")
    if isinstance(command, str):
        command = [command]
    if entrypoint and entrypoint != (img_cfg.get("Entrypoint") or None):
        body["Entrypoint"] = entrypoint
        body["Cmd"] = command or []
    elif command and command != (img_cfg.get("Cmd") or None):
        body["Cmd"] = command

    hc: dict = {}
    restart = host.get("RestartPolicy") or {}
    if (restart.get("Name") or "no") != "no":
        hc["RestartPolicy"] = {"Name": restart["Name"],
                               "MaximumRetryCount": restart.get("MaximumRetryCount") or 0}
    network = host.get("NetworkMode") or "default"
    if network not in ("default", "bridge"):
        hc["NetworkMode"] = network
    if host.get("PublishAllPorts"):
        hc["PublishAllPorts"] = True
    if host.get("PortBindings"):
        hc["PortBindings"] = host["PortBindings"]
    binds = []
    for mount in spec.get("Mounts") or []:
        source = mount.get("Source") if mount.get("Type") == "bind" else mount.get("Name")
        if not source:
            continue
        bind = f"{source}:{mount.get('Destination')}"
        if not mount.get("RW", True):
            bind += ":ro"
        binds.append(bind)
    if binds:
        hc["Binds"] = binds
    for key in ("Privileged", "CapAdd", "CapDrop", "Devices", "ExtraHosts"):
        if host.get(key):
            hc[key] = host[key]
    if hc:
        body["HostConfig"] = hc
    return body


def _recreate_container(params: dict, _config: AgentConfig) -> str:
    """Stop, remove, and re-run a container so it adopts a freshly pulled image.

    ``docker restart`` keeps a container on the image it was created from, so
    a pull + restart never applies an update. Applying one requires
    recreating the container: inspect the existing one, carry its
    user-supplied config (env overrides, ports, volumes, network, restart
    policy, capabilities) onto a new container on the target image, and roll
    the original back into place if the replacement fails to start.

    Compose-managed containers are refused — recreate those with
    ``docker_compose_up`` so compose stays authoritative over their config.
    """
    name = _validate_name(params.get("container_name", ""), "container name")
    spec = ex._docker_inspect(name)

    labels = (spec.get("Config") or {}).get("Labels") or {}
    if labels.get(_COMPOSE_PROJECT_LABEL):
        raise ValueError(
            f"Container {name!r} is managed by docker compose "
            f"(project {labels[_COMPOSE_PROJECT_LABEL]!r}) — "
            f"use docker_compose_up to recreate it"
        )

    image_ref = params.get("image") or (spec.get("Config") or {}).get("Image") or ""
    if not _SAFE_IMAGE.match(image_ref):
        raise ValueError(f"Invalid image name: {image_ref!r}")

    old_image_id = spec.get("Image") or ""
    # The old image is always inspectable while its container exists — docker
    # refuses to remove an image that a container still references.
    old_image = ex._docker_inspect(old_image_id or image_ref, kind="image")
    body = _recreate_body(spec, old_image, image_ref)
    eng = _engine()

    backup = f"{name}.vigil-old"
    try:
        eng.delete(f"/containers/{backup}", query={"force": "true"}, ok=(204, 404))
    except Exception:  # noqa: BLE001 — a stale backup from a failed run, if any
        pass

    eng.post(f"/containers/{name}/stop", ok=(204, 304))
    eng.post(f"/containers/{name}/rename", query={"name": backup})
    try:
        created = eng.post("/containers/create", query={"name": name}, body=body)
        eng.post(f"/containers/{created['Id']}/start", ok=(204, 304))
    except Exception as exc:
        try:
            eng.delete(f"/containers/{name}", query={"force": "true"}, ok=(204, 404))
        except Exception:  # noqa: BLE001 — half-created replacement, if any
            pass
        try:
            eng.post(f"/containers/{backup}/rename", query={"name": name})
            eng.post(f"/containers/{name}/start", ok=(204, 304))
            rollback = "original container restored"
        except Exception as rb_exc:  # noqa: BLE001
            rollback = f"ROLLBACK FAILED, backup container is {backup!r}: {rb_exc}"
        raise RuntimeError(f"Recreate failed ({rollback}): {exc}") from exc
    eng.delete(f"/containers/{backup}", ok=(204, 404))
    collector.request_docker_recheck()

    new_image_id = ex._container_image_id(name)
    changed = "image updated" if new_image_id != old_image_id else "image unchanged"
    return ActionOutput(
        f"Recreated {name} on {image_ref} ({changed})\n"
        f"  old image: {old_image_id[:19]}\n"
        f"  new image: {new_image_id[:19]}",
        {"updated": new_image_id != old_image_id, "old_image_id": old_image_id,
         "new_image_id": new_image_id},
    )


def _update_container(params: dict, _config: AgentConfig) -> str:
    """One action that takes only a container name and works out how the
    container is managed from the container itself, because the server cannot
    know either fact: a compose-managed container is pulled and re-upped
    through its own compose file, a standalone one is pulled and recreated."""
    name = _validate_name(params.get("container_name", ""), "container name")
    spec = ex._docker_inspect(name)
    cfg = spec.get("Config") or {}
    labels = cfg.get("Labels") or {}

    image_ref = cfg.get("Image") or ""
    if not _SAFE_IMAGE.match(image_ref):
        raise ValueError(f"Invalid image reference: {image_ref!r}")

    if "@sha256:" in image_ref:
        current_id = spec.get("Image") or ""
        return ActionOutput(
            f"{name} is pinned to {image_ref} — not updated "
            f"(pinning means the admin chose that exact build)",
            {
                "updated": False,
                "old_image_id": current_id,
                "new_image_id": current_id,
            },
        )

    old_image_id = spec.get("Image") or ""

    compose_file = ""
    if labels.get(_COMPOSE_PROJECT_LABEL):
        service = labels.get("com.docker.compose.service") or name
        _validate_name(service, "service name")

        config_files = labels.get("com.docker.compose.project.config_files") or ""
        compose_file = config_files.split(",")[0].strip() if config_files else ""
        if not compose_file:
            raise ValueError(
                f"Container {name!r} is managed by docker compose but its "
                f"compose labels are missing config files — use "
                f"docker_compose_up with an explicit compose_file"
            )
        compose_file = str(ex._validate_path(compose_file, "compose_file"))

        project_dir = labels.get("com.docker.compose.project.working_dir") or ""
        dir_args = []
        if project_dir:
            dir_args = ["--project-directory", str(ex._validate_path(project_dir, "project directory"))]
            _clear_rollback(project_dir)   # an update ends a rollback

        cmd = [*ex._compose_cmd(), *dir_args, "-f", compose_file]
        ex._run(cmd + ["pull", service], timeout=600, extra_env=ex._compose_env())
        ex._run(cmd + ["up", "-d", "--no-deps", service], timeout=300,
                extra_env=ex._compose_env())
        via = "compose"
    else:
        ex._pull(image_ref)
        pulled_id = ex._image_id(image_ref)
        if pulled_id and pulled_id == old_image_id:
            # Already on the newest build: recreating would only restart a
            # running container for nothing.
            via = "pull"
        else:
            ex._recreate_container({"container_name": name, "image": image_ref}, _config)
            via = "recreate"

    collector.request_docker_recheck()

    new_image_id = ex._container_image_id(name)
    changed = "image updated" if new_image_id != old_image_id else "already current"
    return ActionOutput(
        f"Updated {name} via {via} on {image_ref} ({changed})\n"
        f"  old image: {old_image_id[:19]}\n"
        f"  new image: {new_image_id[:19]}",
        {
            "updated": new_image_id != old_image_id,
            "old_image_id": old_image_id,
            "new_image_id": new_image_id,
        },
    )




def _remove_container(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(params.get("container_name", ""), "container name")
    _engine().delete(f"/containers/{name}", query={"force": "true"})
    collector.request_docker_recheck()
    return ActionOutput(f"removed {name}", {"removed": True})


def _docker_compose_up(params: dict, _config: AgentConfig) -> str:
    compose_file = params.get("compose_file", "")
    path = ex._validate_path(compose_file, "compose_file")
    if not path.is_file():
        raise ValueError(f"Compose file not found: {compose_file}")

    cmd = [*ex._compose_cmd(), "-f", str(path), "up", "-d"]

    services = params.get("services", "")
    if services:
        if isinstance(services, str):
            services = [s.strip() for s in services.split(",") if s.strip()]
        for svc in services:
            _validate_name(svc, "service name")
            cmd.append(svc)

    output = ex._run(cmd, timeout=300, extra_env=ex._compose_env())
    collector.request_docker_recheck()
    return ActionOutput(output, {"compose_file": str(path)})


def _docker_compose_down(params: dict, _config: AgentConfig) -> str:
    compose_file = params.get("compose_file", "")
    path = ex._validate_path(compose_file, "compose_file")
    if not path.is_file():
        raise ValueError(f"Compose file not found: {compose_file}")
    output = ex._run([*ex._compose_cmd(), "-f", str(path), "down"], timeout=120,
                     extra_env=ex._compose_env())
    collector.request_docker_recheck()
    return ActionOutput(output, {"compose_file": str(path)})


def _clear_docker_logs(params: dict, _config: AgentConfig) -> str:
    container = params.get("container_name", "")
    if not container:
        return ActionOutput("No container specified", {"truncated": False})
    _validate_name(container, "container name")
    log_path = str((_engine().get(f"/containers/{container}/json") or {}).get("LogPath") or "")
    if log_path and Path(log_path).exists():
        Path(log_path).write_text("")
        return ActionOutput(f"Truncated log for {container}", {"truncated": True})
    return ActionOutput("No log file found", {"truncated": False})


# ── Stacks (M11) ──────────────────────────────────────────────────────────────


def _stack_compose_args(project: str) -> list[str]:
    """``-p <project> --project-directory <dir> -f <file>…`` for a stack, read
    from the labels compose put on its containers — never a guessed path."""
    _validate_name(project, "stack (compose project)")
    containers = _engine().get(
        "/containers/json",
        query={"all": "1", "filters": json.dumps(
            {"label": [f"{_COMPOSE_PROJECT_LABEL}={project}"]})}) or []
    for c in containers:
        labels = c.get("Labels") or {}
        files = [f.strip() for f in str(labels.get("com.docker.compose.project.config_files")
                                         or "").split(",")
                 if f.strip() and not f.strip().endswith(ROLLBACK_OVERRIDE)]
        if not files:
            continue
        args = ["-p", project]
        workdir = labels.get("com.docker.compose.project.working_dir") or ""
        if workdir:
            args += ["--project-directory", str(ex._validate_path(workdir, "project directory"))]
        for f in files:
            args += ["-f", str(ex._validate_path(f, "compose file"))]
        return args
    raise ValueError(f"No containers of stack {project!r} carry compose file labels — "
                     f"deploy it with docker_compose_up and an explicit compose_file")


def _stack_restart(params: dict, _config: AgentConfig) -> str:
    project = str(params.get("project") or "")
    args = _stack_compose_args(project)
    output = ex._run([*ex._compose_cmd(), *args, "restart"], timeout=300,
                     extra_env=ex._compose_env())
    collector.request_docker_recheck()
    return ActionOutput(output or f"restarted stack {project}", {"project": project})


def _stack_update(params: dict, _config: AgentConfig) -> str:
    """Pull every image of the stack, then bring up whatever changed."""
    project = str(params.get("project") or "")
    args = _stack_compose_args(project)
    if "--project-directory" in args:
        _clear_rollback(args[args.index("--project-directory") + 1])   # an update ends a rollback
    cmd = [*ex._compose_cmd(), *args]
    pulled = ex._run(cmd + ["pull"], timeout=900, extra_env=ex._compose_env())
    output = ex._run(cmd + ["up", "-d"], timeout=600, extra_env=ex._compose_env())
    collector.request_docker_recheck()
    return ActionOutput("\n".join(filter(None, [pulled, output])) or f"updated stack {project}",
                        {"project": project})


# ── Rollback (M11) ────────────────────────────────────────────────────────────

#: A temporary pin: compose merges it over the stack's own file, which is
#: never touched. The next update deletes it.
ROLLBACK_OVERRIDE = "vigil-rollback.override.yaml"


def _clear_rollback(workdir: str) -> bool:
    """Remove a stack's rollback pin, if it has one; True when one was removed."""
    if not workdir:
        return False
    path = Path(str(ex._validate_path(workdir, "project directory"))) / ROLLBACK_OVERRIDE
    if path.exists():
        path.unlink()
        return True
    return False


def _container_rollback(params: dict, _config: AgentConfig) -> str:
    """Put a container back on an earlier image — by digest or image id.

    A compose container gets a pin in an override file next to its compose
    file and is brought up from both; a standalone one is recreated on the
    image. Either way the next update_container (or stack_update) clears it.
    """
    name = _validate_name(params.get("container_name", ""), "container name")
    image = str(params.get("image") or "")
    if not _SAFE_IMAGE.match(image):
        raise ValueError(f"Invalid image reference: {image!r}")
    spec = ex._docker_inspect(name)
    labels = (spec.get("Config") or {}).get("Labels") or {}
    project = labels.get(_COMPOSE_PROJECT_LABEL)
    if project:
        service = labels.get("com.docker.compose.service") or name
        _validate_name(service, "service name")
        workdir = labels.get("com.docker.compose.project.working_dir") or ""
        files = [f.strip() for f in str(labels.get("com.docker.compose.project.config_files") or "")
                 .split(",") if f.strip() and not f.strip().endswith(ROLLBACK_OVERRIDE)]
        if not files or not workdir:
            raise ValueError(f"Container {name!r} lacks the compose labels a rollback needs")
        wd = Path(str(ex._validate_path(workdir, "project directory")))
        override = wd / ROLLBACK_OVERRIDE
        override.write_text(f"# Written by Vigil: {service} rolled back. The next update removes it.\n"
                            f"services:\n  {service}:\n    image: {json.dumps(image)}\n")
        cmd = [*ex._compose_cmd(), "-p", project, "--project-directory", str(wd)]
        for f in files:
            cmd += ["-f", str(ex._validate_path(f, "compose file"))]
        cmd += ["-f", str(override), "up", "-d", "--no-deps", service]
        ex._run(cmd, timeout=300, extra_env=ex._compose_env())
        via = "compose override"
    else:
        ex._recreate_container({"container_name": name, "image": image}, _config)
        via = "recreate"
    collector.request_docker_recheck()
    return ActionOutput(f"Rolled {name} back to {image} via {via}",
                        {"rolled_back": True, "image": image})
