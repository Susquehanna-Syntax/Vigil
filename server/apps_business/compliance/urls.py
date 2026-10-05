from django.urls import path

from . import views

urlpatterns = [
    path("report/", views.report, name="compliance-report"),
    path("report.csv", views.report_csv, name="compliance-report-csv"),
    path("report.html", views.report_html, name="compliance-report-html"),
]
