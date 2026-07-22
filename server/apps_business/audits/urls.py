from django.urls import path

from . import views

urlpatterns = [
    path("", views.audit_list, name="audit-list"),
    path("export/", views.audit_export, name="audit-export"),
]
