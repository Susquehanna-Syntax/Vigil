"""Create a Tag row for every tag string already in use, from all seven sources.

This migration is deliberately **additive**. It creates rows and changes
nothing about how matching works — the string fields stay exactly where they
are and every existing matcher keeps using them. Membership cannot shift,
because nothing yet reads these rows.

The switch-over (repointing each model at the rows, then dropping the string
fields) is a later migration, gated on the characterization suite in
apps/hosts/test_tag_semantics.py still passing.

Canonicalisation is by lowercasing only, never stripping — see the Tag model's
docstring. `Prod` and `prod` already matched each other so they collapse to one
row; `prod` and `prod ` never matched so they stay two. If two spellings did
not match before, they must not start matching now.
"""

from django.db import migrations

# Every place a tag string lives today. Missing one leaves a feature with no
# row to point at when the switch-over happens.
LIST_SOURCES = [
    ("hosts", "Host", "tags"),
    ("tasks", "PatchWave", "tags"),
    ("baselines", "Baseline", "target_tags"),
    ("automations", "Automation", "event_tags"),
    ("automations", "Automation", "target_tags"),
    ("reprovision", "InstallProfile", "completion_tags"),
]
#: RebuildJob.completion_tag is a singular CharField, not a list. It does not
#: match a `_tags` grep, and it is read at completion time rather than
#: snapshotted — so a rebuild in flight during this migration still needs its
#: tag to exist.
SCALAR_SOURCES = [
    ("reprovision", "RebuildJob", "completion_tag"),
]

RESERVED = ("os:", "os_family:", "pkg:", "arch:", "agent:")


def _kind_for(name):
    lowered = str(name).lower()
    if lowered.startswith("agent:"):
        return "agent"
    if lowered.startswith(("os:", "os_family:", "pkg:", "arch:")):
        return "auto"
    return "manual"


def collect_tag_names(apps):
    """Every distinct tag string in the database, with where it came from."""
    seen = {}   # canonical key -> {"names": set, "sources": set}

    def note(raw, source):
        if raw is None:
            return
        name = str(raw)
        if not name.strip():
            return          # a blank tag matches nothing; never worth a row
        key = name.lower()  # NOT stripped — see module docstring
        entry = seen.setdefault(key, {"names": set(), "sources": set()})
        entry["names"].add(name)
        entry["sources"].add(source)

    for app, model, field in LIST_SOURCES:
        try:
            Model = apps.get_model(app, model)
        except LookupError:
            continue
        for row in Model.objects.all().iterator():
            for raw in (getattr(row, field, None) or []):
                note(raw, f"{model}.{field}")

    for app, model, field in SCALAR_SOURCES:
        try:
            Model = apps.get_model(app, model)
        except LookupError:
            continue
        for row in Model.objects.all().iterator():
            note(getattr(row, field, None), f"{model}.{field}")

    return seen


def populate(apps, schema_editor):
    Tag = apps.get_model("hosts", "Tag")
    seen = collect_tag_names(apps)

    merged, near_duplicates = [], []
    for key, entry in sorted(seen.items()):
        names = sorted(entry["names"])
        # Prefer the spelling that reads best: the one with capitals if any,
        # else the first alphabetically. Display only — `key` is what matches.
        display = next((n for n in names if n != n.lower()), names[0])
        Tag.objects.get_or_create(
            key=key,
            defaults={"name": display, "kind": _kind_for(display)},
        )
        if len(names) > 1:
            merged.append((key, names))

    # Pairs that differ only by whitespace are kept apart on purpose. Surface
    # them so an operator can fix a genuine typo deliberately, rather than the
    # migration guessing and moving hosts between waves.
    keys = sorted(seen)
    for key in keys:
        stripped = key.strip()
        twins = [k for k in keys if k != key and k.strip() == stripped]
        if twins and key == min([key] + twins):
            near_duplicates.append(sorted([key] + twins))

    if merged:
        print(f"\n  tags: merged {len(merged)} case-variant group(s) "
              f"(these already matched each other):")
        for key, names in merged[:20]:
            print(f"    {key!r} ← {', '.join(repr(n) for n in names)}")
    if near_duplicates:
        print(f"\n  tags: kept {len(near_duplicates)} whitespace-variant group(s) "
              f"SEPARATE (these never matched, so merging them would change "
              f"what your waves target):")
        for group in near_duplicates[:20]:
            print(f"    {' | '.join(repr(g) for g in group)}")
        print("    → if one of these is a typo, fix it on the host and delete "
              "the stray tag.")
    print(f"\n  tags: {Tag.objects.count()} row(s) now exist.\n")


def unpopulate(apps, schema_editor):
    """Reverse is a clean delete: nothing reads these rows yet, and the string
    fields they were derived from are untouched."""
    apps.get_model("hosts", "Tag").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("hosts", "0011_tag"),
        ("tasks", "0015_patchrollout_action_kind_patchrollout_baseline_and_more"),
        ("baselines", "0001_initial"),
        ("automations", "0007_automation_dispatch_mode"),
        ("reprovision", "0001_initial"),
    ]

    operations = [migrations.RunPython(populate, unpopulate)]
