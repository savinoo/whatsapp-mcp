#!/usr/bin/env python3
"""
WhatsApp → Claude Code Daemon (Agentic Mode)

Receives webhook notifications from the Go WhatsApp bridge when Lucas sends a message.
Batches rapid messages (5s window), invokes Claude CLI with full computer access,
captures the output, and sends it back via the bridge REST API.

Lucas sends a WhatsApp message → Claude works on his Mac → result sent back via WhatsApp.
"""

import json
import logging
import os
import subprocess
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests

# ─── Configuration ───────────────────────────────────────────────────────────

WEBHOOK_PORT = int(os.environ.get("DAEMON_PORT", "9090"))
BRIDGE_API = os.environ.get("BRIDGE_API", "http://localhost:8080")
LUCAS_JID = os.environ.get("WHATSAPP_JID", "5528999301848@s.whatsapp.net")
BATCH_WINDOW_SECONDS = int(os.environ.get("BATCH_WINDOW", "5"))
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "20"))
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "300"))

SYSTEM_PROMPT = """You are Lucas's agentic AI assistant with FULL ACCESS to his Mac computer. You are being invoked via WhatsApp.

You have access to all tools: Bash, Read, Write, Edit, Glob, Grep, etc. USE THEM to complete tasks.

RULES:
- Respond in the SAME LANGUAGE as the user's message (Portuguese if PT, English if EN)
- You CAN and SHOULD execute commands, edit files, run scripts, git operations, etc.
- Be concise in your WhatsApp reply — summarize what you DID, not what you COULD do
- Do NOT use markdown formatting — WhatsApp doesn't render it. Use plain text with *bold* and _italic_ only
- After completing a task, give a brief summary of what was done and the result
- If a task will take many steps, do them all and report the final result
- Working directory is /Users/savino — you have full access to all projects
- For dangerous/destructive operations (rm -rf, force push, etc.), do them if Lucas explicitly asked, but mention what you did
- If something fails, explain the error concisely and suggest a fix
- Keep responses short for WhatsApp (max 2-3 paragraphs). Be direct about results.
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
claude_queue: list[dict] = []
conversation_history: list[dict] = []  # [{role: "user"/"assistant", content: "..."}]


# ─── WhatsApp Send ───────────────────────────────────────────────────────────


def set_typing(composing: bool = True):
    """Set typing indicator (composing/paused) in the WhatsApp chat."""
    try:
        requests.post(
            f"{BRIDGE_API}/api/typing",
            json={"chat_jid": LUCAS_JID, "state": "composing" if composing else "paused"},
            timeout=5,
        )
    except Exception as e:
        log.warning(f"Failed to set typing indicator: {e}")


def send_whatsapp_message(text: str):
    """Send a message back to Lucas via the bridge REST API."""
    if not text.strip():
        return
    try:
        resp = requests.post(
            f"{BRIDGE_API}/api/send",
            json={"recipient": LUCAS_JID, "message": text},
            timeout=30,
        )
        log.info(f"Sent WhatsApp message (status {resp.status_code}, {len(text)} chars)")
    except Exception as e:
        log.error(f"Failed to send WhatsApp message: {e}")


# ─── Claude Invocation ───────────────────────────────────────────────────────


def invoke_claude(messages: list[dict]):
    """Invoke Claude CLI, capture text output, send it via WhatsApp."""
    if not messages:
        return

    # Build the user message from batched messages
    if len(messages) == 1:
        user_msg = messages[0]["content"]
    else:
        lines = [f"- {m['content']}" for m in messages]
        user_msg = "Mensagens recebidas:\n" + "\n".join(lines)

    # Add to conversation history
    conversation_history.append({"role": "user", "content": user_msg})

    # Build prompt with conversation context
    history_lines = []
    for entry in conversation_history[-MAX_HISTORY:]:
        prefix = "Lucas" if entry["role"] == "user" else "Assistente"
        history_lines.append(f"{prefix}: {entry['content']}")

    prompt = "\n".join(history_lines)

    log.info(f"Invoking Claude with: {user_msg[:100]}...")
    set_typing(True)

    claude_bin = os.environ.get("CLAUDE_BIN", "/Users/savino/.local/bin/claude")
    cmd = [
        claude_bin,
        "-p",
        "--dangerously-skip-permissions",
        "--append-system-prompt", SYSTEM_PROMPT,
        prompt,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT,
            cwd="/Users/savino",
        )
        log.info(f"Claude exit code: {result.returncode}")

        response = result.stdout.strip()

        if result.stderr.strip():
            log.warning(f"Claude stderr: {result.stderr[:300]}")

        if result.returncode == 0 and response:
            log.info(f"Claude response ({len(response)} chars): {response[:150]}...")
            conversation_history.append({"role": "assistant", "content": response})

            # Split long messages for WhatsApp readability (max ~4000 chars per message)
            if len(response) > 4000:
                chunks = [response[i:i+4000] for i in range(0, len(response), 4000)]
                for chunk in chunks:
                    send_whatsapp_message(chunk)
                    time.sleep(0.5)
            else:
                send_whatsapp_message(response)
        else:
            log.error(f"Claude failed (exit {result.returncode}): {result.stderr[:200]}")
            send_whatsapp_message("Erro ao processar mensagem. Tenta de novo!")

    except subprocess.TimeoutExpired:
        log.error(f"Claude timed out after {CLAUDE_TIMEOUT}s")
        send_whatsapp_message("Timeout processando mensagem. Tenta algo mais simples!")
    except FileNotFoundError:
        log.error("Claude CLI not found in PATH")
        send_whatsapp_message("Claude CLI nao disponivel.")
    except Exception as e:
        log.error(f"Error invoking Claude: {e}", exc_info=True)
        send_whatsapp_message("Erro ao processar mensagem. Tenta de novo!")
    finally:
        set_typing(False)
        # Process queued messages that arrived while Claude was busy
        with buffer_lock:
            queued = list(claude_queue)
            claude_queue.clear()

        if queued:
            log.info(f"Processing {len(queued)} queued messages")
            invoke_claude(queued)


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

    if claude_lock.acquire(blocking=False):
        try:
            invoke_claude(messages)
        finally:
            claude_lock.release()
    else:
        log.info("Claude is busy, queuing messages for next round")
        with buffer_lock:
            claude_queue.extend(messages)


def add_message(msg: dict):
    """Add a message to the buffer and (re)start the batch timer."""
    global buffer_timer

    with buffer_lock:
        message_buffer.append(msg)
        log.info(f"Buffered message ({len(message_buffer)} in buffer): {msg['content'][:80]}")

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
        pass


# ─── Main ────────────────────────────────────────────────────────────────────


def main():
    log.info(f"Starting WhatsApp daemon on port {WEBHOOK_PORT}")
    log.info(f"Batch window: {BATCH_WINDOW_SECONDS}s")
    log.info(f"Bridge API: {BRIDGE_API}")

    server = HTTPServer(("", WEBHOOK_PORT), WebhookHandler)
    log.info(f"Webhook server listening on http://0.0.0.0:{WEBHOOK_PORT}/webhook")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down daemon...")
        server.shutdown()


if __name__ == "__main__":
    main()
