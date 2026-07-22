from django.urls import path

from . import views

urlpatterns = [
    path("users/", views.users_index, name="users-index"),
    path("users/<int:user_id>/role/", views.user_role, name="user-role"),
    path("totp/", views.totp_status, name="totp-status"),
    path("totp/enroll/", views.totp_enroll_start, name="totp-enroll-start"),
    path("totp/enroll/confirm/", views.totp_enroll_confirm, name="totp-enroll-confirm"),
    path("totp/disable/", views.totp_disable, name="totp-disable"),
    path("totp/debug-code/", views.totp_debug_code, name="totp-debug-code"),
]
