from django.urls import path

from . import views

urlpatterns = [
    path("download/<str:platform>/", views.download_agent, name="agent-download"),
    path("install.sh", views.install_script, name="agent-install-script"),
    path("upload/<str:platform>/", views.upload_agent, name="agent-upload"),
    path("info/", views.agent_info, name="agent-info"),
]
