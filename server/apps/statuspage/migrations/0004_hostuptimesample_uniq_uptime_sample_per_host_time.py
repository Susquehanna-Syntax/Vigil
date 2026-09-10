"""One uptime sample per host per tick — and clear the ones already doubled.

The sampling beat took no lock and the table had no uniqueness, so two
overlapping runs each wrote a full set. The uptime bars count "up" rows against
total rows, so every duplicate nudged published availability upward.

Adding the constraint to a table that already contains duplicates would fail
the migration, and by the nature of the bug an install that has been running a
while is exactly where duplicates are. So they are collapsed first, keeping the
lowest id of each (host, time) group — the rows are identical apart from the
id, so which survives does not matter, only that one does.
"""

from django.db import migrations, models


def collapse_duplicates(apps, schema_editor):
    HostUptimeSample = apps.get_model("statuspage", "HostUptimeSample")
    from django.db.models import Count, Min

    dupes = (HostUptimeSample.objects
             .values("host_id", "time")
             .annotate(n=Count("id"), keep=Min("id"))
             .filter(n__gt=1))

    removed = 0
    for row in dupes.iterator(chunk_size=500):
        deleted, _ = (HostUptimeSample.objects
                      .filter(host_id=row["host_id"], time=row["time"])
                      .exclude(id=row["keep"])
                      .delete())
        removed += deleted
    if removed:
        print(f"  statuspage: collapsed {removed} duplicate uptime sample(s) "
              f"— published availability was reading high by that much.")


def noop(apps, schema_editor):
    """Reversing drops the constraint; the deleted duplicates are not restored,
    and should not be — they were never real measurements."""


class Migration(migrations.Migration):

    dependencies = [
        ('hosts', '0017_transportack'),
        ('statuspage', '0003_hostuptimesample'),
    ]

    operations = [
        migrations.RunPython(collapse_duplicates, noop),
        migrations.AddConstraint(
            model_name='hostuptimesample',
            constraint=models.UniqueConstraint(
                fields=('host', 'time'),
                name='uniq_uptime_sample_per_host_time'),
        ),
    ]
