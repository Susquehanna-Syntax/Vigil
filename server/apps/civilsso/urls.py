from django.urls import path

from apps.civilsso import views

urlpatterns = [
    path("accounts/civil/login/", views.login_start, name="civil-login"),
    path("accounts/civil/callback", views.callback, name="civil-callback"),
]
