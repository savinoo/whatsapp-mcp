#!/usr/bin/env python3
"""
WhatsApp → Claude Code Daemon (Agentic Mode) - Evolution Complete

Receives webhook notifications from the Go WhatsApp bridge when Lucas sends a message.
Batches rapid messages (5s window), invokes Claude CLI with full computer access,
captures the output, and sends it back via the bridge REST API.

Features:
- Phase 1: /cancel + subprocess tracking
- Phase 2: Session continuity (UUID-based)
- Phase 3: Streaming + heartbeat
- Phase 4: SQLite persistence
- Phase 5: File sharing (auto-send modified files)
- Phase 6: /status + polish

Lucas sends a WhatsApp message → Claude works on his Mac → result sent back via WhatsApp.
"""

import json
import logging
import os
import signal
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import requests

# ─── Configuration ───────────────────────────────────────────────────────────

WEBHOOK_PORT = int(os.environ.get("DAEMON_PORT", "9090"))
BRIDGE_API = os.environ.get("BRIDGE_API", "http://localhost:8080")
LUCAS_JID = os.environ.get("WHATSAPP_JID", "5528999301848@s.whatsapp.net")
BATCH_WINDOW_SECONDS = int(os.environ.get("BATCH_WINDOW", "5"))
CLAUDE_TIMEOUT = os.environ.get("CLAUDE_TIMEOUT", "")  # empty = no timeout

SCRIPT_DIR = Path(__file__).parent.resolve()
DB_PATH = SCRIPT_DIR / "daemon.db"

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

Available slash commands (Lucas can use):
- /cancel → Cancel running Claude process
- /new → Start new conversation session
- /history → Show last 10 interactions
- /send <path> → Send file via WhatsApp
- /status → System info (uptime, disk space, session)
"""

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("daemon")

# ─── Global State ────────────────────────────────────────────────────────────

# Message batching
message_buffer: list[dict] = []
buffer_lock = threading.Lock()
buffer_timer: threading.Timer | None = None

# Claude invocation
claude_lock = threading.Lock()
claude_queue: list[dict] = []

# Active process tracking (Phase 1)
active_process: dict = {}  # {"proc": Popen, "stop_heartbeat": Event, "stop_typing": Event}
active_process_lock = threading.Lock()

# Session tracking (Phase 2)
current_session_id: str | None = None
session_message_count: int = 0
session_lock = threading.Lock()

# Daemon stats (Phase 6)
DAEMON_START_TIME = time.time()

# ─── Database (Phase 4) ──────────────────────────────────────────────────────


def init_database():
    """Initialize SQLite database with sessions and interactions tables."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            last_used_at TEXT NOT NULL,
            message_count INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS interactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            user_message TEXT NOT NULL,
            assistant_response TEXT,
            duration_seconds REAL,
            tool_use_count INTEGER DEFAULT 0,
            FOREIGN KEY (session_id) REFERENCES sessions(session_id)
        )
    """)

    conn.commit()
    conn.close()
    log.info(f"Database initialized at {DB_PATH}")


def cleanup_old_sessions():
    """Delete sessions older than 7 days."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    seven_days_ago = time.time() - (7 * 24 * 3600)
    timestamp_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(seven_days_ago))

    cursor.execute("DELETE FROM sessions WHERE last_used_at < ?", (timestamp_str,))
    deleted = cursor.rowcount

    conn.commit()
    conn.close()

    if deleted > 0:
        log.info(f"Cleaned up {deleted} old sessions")


