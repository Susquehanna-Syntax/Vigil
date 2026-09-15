"""One row per setting an operator has changed from the UI.

Absence is meaningful: no row means "whatever the environment or the code
default says". Clearing a field in the UI deletes the row rather than storing
an empty string, so reverting to the default is expressible and a stored value
is never ambiguous.
"""

from __future__ import annotations

from django.conf import settings as django_settings
from django.db import models


class InstanceSetting(models.Model):
    key = models.CharField(max_length=64, primary_key=True)

    #: Plain value for every non-secret kind, stored as text and coerced on
    #: read according to the registry.
    value = models.TextField(blank=True, default="")

    #: Fernet ciphertext for SECRET kinds, keyed off SECRET_KEY exactly as the
    #: AD bind password is (apps/hosts/crypto.py). Rotating SECRET_KEY means
    #: re-entering these through the UI.
    value_encrypted = models.BinaryField(blank=True, default=b"")

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+")

    class Meta:
        verbose_name = "instance setting"

    def __str__(self) -> str:
        from .registry import SECRET_NAMES
        shown = "(secret)" if self.key in SECRET_NAMES else self.value
        return f"{self.key}={shown}"
