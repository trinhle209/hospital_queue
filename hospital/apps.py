from django.apps import AppConfig


class HospitalConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "hospital"
    verbose_name = "Hàng đợi bệnh viện"

    def ready(self):
        from . import signals  # noqa: F401  (đăng ký tín hiệu realtime)