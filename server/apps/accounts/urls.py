from django.urls import path

from . import views

urlpatterns = [
    path("totp/", views.totp_status, name="totp-status"),
    path("totp/enroll/", views.totp_enroll_start, name="totp-enroll-start"),
    path("totp/enroll/confirm/", views.totp_enroll_confirm, name="totp-enroll-confirm"),
    path("totp/disable/", views.totp_disable, name="totp-disable"),
    path("totp/debug-code/", views.totp_debug_code, name="totp-debug-code"),
]
