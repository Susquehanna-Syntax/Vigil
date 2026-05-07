from django.urls import path

from . import views

urlpatterns = [
    path("download/<str:platform>/", views.download_agent, name="agent-download"),
    path("install.sh", views.install_script, name="agent-install-script"),
    path("install.ps1", views.install_ps1, name="agent-install-ps1"),
    path("upload/<str:platform>/", views.upload_agent, name="agent-upload"),
    path("info/", views.agent_info, name="agent-info"),
]
