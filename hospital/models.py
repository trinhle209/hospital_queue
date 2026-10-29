from django.conf import settings
from django.db import models
from django.utils import timezone


class InvalidTransition(ValueError):
    """Chuyển trạng thái phiếu không hợp lệ."""


class Service(models.Model):
    name = models.CharField("Tên dịch vụ", max_length=100)
    code = models.CharField("Mã (tiền tố số)", max_length=3, unique=True)
    description = models.CharField("Mô tả", max_length=255, blank=True)
    default_minutes = models.PositiveIntegerField("Phút/lượt mặc định", default=8)

    def __str__(self):
        return f"{self.code} - {self.name}"


class Counter(models.Model):
    name = models.CharField("Tên quầy", max_length=50)
    service = models.ForeignKey(Service, on_delete=models.CASCADE, related_name="counters")
    is_open = models.BooleanField("Đang mở", default=True)

    def __str__(self):
        return f"{self.name} ({self.service.code})"


class Profile(models.Model):
    ROLES = [("patient", "Bệnh nhân / khách"), ("staff", "Nhân viên"), ("admin", "Quản lý")]
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    role = models.CharField(max_length=10, choices=ROLES, default="patient")
    counter = models.ForeignKey(Counter, null=True, blank=True, on_delete=models.SET_NULL,
                                help_text="Chỉ dùng cho nhân viên")

    def __str__(self):
        return f"{self.user.username} ({self.role})"


