"""Mỗi khi phiếu / quầy / dịch vụ thay đổi, báo cho mọi trình duyệt đang mở WebSocket."""
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Counter, Service, Ticket

BOARD_GROUP = "board"


def notify_board():
    def _send():
        try:
            layer = get_channel_layer()
            if layer is not None:
                async_to_sync(layer.group_send)(BOARD_GROUP, {"type": "board.update"})
        except Exception as exc:  # không để lỗi realtime làm hỏng nghiệp vụ chính
            print("LOI REALTIME:", exc)

    # chỉ gửi SAU khi giao dịch DB đã commit, để client đọc được dữ liệu mới
    transaction.on_commit(_send)


@receiver([post_save, post_delete], sender=Ticket)
@receiver([post_save, post_delete], sender=Counter)
@receiver([post_save, post_delete], sender=Service)
def board_changed(sender, **kwargs):
    notify_board()