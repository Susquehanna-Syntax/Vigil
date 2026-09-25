"""Container actions: lifecycle, image pull, recreate, update, compose, logs.

Moved out of executor.py (which was 2,150 lines). Helpers that tests patch by
name on ``executor`` (``_run``, ``_docker_inspect``, ``_recreate_container``,
``_validate_path``) are called through ``ex.`` so those patches keep applying;
executor re-exports every name defined here.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import collector
from .. import executor as ex
from ..config import AgentConfig
from ..executor import ActionOutput, _SAFE_IMAGE, _container_running, _validate_name


def _restart_container(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(
        params.get("container_name") or params.get("container_id", ""),
        "container name/id",
    )
    output = ex._run(["docker", "restart", name])
    collector.request_docker_recheck()
    return ActionOutput(output, {"running": _container_running(name)})


def _stop_container(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(
        params.get("container_name") or params.get("container_id", ""),
        "container name/id",
    )
    output = ex._run(["docker", "stop", name])
    collector.request_docker_recheck()
    return ActionOutput(output, {"running": _container_running(name)})


def _start_container(params: dict, _config: AgentConfig) -> str:
    name = _validate_name(
        params.get("container_name") or params.get("container_id", ""),
        "container name/id",
    )
    output = ex._run(["docker", "start", name])
    collector.request_docker_recheck()
    return ActionOutput(output, {"running": _container_running(name)})


def _pull_image(params: dict, _config: AgentConfig) -> str:
    image = params.get("image", "")
    if not _SAFE_IMAGE.match(image):
        raise ValueError(f"Invalid image name: {image!r}")
    output = ex._run(["docker", "pull", image], timeout=600)
    collector.request_docker_recheck()
    image_id = ex._run(["docker", "image", "inspect", "--format", "{{.Id}}", image]).strip()
    return ActionOutput(output, {"image_id": image_id})


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
    """Return the parsed ``docker inspect`` object for a container or image."""
    out = ex._run(["docker", "inspect", "--type", kind, ref])
    data = json.loads(out)
    if not data:
        raise RuntimeError(f"docker inspect returned nothing for {ref!r}")
    return data[0]


def _recreate_run_args(spec: dict, old_image: dict, new_image_ref: str) -> list[str]:
    """Build the ``docker run`` argv that reproduces *spec* on a new image.

    Only configuration the *user* supplied at ``docker run`` time is carried
    over — env vars, command, and entrypoint are diffed against the original
    image's defaults so the new image's own defaults still apply (the same
    approach watchtower uses). Exotic host configs (tmpfs, GPUs, log drivers,
    resource limits) are not reproduced; those setups belong in compose.
    """
    cfg = spec.get("Config") or {}
    host = spec.get("HostConfig") or {}
    img_cfg = old_image.get("Config") or {}

    args = ["docker", "run", "-d", "--name", (spec.get("Name") or "").lstrip("/")]

    restart = host.get("RestartPolicy") or {}
    policy = restart.get("Name") or ""
    if policy and policy != "no":
        retries = restart.get("MaximumRetryCount") or 0
        if policy == "on-failure" and retries:
            policy = f"{policy}:{retries}"
        args += ["--restart", policy]

    image_env = set(img_cfg.get("Env") or [])
    for env in cfg.get("Env") or []:
        if env not in image_env:
            args += ["-e", env]

    image_labels = img_cfg.get("Labels") or {}
    for label, value in (cfg.get("Labels") or {}).items():
        if image_labels.get(label) != value:
            args += ["--label", f"{label}={value}"]

    network = host.get("NetworkMode") or "default"
    if network not in ("default", "bridge"):
        args += ["--network", network]

    if host.get("PublishAllPorts"):
        args.append("-P")
    for port, bindings in (host.get("PortBindings") or {}).items():
        for binding in bindings or [{}]:
            host_ip = binding.get("HostIp") or ""
            host_port = binding.get("HostPort") or ""
            if host_ip:
                args += ["-p", f"{host_ip}:{host_port}:{port}"]
            elif host_port:
                args += ["-p", f"{host_port}:{port}"]
            else:
                args += ["-p", port]

    for mount in spec.get("Mounts") or []:
        source = mount.get("Source") if mount.get("Type") == "bind" else mount.get("Name")
        if not source:
            continue
        volume = f"{source}:{mount.get('Destination')}"
        if not mount.get("RW", True):
            volume += ":ro"
        args += ["-v", volume]

    if host.get("Privileged"):
        args.append("--privileged")
    for cap in host.get("CapAdd") or []:
        args += ["--cap-add", cap]
    for cap in host.get("CapDrop") or []:
        args += ["--cap-drop", cap]
    for device in host.get("Devices") or []:
        on_host = device.get("PathOnHost")
        if on_host:
            args += ["--device", f"{on_host}:{device.get('PathInContainer') or on_host}"]
    for extra_host in host.get("ExtraHosts") or []:
        args += ["--add-host", extra_host]
    if cfg.get("User"):
        args += ["--user", cfg["User"]]

    trailing: list[str] = []
    entrypoint = cfg.get("Entrypoint")
    if isinstance(entrypoint, str):
        entrypoint = [entrypoint]
    if entrypoint and entrypoint != (img_cfg.get("Entrypoint") or None):
        # --entrypoint takes a single executable; the rest of the override,
        # plus the command, must be restated as trailing args.
        args += ["--entrypoint", entrypoint[0]]
        trailing += entrypoint[1:]
        trailing += cfg.get("Cmd") or []
    else:
        command = cfg.get("Cmd")
        if isinstance(command, str):
            command = [command]
        if command and command != (img_cfg.get("Cmd") or None):
            trailing += command

    args.append(new_image_ref)
    args += [str(part) for part in trailing]
    return args


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
    run_args = _recreate_run_args(spec, old_image, image_ref)

    backup = f"{name}.vigil-old"
    try:
        ex._run(["docker", "rm", "-f", backup])  # clear stale backup from a failed run
    except RuntimeError:
        pass

    ex._run(["docker", "stop", name])
    ex._run(["docker", "rename", name, backup])
    try:
        ex._run(run_args, timeout=300)
    except Exception as exc:
        try:
            ex._run(["docker", "rm", "-f", name])  # half-created replacement, if any
        except RuntimeError:
            pass
        try:
            ex._run(["docker", "rename", backup, name])
            ex._run(["docker", "start", name])
            rollback = "original container restored"
        except RuntimeError as rb_exc:
            rollback = f"ROLLBACK FAILED, backup container is {backup!r}: {rb_exc}"
        raise RuntimeError(f"Recreate failed ({rollback}): {exc}") from exc
    ex._run(["docker", "rm", backup])
    collector.request_docker_recheck()

    new_image_id = ex._run(["docker", "inspect", "--format", "{{.Image}}", name])
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

        cmd = ["docker", "compose", *dir_args, "-f", compose_file]
        ex._run(cmd + ["pull", service], timeout=600)
        ex._run(cmd + ["up", "-d", "--no-deps", service], timeout=300)
        via = "compose"
    else:
        ex._run(["docker", "pull", image_ref], timeout=600)
        pulled_id = ex._run(["docker", "image", "inspect", "--format", "{{.Id}}", image_ref])
        if pulled_id and pulled_id == old_image_id:
            # Already on the newest build: recreating would only restart a
            # running container for nothing.
            via = "pull"
        else:
            ex._recreate_container({"container_name": name, "image": image_ref}, _config)
            via = "recreate"

    collector.request_docker_recheck()

    new_image_id = ex._run(["docker", "inspect", "--format", "{{.Image}}", name])
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
    output = ex._run(["docker", "rm", "-f", name])
    collector.request_docker_recheck()
    return ActionOutput(output, {"removed": True})


def _docker_compose_up(params: dict, _config: AgentConfig) -> str:
    compose_file = params.get("compose_file", "")
    path = ex._validate_path(compose_file, "compose_file")
    if not path.is_file():
        raise ValueError(f"Compose file not found: {compose_file}")

    cmd = ["docker", "compose", "-f", str(path), "up", "-d"]

    services = params.get("services", "")
    if services:
        if isinstance(services, str):
            services = [s.strip() for s in services.split(",") if s.strip()]
        for svc in services:
            _validate_name(svc, "service name")
            cmd.append(svc)

    output = ex._run(cmd, timeout=300)
    collector.request_docker_recheck()
    return ActionOutput(output, {"compose_file": str(path)})


def _docker_compose_down(params: dict, _config: AgentConfig) -> str:
    compose_file = params.get("compose_file", "")
    path = ex._validate_path(compose_file, "compose_file")
    if not path.is_file():
        raise ValueError(f"Compose file not found: {compose_file}")
    output = ex._run(["docker", "compose", "-f", str(path), "down"], timeout=120)
    collector.request_docker_recheck()
    return ActionOutput(output, {"compose_file": str(path)})


def _clear_docker_logs(params: dict, _config: AgentConfig) -> str:
    container = params.get("container_name", "")
    if not container:
        return ActionOutput("No container specified", {"truncated": False})
    _validate_name(container, "container name")
    log_path = ex._run(
        ["docker", "inspect", "--format={{.LogPath}}", container]
    )
    if log_path and Path(log_path).exists():
        Path(log_path).write_text("")
        return ActionOutput(f"Truncated log for {container}", {"truncated": True})
    return ActionOutput("No log file found", {"truncated": False})
