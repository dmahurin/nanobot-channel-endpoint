import uuid
import time
import threading
import concurrent.futures
from typing import Any

from flask import Flask, request, jsonify
from werkzeug.serving import make_server
from loguru import logger


from pydantic import Field

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Base

class EndpointConfig(Base):
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8080
    allow_from: list[str] = Field(default_factory=lambda: ["*"])

class EndpointChannel(BaseChannel):
    """
    HTTP endpoint channel implementing an OpenAI-compatible /v1/responses interface.
    Simplified implementation using Flask's async support and thread-safe futures.
    """

    name = "endpoint"
    display_name = "Endpoint"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return EndpointConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = EndpointConfig.model_validate(config)
        super().__init__(config, bus)
        self.app = Flask(__name__)
        self._server = None
        self._server_thread = None
        self._pending_responses: dict[str, concurrent.futures.Future] = {}
        self._setup_routes()

    def _setup_routes(self) -> None:
        """Register Flask routes."""

        @self.app.after_request
        def add_cors_headers(response):
            response.headers.add('Access-Control-Allow-Origin', '*')
            response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
            response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
            return response

        @self.app.route("/v1/responses", methods=["POST"])
        async def create_response():
            """
            OpenAI-like chat completion endpoint.
            """
            data = request.get_json()
            if not data or "messages" not in data:
                return jsonify({"error": "Invalid request"}), 400

            user_messages = [m for m in data["messages"] if m.get("role") == "user"]
            if not user_messages:
                return jsonify({"error": "No user message found"}), 400

            content = user_messages[-1].get("content", "")
            sender_id = data.get("user", "default_user")

            if not self.is_allowed(sender_id):
                return jsonify({"error": "Access denied"}), 403

            chat_id = str(uuid.uuid4())
            future = concurrent.futures.Future()
            self._pending_responses[chat_id] = future

            try:
                # Directly forward to the message bus via base class
                await self._handle_message(
                    sender_id=sender_id,
                    chat_id=chat_id,
                    content=content
                )

                # Wait for the response message (blocking the request thread is safe
                # here because werkzeug is running in threaded mode)
                timeout = getattr(self.config, "request_timeout", 60.0)
                try:
                    outbound_msg = future.result(timeout=timeout)
                except concurrent.futures.TimeoutError:
                    return jsonify({"error": "Response timeout"}), 504

                return jsonify({
                    "id": f"chatcmpl-{chat_id}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "choices": [{
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": outbound_msg.content
                        },
                        "finish_reason": "stop"
                    }]
                })

            finally:
                self._pending_responses.pop(chat_id, None)

    async def start(self) -> None:
        """Start the Flask server in a background thread."""
        host = getattr(self.config, "host", "0.0.0.0")
        port = getattr(self.config, "port", 8080)

        self._server = make_server(host, port, self.app, threaded=True)
        self._server_thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._server_thread.start()

        self._running = True
        logger.info(f"Endpoint channel running on http://{host}:{port}")

    async def stop(self) -> None:
        """Stop the Flask server."""
        if self._server:
            self._server.shutdown()
        if self._server_thread:
            self._server_thread.join(timeout=2.0)
        self._running = False
        logger.info("Endpoint channel stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """Fulfill the interface by resolving the pending future."""
        future = self._pending_responses.get(msg.chat_id)
        if future and not future.done():
            future.set_result(msg)
            logger.debug(f"Resolved response for {msg.chat_id}")
