#!/usr/bin/env python3
"""
WhatsApp → Claude Code Daemon (Event-driven)

Receives webhook notifications from the Go WhatsApp bridge when Lucas sends a message.
Batches rapid messages (5s window), invokes Claude CLI, and sends the response back via WhatsApp REST API.
"""

import json
import logging
import subprocess
import threading
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests

# ─── Configuration ───────────────────────────────────────────────────────────

WEBHOOK_PORT = 9090
BRIDGE_API = "http://localhost:8080"
LUCAS_JID = "5528999301848@s.whatsapp.net"
BATCH_WINDOW_SECONDS = 5
SESSION_ID = str(uuid.uuid4())

SYSTEM_PROMPT = """You are Lucas's personal AI assistant, responding via WhatsApp.

RULES:
- Respond in the SAME LANGUAGE as the user's message (Portuguese if they write in Portuguese, English if in English)
- Be concise and direct — this is WhatsApp, not an essay
- Use casual/friendly tone matching WhatsApp chat style
- If the user asks you to do something on their computer, explain that you're running in a limited WhatsApp mode and suggest they use Claude Code directly
- You have access to the conversation history via --session-id, so you remember previous messages

TO SEND YOUR RESPONSE, you MUST use this exact curl command:
curl -s -X POST http://localhost:8080/api/send -H "Content-Type: application/json" -d '{"recipient": "5528999301848@s.whatsapp.net", "message": "YOUR_RESPONSE_HERE"}'

IMPORTANT:
- Always send your response using the curl command above. This is how you reply on WhatsApp.
- If your response is long, split it into multiple messages (multiple curl calls) for better readability on mobile.
- Escape special characters properly in the JSON string (double quotes, newlines, etc).
- Do NOT use markdown formatting — WhatsApp doesn't render it. Use plain text with *bold* and _italic_ only.
- After sending your response via curl, output "DONE" to indicate you've finished.
"""

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("daemon")

# ─── State ───────────────────────────────────────────────────────────────────

message_buffer: list[dict] = []
buffer_lock = threading.Lock()
buffer_timer: threading.Timer | None = None
claude_lock = threading.Lock()
claude_queue: list[dict] = []  # messages that arrive while Claude is processing


# ─── Claude Invocation ───────────────────────────────────────────────────────


def invoke_claude(messages: list[dict]):
    """Invoke Claude CLI with batched messages and let it respond via curl."""
    if not messages:
        return

    # Build the prompt from batched messages
    if len(messages) == 1:
        prompt = messages[0]["content"]
    else:
        lines = [f"- {m['content']}" for m in messages]
        prompt = "Multiple messages received:\n" + "\n".join(lines)

    log.info(f"Invoking Claude with: {prompt[:100]}...")

    cmd = [
        "claude",
        "-p",
        "--session-id", SESSION_ID,
        "--dangerously-skip-permissions",
        "--append-system-prompt", SYSTEM_PROMPT,
        prompt,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            cwd="/Users/savino",
        )
        log.info(f"Claude exit code: {result.returncode}")
        if result.stdout.strip():
            # Claude's stdout may contain the response text (after curl sends it)
            log.info(f"Claude stdout (last 200 chars): ...{result.stdout[-200:]}")
        if result.stderr.strip():
            log.warning(f"Claude stderr: {result.stderr[:500]}")

        # If Claude failed to send via curl, send error message as fallback
        if result.returncode != 0:
            send_whatsapp_message("⚠️ Claude encountered an error processing your message. Try again.")

    except subprocess.TimeoutExpired:
        log.error("Claude timed out after 120s")
        send_whatsapp_message("⚠️ Processing timed out. Try a simpler message.")
    except FileNotFoundError:
        log.error("Claude CLI not found! Make sure 'claude' is in PATH")
        send_whatsapp_message("⚠️ Claude CLI not available.")
    except Exception as e:
        log.error(f"Error invoking Claude: {e}")
        send_whatsapp_message(f"⚠️ Error: {e}")
    finally:
        # Check if there are queued messages that arrived while Claude was processing
        with buffer_lock:
            queued = list(claude_queue)
            claude_queue.clear()

        if queued:
            log.info(f"Processing {len(queued)} queued messages that arrived during Claude invocation")
            invoke_claude(queued)


def send_whatsapp_message(text: str):
    """Send a message back to Lucas via the bridge REST API."""
    try:
        resp = requests.post(
            f"{BRIDGE_API}/api/send",
            json={"recipient": LUCAS_JID, "message": text},
            timeout=10,
        )
        log.info(f"Sent fallback message (status {resp.status_code})")
    except Exception as e:
        log.error(f"Failed to send WhatsApp message: {e}")


# ─── Message Batching ────────────────────────────────────────────────────────


def flush_buffer():
    """Flush the message buffer and invoke Claude."""
    global buffer_timer

    with buffer_lock:
        messages = list(message_buffer)
        message_buffer.clear()
        buffer_timer = None

    if not messages:
        return

    # Try to acquire Claude lock (non-blocking)
    if claude_lock.acquire(blocking=False):
        try:
            invoke_claude(messages)
        finally:
            claude_lock.release()
    else:
        # Claude is busy, queue these messages
        log.info("Claude is busy, queuing messages for next round")
        with buffer_lock:
            claude_queue.extend(messages)


def add_message(msg: dict):
    """Add a message to the buffer and (re)start the batch timer."""
    global buffer_timer

    with buffer_lock:
        message_buffer.append(msg)
        log.info(f"Buffered message ({len(message_buffer)} in buffer): {msg['content'][:80]}")

        # Reset the timer
        if buffer_timer is not None:
            buffer_timer.cancel()

        buffer_timer = threading.Timer(BATCH_WINDOW_SECONDS, flush_buffer)
        buffer_timer.daemon = True
        buffer_timer.start()


# ─── HTTP Webhook Handler ────────────────────────────────────────────────────


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/webhook":
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Invalid JSON")
            return

        sender = data.get("sender", "")
        content = data.get("content", "")
        message_id = data.get("message_id", "")

        log.info(f"Webhook received: sender={sender}, content={content[:80]}")

        if content:
            add_message({
                "sender": sender,
                "content": content,
                "message_id": message_id,
                "timestamp": time.time(),
            })

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        # Suppress default HTTP request logging (we have our own)
        pass


# ─── Main ────────────────────────────────────────────────────────────────────


def main():
    log.info(f"Starting WhatsApp daemon on port {WEBHOOK_PORT}")
    log.info(f"Session ID: {SESSION_ID}")
    log.info(f"Batch window: {BATCH_WINDOW_SECONDS}s")
    log.info(f"Bridge API: {BRIDGE_API}")

    server = HTTPServer(("127.0.0.1", WEBHOOK_PORT), WebhookHandler)
    log.info(f"Webhook server listening on http://127.0.0.1:{WEBHOOK_PORT}/webhook")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down daemon...")
        server.shutdown()


if __name__ == "__main__":
    main()
