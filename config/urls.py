from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from hospital import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/login/", auth_views.LoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("accounts/register/", views.register, name="register"),
    # --- Bệnh nhân / khách ---
    path("", views.home, name="home"),
    path("ticket/take/", views.take_ticket, name="take_ticket"),
    path("ticket/<int:pk>/survey/", views.survey_ticket, name="survey_ticket"),
    path("ticket/<int:pk>/cancel/", views.cancel_ticket, name="cancel_ticket"),
    path("ticket/<int:pk>/slip/", views.ticket_slip, name="ticket_slip"),
    # --- Màn hình TV phòng chờ (công khai) ---
    path("display/", views.display, name="display"),
    # --- API JSON ---
    path("api/board/", views.api_board, name="api_board"),
    path("api/services/", views.api_services, name="api_services"),
    path("api/tickets/", views.api_ticket_create, name="api_ticket_create"),
    path("api/tickets/me/", views.api_ticket_me, name="api_ticket_me"),
    path("api/classify/", views.api_classify, name="api_classify"),
    # --- Nhân viên ---
    path("staff/", views.staff_panel, name="staff_panel"),
    path("staff/live/", views.staff_live, name="staff_live"),
    path("staff/action/", views.staff_action, name="staff_action"),
    path("staff/take/", views.staff_take_ticket, name="staff_take_ticket"),
    # --- Quản lý ---
    path("report/", views.report, name="report"),
]