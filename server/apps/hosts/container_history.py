"""Record what container updates and rollbacks did (M11)."""

from __future__ import annotations


def record_container_task(task) -> None:
    """After a task completes: an update_container step that changed the image
    records the old one and ends any rollback; a container_rollback step marks
    the container rolled back."""
    from .models import ContainerImageHistory, ContainerRollback, DockerContainer

    results = {s.get("id"): s for s in (task.result_data or {}).get("steps") or []
               if isinstance(s, dict)}
    for step in (task.params or {}).get("steps") or []:
        if not isinstance(step, dict):
            continue
        params = step.get("params") or {}
        name = str(params.get("container_name") or "")[:200]
        result = (results.get(step.get("id")) or {}).get("result") or {}
        if not name or (results.get(step.get("id")) or {}).get("status") not in ("ok", "completed"):
            continue
        if step.get("action") == "update_container":
            old = str(result.get("old_image_id") or "")[:80]
            if result.get("updated") and old:
                snap = DockerContainer.objects.filter(host=task.host, name=name).first()
                ContainerImageHistory.objects.create(
                    host=task.host, container_name=name, image_id=old,
                    image_ref=(snap.image if snap else "")[:255],
                    image_digest=(snap.image_digest if snap and snap.image_id == old else "")[:300])
            ContainerRollback.objects.filter(host=task.host, container_name=name).delete()
        elif step.get("action") == "container_rollback" and result.get("rolled_back"):
            ContainerRollback.objects.update_or_create(
                host=task.host, container_name=name,
                defaults={"image": str(result.get("image") or params.get("image") or "")[:300]})