def restore_last_session():
    """Restore the last active session on daemon startup."""
    global current_session_id, session_message_count

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT session_id, message_count FROM sessions
        WHERE is_active = 1
        ORDER BY last_used_at DESC
        LIMIT 1
    """)

    row = cursor.fetchone()
    conn.close()

    if row:
        current_session_id, session_message_count = row
        log.info(f"Restored session {current_session_id[:8]}... ({session_message_count} messages)")
    else:
        log.info("No active session found, will create new on first message")


def save_interaction(session_id: str, user_msg: str, assistant_resp: str,
                     duration: float, tool_count: int):
    """Save an interaction to the database."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute("""
        INSERT INTO interactions
        (session_id, timestamp, user_message, assistant_response, duration_seconds, tool_use_count)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (session_id, timestamp, user_msg, assistant_resp, duration, tool_count))

    # Update session
    cursor.execute("""
        UPDATE sessions
        SET last_used_at = ?, message_count = message_count + 1
        WHERE session_id = ?
    """, (timestamp, session_id))

    conn.commit()
    conn.close()


def create_new_session() -> str:
    """Create a new session and return its UUID."""
    session_id = str(uuid.uuid4())
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Deactivate all previous sessions
    cursor.execute("UPDATE sessions SET is_active = 0")

    # Create new session
    cursor.execute("""
        INSERT INTO sessions (session_id, created_at, last_used_at, message_count, is_active)
        VALUES (?, ?, ?, 0, 1)
    """, (session_id, timestamp, timestamp))

    conn.commit()
    conn.close()

    log.info(f"Created new session: {session_id[:8]}...")
    return session_id


def get_history(limit: int = 10) -> list[dict]:
    """Get last N interactions from the database."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT timestamp, user_message, assistant_response, duration_seconds, tool_use_count
        FROM interactions
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()
    conn.close()

    return [
        {
            "timestamp": r[0],
            "user_message": r[1],
            "assistant_response": r[2],
            "duration": r[3],
            "tool_count": r[4],
        }
        for r in reversed(rows)  # Show oldest first
    ]


def get_interactions_today() -> int:
    """Count interactions from today."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    today = time.strftime("%Y-%m-%d")
    cursor.execute("""
        SELECT COUNT(*) FROM interactions
        WHERE timestamp LIKE ?
    """, (f"{today}%",))

    count = cursor.fetchone()[0]
    conn.close()
    return count


# ─── WhatsApp Communication ──────────────────────────────────────────────────


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


def send_whatsapp_file(file_path: str, caption: str = ""):
    """Send a file via WhatsApp using the bridge API. (Phase 5)"""
    if not os.path.exists(file_path):
        log.error(f"File not found: {file_path}")
        return

    try:
        resp = requests.post(
            f"{BRIDGE_API}/api/send",
            json={
                "recipient": LUCAS_JID,
                "message": caption,
                "media_path": file_path,
            },
            timeout=60,
        )
        log.info(f"Sent file {file_path} (status {resp.status_code})")
    except Exception as e:
        log.error(f"Failed to send file {file_path}: {e}")


# ─── Streaming Reader (Phase 3) ──────────────────────────────────────────────


def read_stream_json(proc: subprocess.Popen, stop_heartbeat: threading.Event,
                     stop_typing: threading.Event,
                     tool_count_ref: dict) -> tuple[str, int, list[str]]:
    """
    Read stream-json output from Claude process.
    Updates tool_count_ref["count"] in real-time for heartbeat thread.
    Returns: (result_text, tool_use_count, modified_files)
    """
    result_text = ""
    tool_use_count = 0
    modified_files = []

    # Track accumulated tool input JSON for Write/Edit tools
    current_tool_name = None
    current_tool_input_chunks = []

    for line in iter(proc.stdout.readline, ""):
        if not line.strip():
            continue

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")

        # Track tool use start
        if event_type == "content_block_start":
            content_block = event.get("content_block", {})
            if content_block.get("type") == "tool_use":
                tool_use_count += 1
                tool_count_ref["count"] = tool_use_count
                current_tool_name = content_block.get("name")
                current_tool_input_chunks = []

        # Accumulate tool input JSON
        elif event_type == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "input_json_delta":
                current_tool_input_chunks.append(delta.get("partial_json", ""))

        # Tool use finished
        elif event_type == "content_block_stop":
            if current_tool_name in ("Write", "Edit", "write", "edit"):
                try:
                    full_input_json = "".join(current_tool_input_chunks)
                    tool_input = json.loads(full_input_json)
                    file_path = tool_input.get("file_path")
                    if file_path:
                        modified_files.append(file_path)
                        log.info(f"Detected modified file: {file_path}")
                except json.JSONDecodeError:
                    pass

            current_tool_name = None
            current_tool_input_chunks = []

        # Extract final result
        elif event_type == "result":
            result_text = event.get("result", "")

    # Stop heartbeat and typing threads
    stop_heartbeat.set()
    stop_typing.set()

    return result_text, tool_use_count, modified_files


