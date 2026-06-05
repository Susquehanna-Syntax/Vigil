"""Introduce the scanner field on VulnScan and rename nessus_scan_id.

Three operations in order:

1. Add ``scanner`` CharField with default ``"nessus"`` — existing rows
   were all created by the Nessus path, so they backfill correctly.
2. Add ``external_scan_id`` as a CharField(64), then copy the old
   ``nessus_scan_id`` integer values into it as strings so no scan id
   gets lost.
3. Drop ``nessus_scan_id``.
4. Add the new (scanner, state) index.

The add → copy → drop sequence preserves data, unlike the destructive
auto-generated rename Django produced when it noticed the type change
(IntegerField → CharField).
"""

from django.db import migrations, models


def copy_nessus_scan_id_to_external(apps, schema_editor):
    VulnScan = apps.get_model("vulns", "VulnScan")
    for scan in VulnScan.objects.exclude(nessus_scan_id__isnull=True):
        scan.external_scan_id = str(scan.nessus_scan_id)
        scan.save(update_fields=["external_scan_id"])


def copy_external_back_to_nessus(apps, schema_editor):
    VulnScan = apps.get_model("vulns", "VulnScan")
    for scan in VulnScan.objects.exclude(external_scan_id=""):
        try:
            scan.nessus_scan_id = int(scan.external_scan_id)
        except (TypeError, ValueError):
            scan.nessus_scan_id = None
        scan.save(update_fields=["nessus_scan_id"])


class Migration(migrations.Migration):

    dependencies = [
        ("vulns", "0002_vulnscan"),
    ]

    operations = [
        migrations.AddField(
            model_name="vulnscan",
            name="scanner",
            field=models.CharField(
                choices=[
                    ("nessus", "Nessus"),
                    ("greenbone", "Greenbone / OpenVAS"),
                    ("trivy", "Trivy"),
                ],
                default="nessus",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="vulnscan",
            name="external_scan_id",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.RunPython(
            copy_nessus_scan_id_to_external,
            reverse_code=copy_external_back_to_nessus,
        ),
        migrations.RemoveField(
            model_name="vulnscan",
            name="nessus_scan_id",
        ),
        migrations.AddIndex(
            model_name="vulnscan",
            index=models.Index(
                fields=["scanner", "state"],
                name="vulns_vulns_scanner_1352a6_idx",
            ),
        ),
    ]
