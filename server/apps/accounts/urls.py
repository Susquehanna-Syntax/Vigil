from django.urls import path

from . import views

urlpatterns = [
    path("capabilities/", views.capabilities_index, name="capabilities-index"),
    path("users/", views.users_index, name="users-index"),
    path("users/<int:user_id>/", views.user_detail, name="user-detail"),
    path("users/<int:user_id>/role/", views.user_role, name="user-role"),
    path("totp/", views.totp_status, name="totp-status"),
    path("totp/enroll/", views.totp_enroll_start, name="totp-enroll-start"),
    path("totp/enroll/confirm/", views.totp_enroll_confirm, name="totp-enroll-confirm"),
    path("totp/disable/", views.totp_disable, name="totp-disable"),
    path("totp/debug-code/", views.totp_debug_code, name="totp-debug-code"),
    path("security-keys/", views.security_keys_list, name="security-keys-list"),
    path("security-keys/register/options/", views.security_key_register_options,
         name="security-key-register-options"),
    path("security-keys/register/", views.security_key_register, name="security-key-register"),
    path("security-keys/assert/options/", views.security_key_assert_options,
         name="security-key-assert-options"),
    path("security-keys/assert/", views.security_key_assert, name="security-key-assert"),
    # Same path, split by method: PATCH renames, DELETE removes.
    path("security-keys/<int:key_id>/", views.security_key_detail, name="security-key-detail"),
    path("session/activity/", views.session_activity, name="session-activity"),
    path("session/", views.session_status, name="session-status"),
]