def heartbeat_thread(stop_event: threading.Event, tool_count_ref: dict):
    """Send heartbeat messages every 30s while Claude is working. (Phase 3)"""
    while not stop_event.is_set():
        stop_event.wait(30)
        if not stop_event.is_set():
            count = tool_count_ref.get("count", 0)
            send_whatsapp_message(f"Trabalhando... ({count} tools usados)")


def typing_refresh_thread(stop_event: threading.Event):
    """Refresh typing indicator every 20s. (Phase 3)"""
    while not stop_event.is_set():
        stop_event.wait(20)
        if not stop_event.is_set():
            set_typing(True)


# ─── Claude Invocation (Phases 1-3-5) ───────────────────────────────────────


def invoke_claude(messages: list[dict]):
    """Invoke Claude CLI with streaming, heartbeat, and file sharing."""
    global current_session_id, session_message_count

    if not messages:
        return

    # Build the user message from batched messages
    if len(messages) == 1:
        user_msg = messages[0]["content"]
    else:
        lines = [f"- {m['content']}" for m in messages]
        user_msg = "Mensagens recebidas:\n" + "\n".join(lines)

    # Ensure we have a session (Phase 2)
    with session_lock:
        if current_session_id is None:
            current_session_id = create_new_session()
            session_message_count = 0

        session_id = current_session_id
        is_first_message = session_message_count == 0
        session_message_count += 1

    log.info(f"Invoking Claude (session {session_id[:8]}..., msg #{session_message_count}): {user_msg[:100]}...")
    set_typing(True)

    claude_bin = os.environ.get("CLAUDE_BIN", "/Users/savino/.local/bin/claude")

    # Build command with session arguments (Phase 2)
    cmd = [
        claude_bin,
        "-p",
        "--dangerously-skip-permissions",
        "--append-system-prompt", SYSTEM_PROMPT,
        "--output-format", "stream-json",  # Phase 3
    ]

    if is_first_message:
        cmd.extend(["--session-id", session_id])
    else:
        cmd.extend(["--resume", session_id])

    cmd.append(user_msg)

    start_time = time.time()

    try:
        # Use Popen for process control (Phase 1)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd="/Users/savino",
            start_new_session=True,  # For clean process group killing
        )

        # Track active process (Phase 1)
        stop_heartbeat = threading.Event()
        stop_typing = threading.Event()

        with active_process_lock:
            active_process["proc"] = proc
            active_process["stop_heartbeat"] = stop_heartbeat
            active_process["stop_typing"] = stop_typing

        # Start heartbeat and typing threads (Phase 3)
        tool_count_ref = {"count": 0}

        heartbeat_t = threading.Thread(
            target=heartbeat_thread,
            args=(stop_heartbeat, tool_count_ref),
            daemon=True
        )
        heartbeat_t.start()

        typing_t = threading.Thread(
            target=typing_refresh_thread,
            args=(stop_typing,),
            daemon=True
        )
        typing_t.start()

        # Read streaming output (Phase 3)
        result_text, tool_use_count, modified_files = read_stream_json(
            proc, stop_heartbeat, stop_typing, tool_count_ref
        )

        # Wait for process to finish
        proc.wait()

        duration = time.time() - start_time

        log.info(f"Claude finished (exit {proc.returncode}, {duration:.1f}s, {tool_use_count} tools)")

        # Clear active process
        with active_process_lock:
            active_process.clear()

        # Handle response
        if proc.returncode == 0 and result_text:
            log.info(f"Claude response ({len(result_text)} chars): {result_text[:150]}...")

            # Save interaction to DB (Phase 4)
            save_interaction(session_id, user_msg, result_text, duration, tool_use_count)

            # Send response (split if too long)
            if len(result_text) > 4000:
                chunks = [result_text[i:i+4000] for i in range(0, len(result_text), 4000)]
                for chunk in chunks:
                    send_whatsapp_message(chunk)
                    time.sleep(0.5)
            else:
                send_whatsapp_message(result_text)

            # Auto-send modified files (Phase 5)
            for file_path in modified_files:
                abs_path = file_path if os.path.isabs(file_path) else os.path.join("/Users/savino", file_path)
                if os.path.exists(abs_path):
                    file_size = os.path.getsize(abs_path)
                    ext = os.path.splitext(abs_path)[1].lower()

                    # Auto-send if <1MB and allowed extension
                    allowed_exts = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".py", ".md",
                                    ".txt", ".json", ".csv", ".html", ".css", ".js"}

                    if file_size < 1_000_000 and ext in allowed_exts:
                        log.info(f"Auto-sending modified file: {abs_path}")
                        send_whatsapp_file(abs_path, f"Arquivo modificado: {os.path.basename(abs_path)}")

        else:
            stderr = proc.stderr.read() if proc.stderr else ""
            log.error(f"Claude failed (exit {proc.returncode}): {stderr[:200]}")
            send_whatsapp_message("Erro ao processar mensagem. Tenta de novo!")

    except FileNotFoundError:
        log.error("Claude CLI not found in PATH")
        send_whatsapp_message("Claude CLI nao disponivel.")
    except Exception as e:
        log.error(f"Error invoking Claude: {e}", exc_info=True)
        send_whatsapp_message("Erro ao processar mensagem. Tenta de novo!")
    finally:
        set_typing(False)

        # Clear active process if still set
        with active_process_lock:
            active_process.clear()

        # Process queued messages that arrived while Claude was busy
        with buffer_lock:
            queued = list(claude_queue)
            claude_queue.clear()

        if queued:
            log.info(f"Processing {len(queued)} queued messages")
            invoke_claude(queued)


