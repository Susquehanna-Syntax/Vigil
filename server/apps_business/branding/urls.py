from django.urls import path

from . import views

app_name = "business_branding"

urlpatterns = [
    path("", views.branding_config, name="branding"),
]
