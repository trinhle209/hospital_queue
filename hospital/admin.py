from django.contrib import admin

from .models import Counter, Profile, Service, Survey, Ticket, TicketEvent


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "default_minutes")


@admin.register(Counter)
class CounterAdmin(admin.ModelAdmin):
    list_display = ("name", "service", "is_open")
    list_editable = ("is_open",)


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "counter")


class TicketEventInline(admin.TabularInline):
    model = TicketEvent
    extra = 0
    can_delete = False
    readonly_fields = ("from_status", "to_status", "by", "counter", "note", "created_at")


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ("label", "service", "status", "counter", "created_at")
    list_filter = ("status", "service")
    search_fields = ("code", "walkin_name", "patient__username")
    inlines = [TicketEventInline]


@admin.register(Survey)
class SurveyAdmin(admin.ModelAdmin):
    list_display = ("ticket", "overall", "wait_rating", "staff_rating", "created_at")