# ─── Slash Commands (Phases 1, 2, 4, 5, 6) ──────────────────────────────────


def handle_cancel_command():
    """/cancel - Kill the running Claude process."""
    with active_process_lock:
        proc = active_process.get("proc")

        if proc and proc.poll() is None:
            try:
                # Kill process group
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                log.info(f"Killed Claude process (PID {proc.pid})")
                send_whatsapp_message("Tarefa cancelada.")

                # Stop heartbeat and typing
                stop_heartbeat = active_process.get("stop_heartbeat")
                stop_typing = active_process.get("stop_typing")
                if stop_heartbeat:
                    stop_heartbeat.set()
                if stop_typing:
                    stop_typing.set()

                active_process.clear()
            except Exception as e:
                log.error(f"Failed to kill Claude process: {e}")
                send_whatsapp_message("Erro ao cancelar tarefa.")
        else:
            send_whatsapp_message("Nenhuma tarefa ativa para cancelar.")


def handle_new_command():
    """/new - Start a new conversation session."""
    global current_session_id, session_message_count

    with session_lock:
        current_session_id = create_new_session()
        session_message_count = 0

    send_whatsapp_message(f"Nova sessao iniciada ({current_session_id[:8]}...).")


def handle_history_command():
    """/history - Show last 10 interactions."""
    history = get_history(10)

    if not history:
        send_whatsapp_message("Nenhum historico encontrado.")
        return

    lines = ["*Historico (ultimas 10 interacoes):*\n"]
    for i, item in enumerate(history, 1):
        timestamp = item["timestamp"]
        user_msg = item["user_message"][:50] + "..." if len(item["user_message"]) > 50 else item["user_message"]
        duration = item["duration"] or 0
        tool_count = item["tool_count"] or 0

        lines.append(f"{i}. [{timestamp}] {user_msg}")
        lines.append(f"   → {duration:.1f}s, {tool_count} tools\n")

    send_whatsapp_message("\n".join(lines))


