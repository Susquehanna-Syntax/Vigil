from django.db import models

class BrandingConfig(models.Model):
    """Business branding for this instance — one row (pk=1). Fields are all
    optional; blank means 'use the Vigil default'. Reads are open so the
    login page can theme before auth; writes require the 'branding' feature."""
    product_name = models.CharField(max_length=60, blank=True, default="")
    logo_url = models.URLField(blank=True, default="")
    accent = models.CharField(max_length=7, blank=True, default="")  # #RRGGBB
    footer_text = models.CharField(max_length=200, blank=True, default="")
    support_url = models.URLField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
