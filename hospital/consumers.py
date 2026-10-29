from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from .services import board_payload
from .signals import BOARD_GROUP


class BoardConsumer(AsyncJsonWebsocketConsumer):
    """Một kết nối dùng chung cho bệnh nhân, nhân viên và màn hình TV.

    Mỗi khi dữ liệu đổi, server phát tín hiệu 'board.update'; từng kết nối tự tính
    lại dữ liệu của riêng mình (bệnh nhân thấy thêm phiếu của chính họ) rồi đẩy xuống.
    """

    async def connect(self):
        await self.channel_layer.group_add(BOARD_GROUP, self.channel_name)
        await self.accept()
        await self.push()

    async def disconnect(self, code):
        await self.channel_layer.group_discard(BOARD_GROUP, self.channel_name)

    async def receive_json(self, content, **kwargs):
        kind = content.get("type")
        if kind == "ping":
            await self.send_json({"type": "pong"})
        elif kind == "refresh":
            await self.push()

    async def board_update(self, event):
        await self.push()

    async def push(self):
        user = self.scope.get("user")
        user_id = user.pk if user is not None and user.is_authenticated else None
        data = await database_sync_to_async(board_payload)(user_id)
        await self.send_json({"type": "board", "data": data})