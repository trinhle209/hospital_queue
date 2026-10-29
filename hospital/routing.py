from django.urls import path

from .consumers import BoardConsumer

websocket_urlpatterns = [
    path("ws/board/", BoardConsumer.as_asgi()),
]