class Ticket(models.Model):
    """Phiếu số thứ tự. Trạng thái đi theo máy trạng thái TRANSITIONS bên dưới.

        waiting ──gọi số──▶ serving ──hoàn tất──▶ done
           │                  │  │
           │                  │  └──vắng mặt──▶ skipped ──khôi phục──▶ waiting (xếp cuối)
           │                  └──chuyển quầy──▶ waiting (giữ chỗ cũ, dành cho quầy đích)
           └──hủy/bỏ──▶ skipped
    """
    STATUS = [("waiting", "Đang chờ"), ("serving", "Đang phục vụ"),
              ("done", "Hoàn tất"), ("skipped", "Bỏ lượt")]
    TRANSITIONS = {
        "waiting": {"serving", "skipped"},
        "serving": {"done", "skipped", "waiting"},   # waiting = chuyển quầy
        "skipped": {"waiting"},                      # khôi phục
        "done": set(),
    }

    number = models.PositiveIntegerField()
    code = models.CharField("Mã phiếu", max_length=10, blank=True)  # VD: KB012, không đổi khi chuyển quầy
    service = models.ForeignKey(Service, on_delete=models.PROTECT)
    # Khách tự lấy số: có patient. Nhân viên bóc hộ: patient để trống.
    patient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                null=True, blank=True)
    walkin_name = models.CharField("Tên khách vãng lai", max_length=100, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="issued_tickets")
    description = models.CharField("Mô tả của bệnh nhân", max_length=255, blank=True)
    ai_note = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=10, choices=STATUS, default="waiting")
    counter = models.ForeignKey(Counter, null=True, blank=True, on_delete=models.SET_NULL)
    # Quầy được dành riêng (sau khi chuyển quầy) và quầy AI gợi ý khi lấy số
    preferred_counter = models.ForeignKey(Counter, null=True, blank=True, on_delete=models.SET_NULL,
                                          related_name="reserved_tickets")
    suggested_counter = models.ForeignKey(Counter, null=True, blank=True, on_delete=models.SET_NULL,
                                          related_name="suggested_tickets")
    # Dữ liệu phục vụ dự báo thời gian chờ
    ahead_at_issue = models.PositiveIntegerField(null=True, blank=True)
    est_wait = models.PositiveIntegerField("Dự báo lúc lấy số (phút)", null=True, blank=True)
    requeue_count = models.PositiveSmallIntegerField(default=0)  # số lần chuyển quầy / khôi phục
    created_at = models.DateTimeField(auto_now_add=True)
    queued_at = models.DateTimeField(default=timezone.now)       # thời điểm vào hàng đợi (dùng để xếp thứ tự)
    called_at = models.DateTimeField(null=True, blank=True)
    announced_at = models.DateTimeField(null=True, blank=True)   # lần gọi/gọi lại gần nhất (cho màn hình TV)
    finished_at = models.DateTimeField(null=True, blank=True)
    rating = models.PositiveSmallIntegerField(null=True, blank=True)

    @property
    def label(self):
        return self.code or f"{self.service.code}{self.number:03d}"

    @property
    def is_walkin(self):
        return self.patient_id is None

    @property
    def customer_name(self):
        if self.patient_id:
            return self.patient.username
        return self.walkin_name or "Khách vãng lai"

    # ---------- máy trạng thái ----------
    def change_status(self, new_status, by=None, counter=None, note=""):
        allowed = self.TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            names = dict(self.STATUS)
            raise InvalidTransition(
                f"Phiếu {self.label} đang '{names[self.status]}', "
                f"không thể chuyển sang '{names[new_status]}'."
            )
        old, now = self.status, timezone.now()
        self.status = new_status
        if new_status == "serving":
            self.counter = counter
            self.preferred_counter = None
            self.called_at = now
            self.announced_at = now
            self.finished_at = None
        elif new_status in ("done", "skipped"):
            self.finished_at = now
        elif new_status == "waiting":
            self.counter = None
            self.called_at = None
            self.finished_at = None
        self.save()
        TicketEvent.objects.create(
            ticket=self, from_status=old, to_status=new_status, by=by,
            counter=counter if counter is not None else self.counter, note=note,
        )

    def transfer_to(self, target, by=None):
        """Chuyển phiếu đang phục vụ sang quầy khác (có thể thuộc dịch vụ khác)."""
        if self.status != "serving":
            raise InvalidTransition("Chỉ chuyển được phiếu đang phục vụ.")
        source = self.counter
        if source and target.pk == source.pk:
            raise InvalidTransition("Phiếu đang ở chính quầy này.")
        if not target.is_open:
            raise InvalidTransition(f"{target.name} đang đóng, không thể chuyển sang.")
        note = f"Chuyển từ {source.name if source else '?'} sang {target.name}"
        self.service = target.service
        self.preferred_counter = target
        self.requeue_count += 1
        # giữ nguyên queued_at: khách không bị mất chỗ đã chờ
        self.change_status("waiting", by=by, counter=source, note=note)

    def restore(self, by=None):
        """Khôi phục phiếu đã bỏ lượt: xếp xuống cuối hàng đợi."""
        if self.status != "skipped":
            raise InvalidTransition("Chỉ khôi phục được phiếu đã bỏ lượt.")
        self.queued_at = timezone.now()
        self.preferred_counter = None
        self.requeue_count += 1
        self.change_status("waiting", by=by, note="Khôi phục vào hàng đợi (xếp cuối)")

    def __str__(self):
        return self.label


class TicketEvent(models.Model):
    """Nhật ký mọi lần đổi trạng thái của phiếu."""
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="events")
    from_status = models.CharField(max_length=10, blank=True)
    to_status = models.CharField(max_length=10)
    by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    counter = models.ForeignKey(Counter, null=True, blank=True, on_delete=models.SET_NULL)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.ticket_id}: {self.from_status or '-'} → {self.to_status}"


class Survey(models.Model):
    """Khảo sát sau khi được phục vụ."""
    ticket = models.OneToOneField(Ticket, on_delete=models.CASCADE, related_name="survey")
    overall = models.PositiveSmallIntegerField("Hài lòng chung")
    wait_rating = models.PositiveSmallIntegerField("Thời gian chờ")
    staff_rating = models.PositiveSmallIntegerField("Thái độ nhân viên")
    comment = models.CharField("Góp ý", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Khảo sát {self.ticket_id}: {self.overall}/5"