import random
from datetime import datetime, timedelta
from math import ceil

from django.core.management.base import BaseCommand
from django.utils import timezone

from hospital.models import Service, Survey, Ticket

TAG = "Khách mẫu"
HOUR_WEIGHTS = {7: 5, 8: 9, 9: 8, 10: 6, 11: 3, 13: 5, 14: 6, 15: 4, 16: 2}
DAILY = {"KB": 60, "XN": 45, "HA": 28, "TH": 50, "TT": 38}
SERVICE_MIN = {"KB": (10, 3), "XN": (6, 2), "HA": (14, 4), "TH": (4, 1), "TT": (6, 2)}  # (trung bình, độ lệch)


class Command(BaseCommand):
    help = "Tạo lịch sử phục vụ mẫu (vài tuần) để dự báo, báo cáo và AI tóm tắt nghẽn có dữ liệu"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=14)
        parser.add_argument("--reset", action="store_true", help="Xóa dữ liệu mẫu cũ rồi tạo lại")

    def handle(self, *args, **opts):
        random.seed(42)
        if opts["reset"]:
            Ticket.objects.filter(walkin_name=TAG, patient__isnull=True).delete()
        elif Ticket.objects.filter(walkin_name=TAG).exists():
            self.stdout.write("Đã có dữ liệu mẫu. Dùng --reset để tạo lại.")
            return
        services = {s.code: s for s in Service.objects.all()}
        if not services:
            self.stderr.write("Hãy chạy: python manage.py seed trước.")
            return

        today = timezone.localdate()
        total_w = sum(HOUR_WEIGHTS.values())
        tickets, created_times, surveys_for = [], [], []
        for d in range(opts["days"], 0, -1):
            day = today - timedelta(days=d)
            if day.weekday() == 6:  # Chủ nhật nghỉ
                continue
            for code, svc in services.items():
                counters = list(svc.counters.all())
                nc = max(len(counters), 1)
                total = int(DAILY.get(code, 30) * random.uniform(0.8, 1.2))
                hours = sorted(random.choices(list(HOUR_WEIGHTS), weights=list(HOUR_WEIGHTS.values()), k=total))
                mean, sd = SERVICE_MIN.get(code, (8, 2))
                for seq, h in enumerate(hours, start=1):
                    created = timezone.make_aware(datetime(
                        day.year, day.month, day.day, h, random.randint(0, 59), random.randint(0, 59)))
                    lam = HOUR_WEIGHTS[h] / total_w * total               # lượt đến mỗi giờ
                    rho = min(lam * mean / (60.0 * nc), 1.5)              # mức tải của quầy
                    ahead = max(0, round(random.gauss(rho * lam * 0.5, 1.5)))
                    wait = ahead * mean / nc * random.uniform(0.75, 1.35) + random.uniform(0, 2)
                    est = int(ceil(ahead * max(mean + random.gauss(0, 1), 1) / nc))
                    duration = max(1.0, random.gauss(mean, sd))
                    called = created + timedelta(minutes=wait)
                    skipped = random.random() < 0.05
                    finished = called + timedelta(minutes=0.5 if skipped else duration)
                    t = Ticket(
                        number=seq, code=f"{code}{seq:03d}", service=svc, patient=None,
                        walkin_name=TAG, status="skipped" if skipped else "done",
                        counter=random.choice(counters) if counters else None,
                        queued_at=created, called_at=called, announced_at=called, finished_at=finished,
                        ahead_at_issue=ahead, est_wait=est, requeue_count=0,
                    )
                    tickets.append(t)
                    created_times.append(created)
                    if not skipped and random.random() < 0.55:
                        score = 5 - wait / 12 + random.gauss(0, 0.7)
                        surveys_for.append((t, int(min(5, max(1, round(score))))))

        Ticket.objects.bulk_create(tickets, batch_size=500)
        for t, c in zip(tickets, created_times):
            t.created_at = c        # auto_now_add ghi đè lúc tạo, nên cập nhật lại sau
        Ticket.objects.bulk_update(tickets, ["created_at"], batch_size=500)

        surveys = []
        for t, overall in surveys_for:
            t.rating = overall
            surveys.append(Survey(ticket=t, overall=overall,
                                  wait_rating=min(5, max(1, overall + random.choice([-1, 0, 0, 1]))),
                                  staff_rating=min(5, max(1, overall + random.choice([0, 0, 1])))))
        Survey.objects.bulk_create(surveys, batch_size=500)
        Ticket.objects.bulk_update([t for t, _ in surveys_for], ["rating"], batch_size=500)
        self.stdout.write(self.style.SUCCESS(
            f"Đã tạo {len(tickets)} phiếu lịch sử và {len(surveys)} khảo sát mẫu."))