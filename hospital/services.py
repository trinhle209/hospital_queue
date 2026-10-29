import hashlib
import json
import os
import re
import time
from collections import defaultdict
from datetime import timedelta
from math import ceil

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Profile, Service, Survey, Ticket, TicketEvent

ACTIVE = ["waiting", "serving"]


class TicketError(Exception):
    """Lỗi nghiệp vụ khi lấy số (hiển thị cho người dùng)."""


def get_profile(user):
    profile, _ = Profile.objects.get_or_create(
        user=user, defaults={"role": "admin" if user.is_superuser else "patient"}
    )
    return profile


def today():
    return timezone.localdate()


# =====================================================================
# HÀNG ĐỢI
# =====================================================================
def waiting_qs(service):
    return Ticket.objects.filter(service=service, status="waiting", queued_at__date=today())


def waiting_for_counter(counter):
    """Phiếu mà quầy này được phép gọi: phiếu chung + phiếu dành riêng cho quầy này."""
    return (waiting_qs(counter.service)
            .filter(Q(preferred_counter__isnull=True) | Q(preferred_counter=counter))
            .order_by("queued_at", "id"))


def people_ahead(ticket):
    return waiting_qs(ticket.service).filter(queued_at__lt=ticket.queued_at).count()


# =====================================================================
# TASK 4 - DỰ BÁO THỜI GIAN CHỜ
#   (1) Trung bình trượt thời gian phục vụ của N lượt gần nhất
#   (2) Hồi quy tuyến tính: thời gian chờ thực tế ~ số người đứng trước lúc lấy số
#   Kết hợp: càng nhiều dữ liệu lịch sử, trọng số hồi quy càng lớn (tối đa 50%).
# =====================================================================
MA_WINDOW = 20     # cửa sổ trượt
SMOOTHING = 3      # số "quan sát giả" bằng thời gian mặc định (xử lý khi chưa đủ dữ liệu)
MIN_REG_SAMPLES = 8


def avg_service_minutes(service):
    """Trung bình trượt có làm mượt. Trả về (phút/lượt, số mẫu thực dùng)."""
    qs = Ticket.objects.filter(
        service=service, status="done",
        called_at__isnull=False, finished_at__isnull=False,
    ).order_by("-finished_at")[:MA_WINDOW]
    mins = [max((t.finished_at - t.called_at).total_seconds() / 60, 0) for t in qs]
    n = len(mins)
    avg = (sum(mins) + SMOOTHING * service.default_minutes) / (n + SMOOTHING)
    return max(avg, 1), n


