import json
from functools import wraps

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.db import transaction
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import services as svc
from .models import Counter, InvalidTransition, Profile, Service, Survey, Ticket

ACTIVE = svc.ACTIVE


def role_required(*roles):
    def deco(fn):
        @wraps(fn)
        @login_required
        def wrapper(request, *a, **k):
            if svc.get_profile(request.user).role not in roles:
                return HttpResponseForbidden("Bạn không có quyền truy cập trang này.")
            return fn(request, *a, **k)
        return wrapper
    return deco


def api_login_required(fn):
    @wraps(fn)
    def wrapper(request, *a, **k):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Chưa đăng nhập."}, status=401)
        return fn(request, *a, **k)
    return wrapper


def _json_body(request):
    try:
        data = json.loads(request.body or b"{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _clamp_rating(value, default=None):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return v if 1 <= v <= 5 else default


# =====================================================================
# ĐĂNG KÝ / TRANG BỆNH NHÂN
# =====================================================================
def register(request):
    form = UserCreationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        Profile.objects.create(user=user, role="patient")
        login(request, user)
        return redirect("home")
    return render(request, "registration/register.html", {"form": form})


@login_required
def home(request):
    profile = svc.get_profile(request.user)
    if profile.role == "staff":
        return redirect("staff_panel")
    if profile.role == "admin":
        return redirect("report")

    my = Ticket.objects.filter(patient=request.user, status__in=ACTIVE).first()
    to_survey = (Ticket.objects.filter(patient=request.user, status="done", survey__isnull=True)
                 .order_by("-finished_at").first())
    history = (Ticket.objects.filter(patient=request.user)
               .select_related("service").order_by("-created_at")[:5])
    return render(request, "home.html", {
        "services": Service.objects.all(), "my": my,
        "to_survey": to_survey, "history": history,
    })


@role_required("patient")
@require_POST
def take_ticket(request):
    """Dự phòng khi trình duyệt không chạy JavaScript (form HTML thường)."""
    try:
        ticket = svc.request_ticket(request.user, request.POST.get("service") or None,
                                    request.POST.get("description", ""))
    except svc.TicketError as e:
        messages.error(request, str(e))
        return redirect("home")
    messages.success(request, f"Đã lấy số {ticket.label}. {ticket.ai_note}")
    return redirect("home")


@role_required("patient")
@require_POST
def survey_ticket(request, pk):
    ticket = get_object_or_404(Ticket, pk=pk, patient=request.user, status="done")
    overall = _clamp_rating(request.POST.get("overall"))
    if overall is None:
        messages.error(request, "Vui lòng chọn mức hài lòng chung.")
        return redirect("home")
    wait_rating = _clamp_rating(request.POST.get("wait_rating"), overall)
    staff_rating = _clamp_rating(request.POST.get("staff_rating"), overall)
    _, created = Survey.objects.get_or_create(ticket=ticket, defaults={
        "overall": overall, "wait_rating": wait_rating, "staff_rating": staff_rating,
        "comment": request.POST.get("comment", "").strip()[:255],
    })
    if created:
        ticket.rating = overall
        ticket.save(update_fields=["rating"])
        messages.success(request, "Cảm ơn bạn đã góp ý!")
    return redirect("home")


@role_required("patient")
@require_POST
def cancel_ticket(request, pk):
    ticket = get_object_or_404(Ticket, pk=pk, patient=request.user, status="waiting")
    try:
        ticket.change_status("skipped", by=request.user, note="Bệnh nhân hủy số")
        messages.info(request, "Đã hủy số.")
    except InvalidTransition as e:
        messages.error(request, str(e))
    return redirect("home")


@login_required
def ticket_slip(request, pk):
    """Phiếu số (có thời gian chờ dự kiến) để xem hoặc in."""
    ticket = get_object_or_404(
        Ticket.objects.select_related("service", "patient", "suggested_counter", "counter"), pk=pk)
    role = svc.get_profile(request.user).role
    if role == "patient" and ticket.patient_id != request.user.id:
        return HttpResponseForbidden("Đây không phải phiếu của bạn.")
    ahead = svc.people_ahead(ticket) if ticket.status == "waiting" else 0
    detail = svc.predict_wait_detail(ticket.service, ahead) if ticket.status == "waiting" else None
    return render(request, "ticket_slip.html", {
        "ticket": ticket, "ahead": ahead, "detail": detail,
        "back_url": reverse("home") if role == "patient" else reverse("staff_panel"),
    })


# =====================================================================
# MÀN HÌNH TV PHÒNG CHỜ (công khai, không cần đăng nhập)
# =====================================================================
def display(request):
    return render(request, "display.html")


# =====================================================================
# API JSON
# =====================================================================
def _ticket_json(t):
    return {
        "id": t.pk, "label": t.label, "status": t.status,
        "service": {"id": t.service_id, "code": t.service.code, "name": t.service.name},
        "ahead_at_issue": t.ahead_at_issue, "est_wait": t.est_wait,
        "suggested_counter": t.suggested_counter.name if t.suggested_counter else None,
        "ai_note": t.ai_note, "slip_url": reverse("ticket_slip", args=[t.pk]),
    }


@api_login_required
@require_GET
def api_board(request):
    return JsonResponse(svc.board_payload(request.user.pk))


@api_login_required
@require_GET
def api_services(request):
    data = svc.board_payload(request.user.pk)["services"]
    return JsonResponse({"services": [
        {k: s[k] for k in ("id", "code", "name", "waiting", "open_counters", "est_wait")}
        for s in data]})


@api_login_required
@require_POST
def api_ticket_create(request):
    """Lấy số theo loại dịch vụ: {"service": 1} hoặc {"description": "tôi bị sốt"}."""
    if svc.get_profile(request.user).role != "patient":
        return JsonResponse({"error": "Chỉ tài khoản bệnh nhân mới tự lấy số."}, status=403)
    body = _json_body(request)
    try:
        ticket = svc.request_ticket(request.user, body.get("service") or None,
                                    body.get("description", ""))
    except svc.TicketError as e:
        return JsonResponse({"error": str(e)}, status=400)
    messages.success(request, f"Đã lấy số {ticket.label}. {ticket.ai_note}")
    return JsonResponse(_ticket_json(ticket), status=201)


@api_login_required
@require_GET
def api_ticket_me(request):
    return JsonResponse({"ticket": svc.board_payload(request.user.pk)["mine"]})


@api_login_required
@require_POST
def api_classify(request):
    """AI phân loại mô tả ngắn: trả về dịch vụ + quầy gợi ý ít tải nhất + thời gian chờ."""
    description = str(_json_body(request).get("description", "")).strip()[:255]
    if not description:
        return JsonResponse({"error": "Hãy nhập mô tả ngắn về nhu cầu."}, status=400)
    r = svc.classify_and_suggest(description, str(request.user.pk))
    if not r:
        return JsonResponse({"error": "Chưa xác định được dịch vụ."}, status=400)
    s, c = r["service"], r["counter"]
    return JsonResponse({
        "service": {"id": s.id, "code": s.code, "name": s.name},
        "counter": {"id": c.id, "name": c.name} if c else None,
        "counter_note": r["counter_note"], "reason": r["note"], "source": r["source"],
        "ahead": r["ahead"], "est_wait": r["est_wait"], "method": r["method"],
    })


# =====================================================================
# NHÂN VIÊN
# =====================================================================
def _staff_context(profile):
    counter = profile.counter
    ctx = {"counter": counter}
    if counter:
        today = timezone.localdate()
        ctx["current"] = (Ticket.objects.filter(counter=counter, status="serving")
                          .select_related("patient", "service").first())
        ctx["waiting"] = (svc.waiting_qs(counter.service)
                          .select_related("patient", "preferred_counter", "suggested_counter")
                          .order_by("queued_at", "id"))
        ctx["skipped"] = (Ticket.objects.filter(service=counter.service, status="skipped",
                                                finished_at__date=today, counter__isnull=False)
                          .select_related("patient").order_by("-finished_at")[:8])
        ctx["other_counters"] = (Counter.objects.exclude(pk=counter.pk)
                                 .select_related("service").order_by("service__code", "name"))
    return ctx


@role_required("staff", "admin")
def staff_panel(request):
    ctx = _staff_context(svc.get_profile(request.user))
    ctx["services"] = Service.objects.all()
    return render(request, "staff.html", ctx)


@role_required("staff", "admin")
def staff_live(request):
    """Phần giao diện tự làm mới qua WebSocket (không đụng tới thông báo flash)."""
    return render(request, "staff_live.html", _staff_context(svc.get_profile(request.user)))


@role_required("staff", "admin")
@require_POST
def staff_action(request):
    counter = svc.get_profile(request.user).counter
    if not counter:
        messages.error(request, "Tài khoản chưa được gán quầy.")
        return redirect("staff_panel")
    action = request.POST.get("action")
    try:
        with transaction.atomic():
            # khóa quầy: bấm đúp hoặc 2 thiết bị cùng thao tác vẫn không bị lệch
            counter = Counter.objects.select_for_update().get(pk=counter.pk)
            current = Ticket.objects.filter(counter=counter, status="serving").first()

            if action == "call":
                if current:
                    messages.error(request, "Hãy hoàn tất hoặc chuyển số hiện tại trước.")
                elif not counter.is_open:
                    messages.error(request, "Quầy đang đóng.")
                else:
                    nxt = svc.waiting_for_counter(counter).select_for_update(skip_locked=True).first()
                    if nxt:
                        nxt.change_status("serving", by=request.user, counter=counter, note="Gọi số")
                    else:
                        messages.info(request, "Không còn ai đang chờ.")

            elif action == "recall":
                if not current:
                    messages.error(request, "Chưa có số nào đang phục vụ để gọi lại.")
                else:
                    current.announced_at = timezone.now()
                    current.save(update_fields=["announced_at"])
                    messages.info(request, f"Đã gọi lại {current.label}.")

            elif action in ("done", "skip"):
                if not current:
                    messages.error(request, "Chưa có số nào đang phục vụ.")
                elif action == "done":
                    current.change_status("done", by=request.user, note="Hoàn tất phục vụ")
                else:
                    current.change_status("skipped", by=request.user, note="Khách vắng mặt, bỏ lượt")
                    messages.info(request, f"Đã bỏ lượt {current.label}.")

            elif action == "transfer":
                target = Counter.objects.filter(pk=request.POST.get("target") or 0).first()
                if not current:
                    messages.error(request, "Chưa có số nào đang phục vụ để chuyển.")
                elif not target:
                    messages.error(request, "Hãy chọn quầy cần chuyển tới.")
                else:
                    label = current.label
                    current.transfer_to(target, by=request.user)
                    messages.success(request, f"Đã chuyển {label} sang {target.name}.")

            elif action == "restore":
                t = (Ticket.objects.select_for_update()
                     .filter(pk=request.POST.get("ticket") or 0, status="skipped",
                             service=counter.service).first())
                if not t:
                    messages.error(request, "Không tìm thấy phiếu cần khôi phục.")
                else:
                    t.restore(by=request.user)
                    messages.success(request, f"Đã đưa {t.label} trở lại cuối hàng đợi.")

            elif action == "toggle":
                if counter.is_open:
                    if current:
                        messages.error(request, "Hoàn tất hoặc chuyển số hiện tại trước khi đóng quầy.")
                    else:
                        counter.is_open = False
                        counter.save()
                        # trả các phiếu đang dành riêng cho quầy này về hàng chung
                        Ticket.objects.filter(preferred_counter=counter, status="waiting"
                                              ).update(preferred_counter=None)
                        if (not counter.service.counters.filter(is_open=True).exists()
                                and svc.waiting_qs(counter.service).exists()):
                            messages.warning(request, "Đây là quầy mở cuối cùng của dịch vụ "
                                             "mà vẫn còn khách chờ.")
                else:
                    counter.is_open = True
                    counter.save()
            else:
                messages.error(request, "Thao tác không hợp lệ.")
    except InvalidTransition as e:
        messages.error(request, str(e))
    return redirect("staff_panel")


@role_required("staff", "admin")
@require_POST
def staff_take_ticket(request):
    """Nhân viên bóc số hộ khách không biết dùng điện thoại."""
    description = request.POST.get("description", "").strip()[:255]
    walkin_name = request.POST.get("walkin_name", "").strip()[:100]
    service_id = request.POST.get("service")
    ai_note = ""
    if service_id:
        service = get_object_or_404(Service, pk=service_id)
    else:
        service, ai_note, _ = svc.classify_cached(description, f"staff{request.user.pk}")
        if not service:
            messages.error(request, "Hãy nhập mô tả nhu cầu hoặc chọn dịch vụ.")
            return redirect("staff_panel")
    ticket = svc.issue_ticket(service, description=description, ai_note=ai_note,
                              walkin_name=walkin_name, created_by=request.user)
    return redirect(reverse("ticket_slip", args=[ticket.pk]))


# =====================================================================
# QUẢN LÝ
# =====================================================================
@role_required("admin")
def report(request):
    stats = svc.get_stats(14)
    summary = svc.summarize_bottleneck(stats) if request.GET.get("ai") else None
    return render(request, "report.html", {
        "stats": stats, "summary": summary,
        "max_hour": max([h["count"] for h in stats["hours"]] or [1]),
    })