"""Point every tag relation at the rows seeded by 0012, without changing matching.

Still additive. The string fields remain authoritative and every matcher keeps
reading them; this only fills in the mirror so the two can be compared before
the switch-over. If this migration is wrong, nothing breaks yet — which is the
whole reason for doing it as a separate step.

Canonicalisation is by lowercase key, whitespace preserved, exactly as 0012
seeded it. A string with no matching row means 0012 missed a source; that is a
bug worth surfacing rather than silently creating the row here, so it is
counted and reported.
"""

from django.db import migrations

LIST_RELATIONS = [
    ("hosts", "Host", "tags", "tag_rows"),
    ("tasks", "PatchWave", "tags", "tag_rows"),
    ("baselines", "Baseline", "target_tags", "target_tag_rows"),
    ("automations", "Automation", "event_tags", "event_tag_rows"),
    ("automations", "Automation", "target_tags", "target_tag_rows"),
    ("reprovision", "InstallProfile", "completion_tags", "completion_tag_rows"),
]


def link(apps, schema_editor):
    Tag = apps.get_model("hosts", "Tag")
    by_key = {t.key: t for t in Tag.objects.all()}
    linked = 0
    orphans = set()

    for app, model, string_field, relation in LIST_RELATIONS:
        try:
            Model = apps.get_model(app, model)
        except LookupError:
            continue
        for row in Model.objects.all().iterator():
            names = [n for n in (getattr(row, string_field, None) or [])
                     if str(n).strip()]
            if not names:
                continue
            tags = []
            for name in names:
                tag = by_key.get(str(name).lower())
                if tag is None:
                    orphans.add(f"{model}.{string_field}: {name!r}")
                    continue
                tags.append(tag)
            if tags:
                getattr(row, relation).set(tags)
                linked += len(tags)

    # RebuildJob carries one tag as a plain string, not a list.
    try:
        RebuildJob = apps.get_model("reprovision", "RebuildJob")
    except LookupError:
        RebuildJob = None
    if RebuildJob is not None:
        for job in RebuildJob.objects.exclude(completion_tag="").iterator():
            tag = by_key.get(str(job.completion_tag).lower())
            if tag is None:
                orphans.add(f"RebuildJob.completion_tag: {job.completion_tag!r}")
                continue
            job.completion_tag_row = tag
            job.save(update_fields=["completion_tag_row"])
            linked += 1

    if orphans:
        print(f"\n  tags: {len(orphans)} string(s) had no row — 0012 missed a "
              f"source, or a row was deleted between migrations:")
        for o in sorted(orphans)[:20]:
            print(f"    {o}")
    print(f"\n  tags: linked {linked} reference(s) to rows. "
          f"String fields remain authoritative.\n")


def unlink(apps, schema_editor):
    """Clear the mirrors. The strings are untouched, so this is lossless."""
    for app, model, _string_field, relation in LIST_RELATIONS:
        try:
            Model = apps.get_model(app, model)
        except LookupError:
            continue
        for row in Model.objects.all().iterator():
            getattr(row, relation).clear()
    try:
        RebuildJob = apps.get_model("reprovision", "RebuildJob")
    except LookupError:
        return
    RebuildJob.objects.update(completion_tag_row=None)


class Migration(migrations.Migration):

    dependencies = [
        ("hosts", "0013_host_tag_rows"),
        ("tasks", "0016_patchwave_tag_rows"),
        ("baselines", "0006_baseline_target_tag_rows"),
        ("automations", "0008_automation_event_tag_rows_automation_target_tag_rows"),
        ("reprovision", "0004_installprofile_completion_tag_rows_and_more"),
    ]

    operations = [migrations.RunPython(link, unlink)]
