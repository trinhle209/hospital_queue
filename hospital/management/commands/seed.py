import os

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from hospital.models import Counter, Profile, Service


class Command(BaseCommand):
    help = "Tạo dịch vụ, quầy và tài khoản mẫu (chạy nhiều lần vẫn an toàn)"

    def handle(self, *args, **opts):
        data = [
            ("KB", "Khám bệnh", "Khám tổng quát, đau, sốt, ho, tái khám", 10, 2),
            ("XN", "Xét nghiệm", "Xét nghiệm máu, nước tiểu", 6, 1),
            ("HA", "Chẩn đoán hình ảnh", "X-quang, siêu âm, CT, MRI", 12, 1),
            ("TH", "Nhận thuốc", "Lấy thuốc theo đơn", 4, 1),
            ("TT", "Thanh toán viện phí", "Thanh toán, bảo hiểm, hoàn tiền", 5, 1),
        ]
        for code, name, desc, mins, n in data:
            s, _ = Service.objects.get_or_create(
                code=code, defaults={"name": name, "description": desc, "default_minutes": mins})
            for i in range(1, n + 1):
                Counter.objects.get_or_create(service=s, name=f"Quầy {code}-{i}")

        # --- Quản lý ---
        admin_pw = os.getenv("ADMIN_PASSWORD", "admin123")
        admin = User.objects.filter(username="admin").first()
        if not admin:
            admin = User.objects.create_superuser("admin", password=admin_pw)
        Profile.objects.update_or_create(user=admin, defaults={"role": "admin"})

        # --- Nhân viên (mỗi người một quầy) ---
        staff_accounts = [
            ("nhanvien1", "Quầy KB-1"), ("nhanvien2", "Quầy KB-2"),
            ("nhanvien_xn", "Quầy XN-1"), ("nhanvien_ha", "Quầy HA-1"),
        ]
        for username, counter_name in staff_accounts:
            u, created = User.objects.get_or_create(username=username)
            if created or username == "nhanvien1":
                u.set_password("123456")
                u.save()
            Profile.objects.update_or_create(
                user=u, defaults={"role": "staff", "counter": Counter.objects.get(name=counter_name)})

        # --- Bệnh nhân / khách ---
        for username in ("benhnhan1", "benhnhan2", "benhnhan3"):
            u, created = User.objects.get_or_create(username=username)
            if created:
                u.set_password("123456")
                u.save()
            Profile.objects.get_or_create(user=u, defaults={"role": "patient"})

        self.stdout.write(self.style.SUCCESS(
            "Xong! admin / nhanvien1, nhanvien2, nhanvien_xn, nhanvien_ha / benhnhan1..3"))