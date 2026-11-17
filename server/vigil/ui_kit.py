"""The admin-only UI kit page (docs/UI-STANDARDS.md): every shared component,
rendered with the live stylesheets so a design change can be checked in one
place and in all four themes."""

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render

from apps.accounts.models import Role
from apps.accounts.permissions import OWNER, role_of


@login_required(login_url="/login/")
def ui_kit(request):
    if role_of(request.user) not in (Role.ADMIN, OWNER):
        raise Http404
    return render(request, "ui_kit.html")
