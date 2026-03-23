import uuid
import time
import threading
import queue
import json
from typing import Any

from flask import Flask, request, jsonify, Response, stream_with_context
from werkzeug.serving import make_server
from loguru import logger


from pydantic import Field

from functools import wraps

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Base

class EndpointConfig(Base):
    enabled: bool = False
    api_key: str = ""
    host: str = "127.0.0.1"
    port: int = 8080
    allow_from: list[str] = Field(default_factory=lambda: ["*"])
    streaming: bool = True
    request_timeout: float = 60.0

class EndpointChannel(BaseChannel):
    """
    HTTP endpoint channel implementing an OpenAI /v1/responses interface.
    Simplified implementation using Flask's async support and thread-safe futures.

    This channel translates Responses API style POST requests into bus messages
    and waits for the corresponding outbound response to fulfill the HTTP request.
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
        self._response_queues: dict[str, queue.SimpleQueue] = {}
        self._responses_lock = threading.Lock()
        self._setup_routes()

    def _register_queue(self, chat_id: str) -> queue.SimpleQueue:
        q: queue.SimpleQueue = queue.SimpleQueue()
        with self._responses_lock:
            self._response_queues[chat_id] = q
        return q

    def _get_queue(self, chat_id: str) -> queue.SimpleQueue | None:
        with self._responses_lock:
            return self._response_queues.get(chat_id)

    def _pop_queue(self, chat_id: str) -> None:
        with self._responses_lock:
            self._response_queues.pop(chat_id, None)

    def _build_response_payload(self, chat_id: str, model: str, content: str, created_at: int) -> dict[str, Any]:
        return {
            "id": chat_id,
            "object": "response",
            "created_at": created_at,
            "model": model,
            "status": "completed",
            "output": [
                {
                    "id": f"msg_{uuid.uuid4().hex}",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": content
                        }
                    ]
                }
            ],
            "usage": {
                "total_tokens": -1
            }
        }

    def _sse_event(self, event: str, data: dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    def _setup_routes(self) -> None:
        """Register Flask routes."""

        def require_api_key(f):
            @wraps(f)
            async def decorated(*args, **kwargs):
                if self.config.api_key != "":
                    auth = request.headers.get("Authorization", "")
                    # Expect format: "Bearer <key>"
                    if not auth.startswith("Bearer "):
                        return jsonify({"error": "missing_authorization"}), 401
                    token = auth.split(" ", 1)[1].strip()
                    if not token:
                        return jsonify({"error": "invalid_token"}), 401
                    if token != self.config.api_key:
                        return jsonify({"error": "invalid_api_key"}), 403
                return await f(*args, **kwargs)
            return decorated

        @self.app.after_request
        def add_cors_headers(response):
            response.headers.add('Access-Control-Allow-Origin', '*')
            response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
            response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
            return response

        @self.app.route("/v1/responses", methods=["POST"])
        @require_api_key
        async def create_response():
            """
            OpenAI Responses API endpoint.

            Request Schema:
            {
                "model": "...",
                "input": [
                    {"type": "message", "role": "user", "content": [{"type": "text", "text": "..."}]}
                ],
                "instructions": "System instructions",
                "previous_response_id": "resp_...",
                "store": true,
                "metadata": {...}
            }
            """
            data = request.get_json()
            if not data:
                return jsonify({"error": "Missing request body"}), 400

            # Input is an array of items in Responses API
            input_items = data.get("input", [])
            instructions = data.get("instructions", "")

            content = ""
            if isinstance(input_items, list):
                # Extract text from the last user message item
                for item in reversed(input_items):
                    if item.get("type") == "message" and item.get("role") == "user":
                        item_content = item.get("content")
                        if isinstance(item_content, list):
                            content = " ".join([c.get("text", "") for c in item_content if c.get("type") == "text"])
                        elif isinstance(item_content, str):
                            content = item_content
                        break
            elif isinstance(input_items, str):
                # Handle simplified input string if provided
                content = input_items

            # Fallback to legacy 'messages' if 'input' is empty
            if not content and "messages" in data:
                for m in reversed(data["messages"]):
                    if m.get("role") == "user":
                        content = m.get("content", "")
                        break

            if not content:
                return jsonify({"error": "Invalid request: 'input' or 'messages' with user content is required"}), 400

            sender_id = data.get("user", "default_user")
            chat_id = data.get("previous_response_id", f"resp_{uuid.uuid4().hex}")

            if not self.is_allowed(sender_id):
                return jsonify({"error": "Access denied"}), 403

            stream = bool(data.get("stream", False))
            q = self._register_queue(chat_id)

            try:
                # Directly forward to the message bus via base class
                await self._handle_message(
                    sender_id=sender_id,
                    chat_id=chat_id,
                    content=content,
                    metadata={
                        "http_request": True,
                        "instructions": instructions,
                        "store": data.get("store", True),
                        "model": data.get("model"),
                        "stream": stream
                    }
                )

                timeout = float(getattr(self.config, "request_timeout", 60.0))
                model = data.get("model", "agent-v1")
                created_at = int(time.time())

                if stream:
                    def event_stream():
                        buffer = ""
                        try:
                            yield self._sse_event(
                                "response.created",
                                {
                                    "id": chat_id,
                                    "object": "response",
                                    "created_at": created_at,
                                    "model": model,
                                    "status": "in_progress"
                                }
                            )
                            while True:
                                try:
                                    item = q.get(timeout=timeout)
                                except queue.Empty:
                                    yield self._sse_event("response.error", {"error": "Response timeout"})
                                    break

                                item_type = item.get("type")
                                if item_type == "delta":
                                    delta = item.get("delta", "")
                                    buffer += delta
                                    yield self._sse_event(
                                        "response.output_text.delta",
                                        {"delta": delta, "response_id": chat_id}
                                    )
                                elif item_type == "end":
                                    meta = item.get("meta", {})
                                    if meta.get("_resuming"):
                                        continue
                                    payload = self._build_response_payload(chat_id, model, buffer, created_at)
                                    yield self._sse_event("response.completed", payload)
                                    break
                                elif item_type == "final":
                                    outbound_msg = item.get("msg")
                                    content_text = outbound_msg.content if outbound_msg else buffer
                                    payload = self._build_response_payload(chat_id, model, content_text, created_at)
                                    yield self._sse_event("response.completed", payload)
                                    break
                        finally:
                            self._pop_queue(chat_id)

                    headers = {
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no"
                    }
                    return Response(stream_with_context(event_stream()), mimetype="text/event-stream", headers=headers)

                # Non-streaming: aggregate deltas or await final message
                buffer = ""
                while True:
                    try:
                        item = q.get(timeout=timeout)
                    except queue.Empty:
                        return jsonify({"error": "Response timeout"}), 504

                    item_type = item.get("type")
                    if item_type == "delta":
                        buffer += item.get("delta", "")
                        continue
                    if item_type == "end":
                        meta = item.get("meta", {})
                        if meta.get("_resuming"):
                            continue
                        payload = self._build_response_payload(chat_id, model, buffer, created_at)
                        return jsonify(payload)
                    if item_type == "final":
                        outbound_msg = item.get("msg")
                        content_text = outbound_msg.content if outbound_msg else buffer
                        payload = self._build_response_payload(chat_id, model, content_text, created_at)
                        return jsonify(payload)

            finally:
                if not stream:
                    self._pop_queue(chat_id)

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
        """Fulfill the interface by delivering a final message to the queue."""
        q = self._get_queue(msg.chat_id)
        if q:
            q.put_nowait({"type": "final", "msg": msg})
            logger.debug(f"Enqueued final response for {msg.chat_id}")
        else:
            logger.warning(f"Received outbound message for unknown or expired chat_id: {msg.chat_id}")

    async def send_delta(self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None) -> None:
        """Deliver a streaming chunk to the pending response queue."""
        q = self._get_queue(chat_id)
        if not q:
            logger.warning(f"Received stream delta for unknown or expired chat_id: {chat_id}")
            return

        meta = metadata or {}
        if meta.get("_stream_end"):
            q.put_nowait({"type": "end", "meta": meta})
            logger.debug(f"Enqueued stream end for {chat_id}")
            return

        q.put_nowait({"type": "delta", "delta": delta, "meta": meta})
        logger.debug(f"Enqueued stream delta for {chat_id}")
