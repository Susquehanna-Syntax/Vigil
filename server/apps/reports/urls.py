from django.urls import path

from . import views

urlpatterns = [
    path("", views.report_index, name="report-index"),
    # Before <uuid:report_id>/ so that "widgets/" is not read as a report id.
    path("widgets/", views.widget_catalog, name="report-widget-catalog"),
    path("<uuid:report_id>/", views.report_detail, name="report-detail"),
    path("<uuid:report_id>/data/", views.report_data, name="report-data"),
    path("<uuid:report_id>/data/<int:index>.csv", views.report_data_csv,
         name="report-data-csv"),
]