def _linear_fit(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return my - b * mx, b


def regression_wait(service, ahead):
    """Dự báo bằng hồi quy tuyến tính trên lịch sử 30 ngày. Trả về (phút, số mẫu) hoặc None."""
    since = timezone.now() - timedelta(days=30)
    qs = Ticket.objects.filter(
        service=service, requeue_count=0, ahead_at_issue__isnull=False,
        called_at__isnull=False, queued_at__gte=since,
    ).order_by("-queued_at")[:300]
    pts = [(t.ahead_at_issue, (t.called_at - t.queued_at).total_seconds() / 60)
           for t in qs if t.called_at >= t.queued_at]
    if len(pts) < MIN_REG_SAMPLES:
        return None
    fit = _linear_fit([p[0] for p in pts], [p[1] for p in pts])
    if not fit or fit[1] <= 0:      # độ dốc <= 0 là dữ liệu chưa đáng tin
        return None
    a, b = fit
    return max(a + b * ahead, 0), len(pts)


def predict_wait_detail(service, ahead, open_counters=None):
    if open_counters is None:
        open_counters = service.counters.filter(is_open=True).count()
    counters = max(open_counters, 1)
    avg, n = avg_service_minutes(service)
    base = ahead * avg / counters
    minutes = base
    method = f"Trung bình trượt {n} lượt gần nhất"
    reg = regression_wait(service, ahead) if ahead > 0 else None
    if reg:
        pred, m = reg
        w = min(0.5, m / 60)
        minutes = (1 - w) * base + w * pred
        method += f" + hồi quy tuyến tính ({m} mẫu, trọng số {int(w * 100)}%)"
    if open_counters == 0 and ahead > 0:
        method += " (hiện không có quầy nào mở)"
    return {
        "minutes": int(ceil(round(minutes, 2))) if minutes > 0 else 0,
        "method": method,
        "avg_service": round(avg, 1),
        "counters": open_counters,
        "ahead": ahead,
    }


def predict_wait_minutes(service, ahead):
    return predict_wait_detail(service, ahead)["minutes"]


# =====================================================================
# TASK 5 - GỢI Ý QUẦY ÍT TẢI NHẤT
# =====================================================================
def counter_load(counter, avg_minutes):
    busy = Ticket.objects.filter(counter=counter, status="serving").count()
    reserved = Ticket.objects.filter(preferred_counter=counter, status="waiting").count()
    assigned = Ticket.objects.filter(suggested_counter=counter, status="waiting",
                                     queued_at__date=today()).count()
    done_today = Ticket.objects.filter(counter=counter, status="done",
                                       finished_at__date=today()).count()
    return (busy + reserved + assigned) * avg_minutes, done_today


def suggest_counter(service):
    """Quầy đang mở có khối lượng việc (phút) nhỏ nhất; hòa thì chọn quầy đã phục vụ ít hơn."""
    avg, _ = avg_service_minutes(service)
    best = None
    for c in service.counters.filter(is_open=True).order_by("id"):
        load, done = counter_load(c, avg)
        key = (load, done, c.pk)
        if best is None or key < best[0]:
            best = (key, c, load)
    if not best:
        return None, "Hiện chưa có quầy nào đang mở"
    _, counter, load = best
    if load == 0:
        return counter, "đang rảnh"
    return counter, f"ít việc nhất (~{int(ceil(load))} phút việc đang chờ)"


# =====================================================================
# TASK 5 - AI PHÂN LOẠI (GEMINI)
# =====================================================================
def _ai(prompt, max_tokens=400):
    """Gọi Gemini, tự thử lại khi quá tải và đổi sang model dự phòng."""
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    try:
        from google import genai
        from google.genai import types
    except Exception as e:
        print("LOI GEMINI (import):", e)
        return None

    client = genai.Client(api_key=key)
    models = [
        os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
        os.getenv("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash"),
    ]
    for model in models:
        for attempt in range(3):
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(max_output_tokens=max_tokens * 4),
                )
                if resp.text:
                    return resp.text
            except Exception as e:
                msg = str(e)
                print(f"LOI GEMINI [{model}] lan {attempt + 1}: {msg[:200]}")
                if "503" in msg or "429" in msg or "UNAVAILABLE" in msg:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
    return None


KEYWORDS = {
    "XN": ["xét nghiệm", "máu", "nước tiểu", "đường huyết", "mỡ máu"],
    "HA": ["x-quang", "xquang", "siêu âm", "ct", "mri", "chụp", "nội soi"],
    "TH": ["thuốc", "đơn thuốc", "nhận thuốc", "lấy thuốc"],
    "TT": ["thanh toán", "viện phí", "bảo hiểm", "hoàn tiền", "hóa đơn", "bhyt"],
    "KB": ["khám", "đau", "sốt", "ho", "mệt", "chóng mặt", "tái khám", "đau đầu"],
}


def _has_keyword(text, keyword):
    # khớp nguyên từ để "ho" không dính vào "hoàn", "ct" không dính vào từ khác
    return re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", text) is not None