def handle_send_command(file_path: str):
    """/send <path> - Send a file explicitly (max 16MB)."""
    if not file_path:
        send_whatsapp_message("Uso: /send <caminho-do-arquivo>")
        return

    # Resolve path
    abs_path = file_path if os.path.isabs(file_path) else os.path.join("/Users/savino", file_path)

    if not os.path.exists(abs_path):
        send_whatsapp_message(f"Arquivo nao encontrado: {file_path}")
        return

    file_size = os.path.getsize(abs_path)
    if file_size > 16_000_000:
        send_whatsapp_message(f"Arquivo muito grande ({file_size / 1_000_000:.1f}MB). Max 16MB.")
        return

    log.info(f"Sending file via /send: {abs_path}")
    send_whatsapp_file(abs_path, f"Arquivo: {os.path.basename(abs_path)}")


def handle_status_command():
    """/status - Show system info (uptime, disk, session, interactions)."""
    # Uptime
    uptime_seconds = time.time() - DAEMON_START_TIME
    uptime_hours = int(uptime_seconds // 3600)
    uptime_minutes = int((uptime_seconds % 3600) // 60)
    uptime_str = f"{uptime_hours}h {uptime_minutes}m"

    # Disk space
    disk = shutil.disk_usage("/Users/savino")
    free_gb = disk.free / (1024 ** 3)

    # Session
    with session_lock:
        session_id_short = current_session_id[:8] + "..." if current_session_id else "Nenhuma"

    # Interactions today
    interactions_today = get_interactions_today()

    status_msg = f"""*Status do Daemon*

Uptime: {uptime_str}
Disco livre: {free_gb:.1f} GB
Sessao atual: {session_id_short}
Interacoes hoje: {interactions_today}
"""

    send_whatsapp_message(status_msg)


def handle_slash_command(content: str):
    """Process slash commands immediately (bypass batching)."""
    parts = content.split(maxsplit=1)
    command = parts[0].lower()

    if command == "/cancel":
        handle_cancel_command()
    elif command == "/new":
        handle_new_command()
    elif command == "/history":
        handle_history_command()
    elif command == "/send":
        file_path = parts[1] if len(parts) > 1 else ""
        handle_send_command(file_path)
    elif command == "/status":
        handle_status_command()
    else:
        send_whatsapp_message(f"Comando desconhecido: {command}\n\nDisponiveis: /cancel, /new, /history, /send, /status")


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
            # Check if it's a slash command (Phase 1)
            if content.startswith("/"):
                # Process immediately in separate thread
                threading.Thread(target=handle_slash_command, args=(content,), daemon=True).start()
            else:
                # Normal message - add to batch buffer
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


# ─── Health Check (Phase 6) ──────────────────────────────────────────────────


def check_bridge_health():
    """Check if the bridge API is reachable on startup."""
    try:
        resp = requests.get(f"{BRIDGE_API}/health", timeout=5)
        if resp.status_code == 200:
            log.info("Bridge API health check: OK")
        else:
            log.warning(f"Bridge API health check failed: status {resp.status_code}")
    except Exception as e:
        log.warning(f"Bridge API unreachable: {e}")


# ─── Main ────────────────────────────────────────────────────────────────────


def main():
    log.info(f"Starting WhatsApp daemon on port {WEBHOOK_PORT}")
    log.info(f"Batch window: {BATCH_WINDOW_SECONDS}s")
    log.info(f"Bridge API: {BRIDGE_API}")
    log.info(f"Database: {DB_PATH}")

    # Initialize database (Phase 4)
    init_database()
    cleanup_old_sessions()
    restore_last_session()

    # Health check (Phase 6)
    check_bridge_health()

    server = HTTPServer(("", WEBHOOK_PORT), WebhookHandler)
    log.info(f"Webhook server listening on http://0.0.0.0:{WEBHOOK_PORT}/webhook")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down daemon...")
        server.shutdown()


if __name__ == "__main__":
    main()
