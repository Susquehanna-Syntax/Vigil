from django.urls import path

from . import views

urlpatterns = [
    path("", views.browse, name="catalog-browse"),
    path("categories/", views.categories, name="catalog-categories"),
    path("status/", views.status, name="catalog-status"),
    path("enable/", views.enable, name="catalog-enable"),
    path("sync/", views.sync_now, name="catalog-sync"),
    path("install-task/", views.install_task, name="catalog-install-task"),
    path("<int:package_id>/", views.detail, name="catalog-detail"),
    path("<int:package_id>/icon/", views.icon, name="catalog-icon"),
]