def classify_request(description):
    """Trả về (Service, ghi chú, nguồn). Nguồn: 'ai' | 'keyword' | 'default' | None."""
    services = list(Service.objects.all())
    if not services or not description.strip():
        return None, "", None
    options = "\n".join(f"- {s.code}: {s.name} ({s.description})" for s in services)
    reply = _ai(
        "Bạn là nhân viên phân luồng tại bệnh viện. Dựa vào mô tả của người bệnh, "
        "chọn ĐÚNG MỘT mã dịch vụ phù hợp nhất.\n"
        f"Danh sách dịch vụ:\n{options}\n\nMô tả: \"{description}\"\n\n"
        'Chỉ trả về JSON dạng {"code": "KB", "reason": "lý do ngắn gọn tiếng Việt"}',
        max_tokens=150,
    )
    if reply:
        try:
            data = json.loads(reply[reply.index("{"): reply.rindex("}") + 1])
            code = str(data["code"]).strip().upper()
            svc = next((s for s in services if s.code == code), None)
            if svc:
                return svc, "AI: " + str(data.get("reason", "")).strip(), "ai"
        except Exception:
            pass
    text = description.lower()
    for code in ["XN", "HA", "TH", "TT", "KB"]:
        if any(_has_keyword(text, k) for k in KEYWORDS[code]):
            svc = next((s for s in services if s.code == code), None)
            if svc:
                return svc, "Gợi ý theo từ khóa", "keyword"
    default = next((s for s in services if s.code == "KB"), None)
    if default:
        return default, "Chưa rõ nhu cầu, xếp vào Khám bệnh để được hướng dẫn thêm", "default"
    return None, "", None


def classify_cached(description, user_key=""):
    """Lưu kết quả phân loại 5 phút để bấm 'Gợi ý' rồi 'Lấy số' không phải gọi AI hai lần."""
    key = "cls:" + hashlib.md5(f"{user_key}|{description}".encode()).hexdigest()
    hit = cache.get(key)
    if hit:
        svc = Service.objects.filter(pk=hit[0]).first()
        if svc:
            return svc, hit[1], hit[2]
    svc, note, source = classify_request(description)
    if svc:
        cache.set(key, (svc.pk, note, source), 300)
    return svc, note, source


def classify_and_suggest(description, user_key=""):
    """Dịch vụ + quầy gợi ý + thời gian chờ dự kiến cho một mô tả ngắn."""
    service, note, source = classify_cached(description, user_key)
    if not service:
        return None
    counter, counter_note = suggest_counter(service)
    ahead = waiting_qs(service).count()
    detail = predict_wait_detail(service, ahead)
    return {"service": service, "note": note, "source": source,
            "counter": counter, "counter_note": counter_note,
            "ahead": ahead, "est_wait": detail["minutes"], "method": detail["method"]}


# =====================================================================
# CẤP SỐ
# =====================================================================
def issue_ticket(service, description="", ai_note="", patient=None,
                 walkin_name="", created_by=None):
    """Cấp số mới (dùng chung cho bệnh nhân tự lấy và nhân viên bóc hộ)."""
    with transaction.atomic():
        Service.objects.select_for_update().get(pk=service.pk)  # khóa để không trùng số
        prefix = service.code
        n = Ticket.objects.filter(
            Q(code__regex=rf"^{re.escape(prefix)}[0-9]+$") | Q(code="", service=service),
            created_at__date=today(),
        ).count() + 1
        ahead = waiting_qs(service).count()
        est = predict_wait_minutes(service, ahead)
        suggested, _ = suggest_counter(service)
        ticket = Ticket.objects.create(
            number=n, code=f"{prefix}{n:03d}", service=service, patient=patient,
            walkin_name=walkin_name, created_by=created_by, description=description,
            ai_note=ai_note, queued_at=timezone.now(), ahead_at_issue=ahead,
            est_wait=est, suggested_counter=suggested,
        )
        TicketEvent.objects.create(
            ticket=ticket, from_status="", to_status="waiting", by=created_by or patient,
            note="Nhân viên bóc số hộ" if created_by else "Lấy số online",
        )
        return ticket


def request_ticket(patient, service_id=None, description=""):
    """Bệnh nhân lấy số: chọn dịch vụ thủ công hoặc để AI phân loại."""
    description = (description or "").strip()[:255]
    if Ticket.objects.filter(patient=patient, status__in=ACTIVE).exists():
        raise TicketError("Bạn đang có một số chưa hoàn tất.")
    ai_note = ""
    if service_id:
        service = Service.objects.filter(pk=service_id).first()
        if not service:
            raise TicketError("Dịch vụ không tồn tại.")
    else:
        service, ai_note, _ = classify_cached(description, str(patient.pk))
        if not service:
            raise TicketError("Hãy mô tả nhu cầu hoặc chọn dịch vụ thủ công.")
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=patient.pk)  # chống bấm 2 lần
        if Ticket.objects.filter(patient=patient, status__in=ACTIVE).exists():
            raise TicketError("Bạn đang có một số chưa hoàn tất.")
        return issue_ticket(service, description=description, ai_note=ai_note, patient=patient)


# =====================================================================
# TASK 2 - DỮ LIỆU ĐẨY QUA WEBSOCKET / API
# =====================================================================
def board_payload(user_id=None):
    t0 = today()
    serving_all = list(Ticket.objects.filter(status="serving")
                       .select_related("counter", "service"))
    by_counter = {t.counter_id: t for t in serving_all}
    by_service = defaultdict(list)
    for t in serving_all:
        by_service[t.service_id].append(
            {"label": t.label, "counter": t.counter.name if t.counter else ""})

    services, counters = [], []
    for s in Service.objects.prefetch_related("counters").order_by("id"):
        cs = list(s.counters.all())
        open_n = sum(1 for c in cs if c.is_open)
        waiting = waiting_qs(s).count()
        services.append({
            "id": s.id, "name": s.name, "code": s.code, "waiting": waiting,
            "serving": by_service.get(s.id, []), "open_counters": open_n,
            "est_wait": predict_wait_minutes(s, waiting),
        })
        for c in cs:
            cur = by_counter.get(c.id)
            counters.append({"id": c.id, "name": c.name, "service": s.code,
                             "is_open": c.is_open, "current": cur.label if cur else None})

    recent = list(Ticket.objects.filter(announced_at__date=t0, counter__isnull=False)
                  .select_related("counter").order_by("-announced_at")[:6])
    calls = [{"label": t.label, "counter": t.counter.name,
              "at": t.announced_at.isoformat(), "status": t.status} for t in recent]

    mine = None
    if user_id:
        t = (Ticket.objects.filter(patient_id=user_id, status__in=ACTIVE)
             .select_related("counter", "service", "suggested_counter").first())
        if t:
            ahead = people_ahead(t) if t.status == "waiting" else 0
            detail = (predict_wait_detail(t.service, ahead) if t.status == "waiting"
                      else {"minutes": 0, "method": ""})
            mine = {
                "id": t.pk, "label": t.label, "status": t.status, "service": t.service.name,
                "ahead": ahead, "est_wait": detail["minutes"], "method": detail["method"],
                "counter": t.counter.name if t.counter else "",
                "suggested_counter": t.suggested_counter.name if t.suggested_counter else "",
                "ai_note": t.ai_note,
            }
    return {"services": services, "counters": counters, "calls": calls, "mine": mine,
            "server_time": timezone.localtime().strftime("%H:%M:%S")}


# =====================================================================
# TASK 4 - THỐNG KÊ + TASK 5 - TÓM TẮT NGUYÊN NHÂN NGHẼN
# =====================================================================
def get_stats(days=14):
    since = timezone.now() - timedelta(days=days)
    tickets = list(Ticket.objects.filter(created_at__gte=since)
                   .select_related("service", "counter"))
    per_service = defaultdict(lambda: {"total": 0, "done": 0, "skipped": 0,
                                       "waits": [], "durations": [], "ratings": []})
    per_counter = defaultdict(lambda: {"served": 0, "durations": []})
    by_hour = defaultdict(lambda: {"count": 0, "waits": []})
    errors = []

    for t in tickets:
        s = per_service[t.service.name]
        s["total"] += 1
        hour = timezone.localtime(t.created_at).hour
        by_hour[hour]["count"] += 1
        if t.status == "skipped":
            s["skipped"] += 1
        if t.called_at and t.requeue_count == 0:
            wait = max((t.called_at - t.queued_at).total_seconds() / 60, 0)
            s["waits"].append(wait)
            by_hour[hour]["waits"].append(wait)
            if t.est_wait is not None:
                errors.append(t.est_wait - wait)
        if t.status == "done":
            s["done"] += 1
            if t.called_at and t.finished_at:
                dur = max((t.finished_at - t.called_at).total_seconds() / 60, 0)
                s["durations"].append(dur)
                if t.counter:
                    c = per_counter[t.counter.name]
                    c["served"] += 1
                    c["durations"].append(dur)
        if t.rating:
            s["ratings"].append(t.rating)

    def avg(x):
        return round(sum(x) / len(x), 1) if x else 0

    rows = [{"name": n, "total": v["total"], "done": v["done"], "skipped": v["skipped"],
             "avg_wait": avg(v["waits"]), "avg_service": avg(v["durations"]),
             "rating": avg(v["ratings"])} for n, v in per_service.items()]
    counter_rows = [{"name": n, "served": v["served"], "avg_service": avg(v["durations"])}
                    for n, v in sorted(per_counter.items())]
    hours = [{"hour": h, "count": by_hour[h]["count"], "avg_wait": avg(by_hour[h]["waits"])}
             for h in sorted(by_hour)]
    accuracy = None
    if errors:
        accuracy = {"n": len(errors),
                    "mae": round(sum(abs(e) for e in errors) / len(errors), 1),
                    "bias": round(sum(errors) / len(errors), 1)}

    surveys = list(Survey.objects.filter(created_at__gte=since))
    survey = None
    if surveys:
        survey = {"n": len(surveys),
                  "overall": avg([s.overall for s in surveys]),
                  "wait": avg([s.wait_rating for s in surveys]),
                  "staff": avg([s.staff_rating for s in surveys])}

    total = len(tickets)
    skipped = sum(r["skipped"] for r in rows)
    return {"days": days,
            "totals": {"tickets": total, "done": sum(r["done"] for r in rows),
                       "skipped": skipped,
                       "skip_rate": round(100 * skipped / total, 1) if total else 0},
            "rows": rows, "counters": counter_rows, "hours": hours,
            "accuracy": accuracy, "survey": survey}


def summarize_bottleneck(stats):
    if not stats["rows"]:
        return "Chưa có dữ liệu lịch sử để phân tích."
    reply = _ai(
        f"Bạn là chuyên gia vận hành bệnh viện. Dưới đây là thống kê {stats['days']} ngày qua "
        "(avg_wait = phút chờ trung bình, avg_service = phút phục vụ trung bình, "
        "hours = số lượt lấy số và phút chờ trung bình theo giờ, counters = số lượt từng quầy phục vụ).\n"
        f"{json.dumps(stats, ensure_ascii=False)}\n\n"
        "Hãy tóm tắt trong 4-5 câu tiếng Việt: nguyên nhân nghẽn chính, khung giờ cao điểm, "
        "quầy/dịch vụ có vấn đề và 2 đề xuất cải thiện cụ thể.",
        max_tokens=500,
    )
    if reply:
        return reply
    worst = max(stats["rows"], key=lambda r: r["avg_wait"])
    peak = max(stats["hours"], key=lambda h: h["count"]) if stats["hours"] else None
    text = f"Dịch vụ chờ lâu nhất: {worst['name']} (~{worst['avg_wait']} phút). "
    if peak:
        text += f"Cao điểm lúc {peak['hour']}h với {peak['count']} lượt. "
    text += "Đề xuất: mở thêm quầy vào giờ cao điểm và rút ngắn thời gian xử lý ở dịch vụ chậm nhất."
    return text + " (Bật GEMINI_API_KEY trong file .env để có phân tích AI chi tiết.)"