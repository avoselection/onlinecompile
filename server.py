import asyncio
import base64
import contextlib
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import signal
import socket
import sys
import tempfile
import time
from html import escape
from urllib.parse import quote
from asyncio.subprocess import PIPE, Process, create_subprocess_exec
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

try:
    import bcrypt
except ImportError:  # pragma: no cover - optional until dependency is installed
    bcrypt = None

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
import uvicorn

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PREFERRED_STATIC_DIR = os.path.join(BASE_DIR, "static")
STATIC_DIR = PREFERRED_STATIC_DIR if os.path.exists(os.path.join(PREFERRED_STATIC_DIR, "index.html")) else BASE_DIR
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DATA_DIR = os.path.join(BASE_DIR, "room_data")
INITIAL_DOC = """# onlinecompile

print("Hello, students!")
"""
HOST_RECONNECT_TIMEOUT_SECONDS = 5 * 60
AUTOSAVE_FILENAME = "__autosave__.py"
SESSION_STATE_FILENAME = "session_state.json"
DOWNLOAD_TTL_SECONDS = 5 * 60
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_CHAT_MESSAGE_CHARS = 1000
MAX_FILENAME_LENGTH = 120
WINDOWS_RESERVED_FILENAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

PYTHON_KEYWORDS = {"False", "None", "True", "and", "as", "assert", "async", "await", "break", "class", "continue",
    "def", "del", "elif", "else", "except", "finally", "for", "from", "global", "if", "import",
    "in", "is", "lambda", "nonlocal", "not", "or", "pass", "raise", "return", "try", "while", "with", "yield", "match", "case"}

app = FastAPI()
os.makedirs(DATA_DIR, exist_ok=True)


def utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get_local_access_urls(port: int) -> List[str]:
    urls = [f"http://127.0.0.1:{port}"]
    with contextlib.suppress(Exception):
        hostname = socket.gethostname()
        for family, _, _, _, sockaddr in socket.getaddrinfo(hostname, None, family=socket.AF_INET):
            host = sockaddr[0]
            if host and not host.startswith("127.") and host not in {url.split('//', 1)[1].split(':', 1)[0] for url in urls}:
                urls.append(f"http://{host}:{port}")
    return urls


def load_host_config() -> dict:
    default_config = {
        "hosts": [
            {
                "username": "HOST",
                "password": "Example1",
            }
        ]
    }

    if not os.path.exists(CONFIG_PATH):
        return default_config

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return default_config

    if not isinstance(data, dict):
        return default_config

    hosts = data.get("hosts")
    if isinstance(hosts, list):
        normalized: List[dict] = []
        for item in hosts:
            if not isinstance(item, dict):
                continue
            username = str(item.get("username") or "").strip()
            password = str(item.get("password") or "")
            password_hash = str(item.get("password_hash") or "")
            if not username:
                continue
            normalized_item = {"username": username}
            if password_hash:
                normalized_item["password_hash"] = password_hash
            elif password:
                normalized_item["password"] = password
            else:
                continue
            normalized.append(normalized_item)
        if normalized:
            return {"hosts": normalized}
    return default_config


def get_host_config() -> dict:
    return load_host_config()


def normalize_secret_for_bcrypt(secret: str) -> bytes:
    data = (secret or "").encode("utf-8")
    if len(data) <= 72:
        return data
    return base64.b64encode(hashlib.sha256(data).digest())


def hash_password_bcrypt(password: str) -> str:
    if bcrypt is None:
        raise RuntimeError("Модуль bcrypt не установлен. Добавьте зависимость и выполните poetry install.")
    return bcrypt.hashpw(normalize_secret_for_bcrypt(password), bcrypt.gensalt()).decode("utf-8")


def verify_password_bcrypt(password: str, password_hash: str) -> bool:
    normalized_hash = str(password_hash or "").strip()
    if bcrypt is None or not normalized_hash:
        return False
    hash_bytes = normalized_hash.encode("utf-8")
    if hash_bytes.startswith((b"$2y$", b"$2a$")):
        hash_bytes = b"$2b$" + hash_bytes[4:]
    try:
        return bcrypt.checkpw(normalize_secret_for_bcrypt(password), hash_bytes)
    except ValueError:
        return False




def secure_text_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(str(left or "").encode("utf-8"), str(right or "").encode("utf-8"))

def room_saved_dir(room_id: str) -> str:
    return os.path.join(DATA_DIR, sanitize_room_id(room_id))


def room_state_path(room_id: str) -> str:
    return os.path.join(room_saved_dir(room_id), SESSION_STATE_FILENAME)


def room_has_persisted_data(room_id: str) -> bool:
    saved_dir = room_saved_dir(room_id)
    if os.path.exists(room_state_path(room_id)):
        return True
    if not os.path.isdir(saved_dir):
        return False
    for root, _, files in os.walk(saved_dir):
        for name in files:
            if name.startswith('.') or name.endswith('.tmp'):
                continue
            return True
    return False




def format_role_label(role: str) -> str:
    return "Ведущий" if role == "host" else "Студент"


def room_last_saved_at(room_id: str) -> Optional[float]:
    path = room_state_path(room_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        value = str(data.get("last_saved_at") or "").strip()
        if not value:
            return os.path.getmtime(path)
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value).timestamp()
    except Exception:
        with contextlib.suppress(Exception):
            return os.path.getmtime(path)
        return None


def can_download_room(room_id: str) -> bool:
    room = sanitize_room_id(room_id)
    session = sessions.get(room)
    if session is not None:
        return True
    ts = room_last_saved_at(room)
    return ts is not None and (time.time() - ts) <= DOWNLOAD_TTL_SECONDS


def highlight_python_html(code_line: str) -> str:
    text = str(code_line or "")
    token_re = re.compile(r"(#[^\n]*|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|\b\d+(?:\.\d+)?\b|\b[A-Za-z_][A-Za-z0-9_]*\b)", re.DOTALL)
    parts = []
    pos = 0
    for match in token_re.finditer(text):
        start, end = match.span()
        if start > pos:
            parts.append(escape(text[pos:start]))
        token = match.group(0)
        cls = "token-name"
        if token.startswith("#"):
            cls = "token-comment"
        elif token[:1] in {'"', "'"}:
            cls = "token-string"
        elif re.fullmatch(r'\d+(?:\.\d+)?', token):
            cls = "token-number"
        elif token in PYTHON_KEYWORDS:
            cls = "token-keyword"
        parts.append(f'<span class="{cls}">{escape(token)}</span>')
        pos = end
    if pos < len(text):
        parts.append(escape(text[pos:]))
    return ''.join(parts)


def authenticate_host(username: str, password: str) -> Tuple[bool, str]:
    username = str(username or "").strip()
    password = str(password or "")
    if not username or not password:
        return False, "Введите логин и пароль преподавателя."

    host_config = get_host_config()
    for host in host_config.get("hosts", []):
        configured_username = str(host.get("username") or "").strip()
        if not configured_username or not secure_text_equal(username, configured_username):
            continue

        password_hash = str(host.get("password_hash") or "").strip()
        if password_hash:
            if bcrypt is None:
                return False, "Зависимость bcrypt не установлена. Выполните poetry install и перезапустите сервер."
            if verify_password_bcrypt(password, password_hash):
                return True, ""
            return False, "Неверный пароль преподавателя."

        legacy_password = str(host.get("password") or "")
        if legacy_password and secure_text_equal(password, legacy_password):
            return True, ""
        return False, "Неверный пароль преподавателя."

    return False, "Пользователь преподавателя не найден в config.json."


def sanitize_room_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", (value or "").strip())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return cleaned[:64] or "default"


def sanitize_filename(name: str) -> str:
    raw = str(name or "").strip().replace("\\", "/")
    base = os.path.basename(raw)
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip(" ._")
    if not base:
        base = "snippet"

    root, ext = os.path.splitext(base)
    if ext.lower() == ".py":
        suffix = ".py"
    else:
        root = base
        suffix = ".py"

    root = re.sub(r"_+", "_", root).strip(" ._") or "snippet"
    if root.upper() in WINDOWS_RESERVED_FILENAMES:
        root = f"{root}_file"

    max_root_len = max(1, MAX_FILENAME_LENGTH - len(suffix))
    return f"{root[:max_root_len]}{suffix}"


def utf8_size(value: str) -> int:
    return len(str(value or "").encode("utf-8"))


def sanitize_personal_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9А-Яа-яЁё _.-]", "", (name or "").strip())
    return cleaned[:60] or "Guest"


def check_syntax(code: str) -> Tuple[bool, str]:
    try:
        import ast
        ast.parse(code)
        return True, ""
    except SyntaxError as e:
        return False, f"{e.msg} (line {e.lineno}, col {e.offset})"


def make_preexec():
    if os.name != "posix":
        return None

    import resource

    def _limit():
        with contextlib.suppress(Exception):
            os.setsid()
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
            resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024 * 1024, 2 * 1024 * 1024))
        except Exception:
            pass

    return _limit


@dataclass
class Client:
    ws: WebSocket
    name: str
    role: str
    color: str
    id: str = field(default_factory=lambda: secrets.token_hex(8))
    region: Optional[Tuple[int, int]] = None
    can_edit: bool = False
    cursor_line: int = 1
    cursor_col: int = 1
    completed: bool = False
    latency_ms: Optional[int] = None
    access_grants: int = 0
    username: str = ""


class Session:
    def __init__(self, room_id: str):
        self.room_id = sanitize_room_id(room_id)
        self.clients: Dict[str, Client] = {}
        self.ip_map: Dict[str, str] = {}
        self.host_id: Optional[str] = None
        self.host_username: Optional[str] = None
        self.host_reconnect_deadline: Optional[float] = None
        self.host_reconnect_task: Optional[asyncio.Task] = None
        self.lock = asyncio.Lock()

        self.doc_text: str = INITIAL_DOC
        self.version: int = 1
        self.active_editor_id: Optional[str] = None
        self.files: Dict[str, str] = {"main.py": INITIAL_DOC}
        self.current_filename: str = "main.py"

        self.run_task: Optional[asyncio.Task] = None
        self.running_process: Optional[Process] = None
        self.running_tmpdir: Optional[str] = None
        self.stop_requested: bool = False

        self.saved_dir = os.path.join(DATA_DIR, self.room_id)
        self.students_dir = os.path.join(self.saved_dir, "students")
        self.reports_dir = os.path.join(self.saved_dir, "reports")
        os.makedirs(self.students_dir, exist_ok=True)
        os.makedirs(self.reports_dir, exist_ok=True)

        self.blame_by_file: Dict[str, List[dict]] = {}
        self.audit_log: List[dict] = []
        self.student_metrics: Dict[str, dict] = {}
        self.last_saved_at: Optional[str] = None

        self.colors_cycle = iter([
            "#f44336", "#e91e63", "#9c27b0", "#673ab7", "#3f51b5", "#2196f3",
            "#03a9f4", "#00bcd4", "#009688", "#4caf50", "#8bc34a", "#cddc39",
            "#ff9800", "#ff5722", "#795548", "#607d8b", "#16a085", "#27ae60",
            "#2980b9", "#8e44ad", "#2c3e50", "#e67e22", "#e74c3c", "#f1c40f",
            "#6c5ce7", "#00cec9", "#fd79a8", "#0984e3", "#00b894", "#e17055",
        ])
        self.load_state()

    def next_color(self) -> str:
        try:
            return next(self.colors_cycle)
        except StopIteration:
            return "#{:06x}".format(abs(hash((self.room_id, len(self.clients)))) % 0xFFFFFF)

    def session_state_path(self) -> str:
        return os.path.join(self.saved_dir, SESSION_STATE_FILENAME)

    def autosave_path(self) -> str:
        return os.path.join(self.saved_dir, AUTOSAVE_FILENAME)

    def line_count(self) -> int:
        return max(1, len(self.doc_text.splitlines()))

    def ensure_file_blame(self, filename: str, text: Optional[str] = None):
        filename = sanitize_filename(filename)
        src = self.files.get(filename, text if text is not None else self.doc_text)
        lines = src.splitlines() or [""]
        blame = self.blame_by_file.get(filename) or []
        while len(blame) < len(lines):
            blame.append({
                "line": len(blame) + 1,
                "author": "Система",
                "timestamp": utc_iso(),
                "access_grant_no": 0,
            })
        if len(blame) > len(lines):
            blame = blame[:len(lines)]
        for idx in range(len(lines)):
            blame[idx]["line"] = idx + 1
        self.blame_by_file[filename] = blame

    def replace_file_text(self, filename: str, text: str, author: Optional[Client] = None):
        filename = sanitize_filename(filename)
        content = str(text or "")
        self.files[filename] = content
        self.current_filename = filename
        self.doc_text = content

        lines = content.splitlines() or [""]
        author_name = author.name if author else "Система"
        access_grant_no = author.access_grants if author else 0
        self.blame_by_file[filename] = [
            {
                "line": index + 1,
                "author": author_name,
                "timestamp": utc_iso(),
                "access_grant_no": access_grant_no,
            }
            for index in range(len(lines))
        ]
        self.version += 1
        self.persist_state()

    def load_state(self):
        path = self.session_state_path()
        if not os.path.exists(path):
            self.ensure_file_blame(self.current_filename, self.doc_text)
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            self.ensure_file_blame(self.current_filename, self.doc_text)
            return

        if isinstance(data.get("files"), dict):
            self.files = {sanitize_filename(k): str(v) for k, v in data["files"].items()}
        self.current_filename = sanitize_filename(data.get("current_filename") or "main.py")
        if self.current_filename not in self.files:
            self.files[self.current_filename] = INITIAL_DOC
        self.doc_text = str(data.get("doc_text") or self.files.get(self.current_filename, INITIAL_DOC))
        try:
            self.version = max(1, int(data.get("version") or 1))
        except (TypeError, ValueError):
            self.version = 1
        self.host_username = str(data.get("host_username") or "") or None
        self.student_metrics = data.get("student_metrics") if isinstance(data.get("student_metrics"), dict) else {}
        self.audit_log = data.get("audit_log") if isinstance(data.get("audit_log"), list) else []
        self.blame_by_file = data.get("blame_by_file") if isinstance(data.get("blame_by_file"), dict) else {}
        self.last_saved_at = data.get("last_saved_at")
        self.ensure_file_blame(self.current_filename, self.doc_text)

    def persist_state(self):
        os.makedirs(self.saved_dir, exist_ok=True)
        payload = {
            "room_id": self.room_id,
            "version": self.version,
            "doc_text": self.doc_text,
            "current_filename": self.current_filename,
            "files": self.files,
            "student_metrics": self.student_metrics,
            "audit_log": self.audit_log[-500:],
            "blame_by_file": self.blame_by_file,
            "last_saved_at": utc_iso(),
            "host_username": self.host_username,
        }
        state_tmp = self.session_state_path() + ".tmp"
        autosave_tmp = self.autosave_path() + ".tmp"
        with open(state_tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(state_tmp, self.session_state_path())
        with open(autosave_tmp, "w", encoding="utf-8") as f:
            f.write(self.doc_text)
        os.replace(autosave_tmp, self.autosave_path())
        self.last_saved_at = payload["last_saved_at"]

    def upsert_student_metric(self, client: Client) -> dict:
        metrics = self.student_metrics.setdefault(client.name, {
            "name": client.name,
            "access_grants": 0,
            "last_edit_at": None,
        })
        metrics.pop("score", None)
        client.access_grants = int(metrics.get("access_grants") or 0)
        return metrics

    def list_clients(self) -> List[dict]:
        items: List[dict] = []
        for client in self.clients.values():
            metrics = self.upsert_student_metric(client) if client.role == "student" else {"access_grants": 0}
            items.append({
                "id": client.id,
                "name": client.name,
                "role": client.role,
                "role_label": format_role_label(client.role),
                "color": client.color,
                "can_edit": client.can_edit,
                "region": client.region,
                "is_active_editor": self.active_editor_id == client.id,
                "completed": client.completed,
                "latency_ms": client.latency_ms,
                "access_grants": metrics.get("access_grants", 0),
            })
        return items

    def index_to_linecol(self, text: str, idx: int) -> Tuple[int, int]:
        if idx <= 0:
            return 1, 1
        line = 1
        col = 1
        for ch in text[:idx]:
            if ch == "\n":
                line += 1
                col = 1
            else:
                col += 1
        return line, col

    def range_to_lines(self, text: str, start_idx: int, end_idx: int) -> Tuple[int, int]:
        start_line, _ = self.index_to_linecol(text, start_idx)
        end_line, _ = self.index_to_linecol(text, max(start_idx, end_idx - 1))
        return start_line, max(start_line, end_line)

    def mark_blame_lines(self, filename: str, text_before: str, start_idx: int, end_idx: int, insert_text: str, client: Client):
        filename = sanitize_filename(filename)
        before_lines = text_before.splitlines() or [""]
        new_text = text_before[:start_idx] + insert_text + text_before[end_idx:]
        after_lines = new_text.splitlines() or [""]
        blame = list(self.blame_by_file.get(filename) or [])
        while len(blame) < len(before_lines):
            blame.append({"line": len(blame) + 1, "author": "Система", "timestamp": utc_iso(), "access_grant_no": 0})

        start_line, end_line = self.range_to_lines(text_before, start_idx, end_idx)
        inserted_line_count = max(1, insert_text.count("\n") + 1 if insert_text else max(1, end_line - start_line + 1))
        new_end_line = start_line + inserted_line_count - 1

        replacement = [{
            "line": line_no,
            "author": client.name,
            "timestamp": utc_iso(),
            "access_grant_no": client.access_grants,
        } for line_no in range(start_line, new_end_line + 1)]

        blame[start_line - 1:end_line] = replacement
        while len(blame) < len(after_lines):
            blame.append({"line": len(blame) + 1, "author": client.name, "timestamp": utc_iso(), "access_grant_no": client.access_grants})
        blame = blame[:len(after_lines)]
        for i in range(len(blame)):
            blame[i]["line"] = i + 1
        self.blame_by_file[filename] = blame

    def build_blame_rows(self, filename: str) -> List[dict]:
        filename = sanitize_filename(filename)
        text = self.files.get(filename, "")
        lines = text.splitlines() or [""]
        self.ensure_file_blame(filename, text)
        blame = self.blame_by_file.get(filename, [])
        rows: List[dict] = []
        for idx, line_text in enumerate(lines):
            meta = blame[idx] if idx < len(blame) else {}
            rows.append({
                "line": idx + 1,
                "author": meta.get("author", "Неизвестно"),
                "edited_at": meta.get("timestamp"),
                "access_grant_no": meta.get("access_grant_no", 0),
                "text": line_text,
            })
        return rows

    def save_student_snapshot(self, client: Client):
        student_dir = os.path.join(self.students_dir, sanitize_personal_name(client.name))
        os.makedirs(student_dir, exist_ok=True)
        path = os.path.join(student_dir, sanitize_filename(self.current_filename))
        with contextlib.suppress(Exception):
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.doc_text)

    async def send_to(self, client_id: str, payload: dict):
        client = self.clients.get(client_id)
        if not client:
            return
        with contextlib.suppress(Exception):
            await client.ws.send_text(json.dumps(payload, ensure_ascii=False))

    async def broadcast(self, payload: dict, exclude: Optional[set] = None):
        exclude = exclude or set()
        message = json.dumps(payload, ensure_ascii=False)
        to_remove: List[str] = []
        for cid, client in list(self.clients.items()):
            if cid in exclude:
                continue
            try:
                await client.ws.send_text(message)
            except Exception:
                to_remove.append(cid)
        for cid in to_remove:
            self.clients.pop(cid, None)

    async def broadcast_participants(self):
        await self.broadcast({"type": "participants", "participants": self.list_clients()})

    async def set_current_file(self, filename: str):
        filename = sanitize_filename(filename)
        if filename not in self.files:
            self.files[filename] = ""
        self.current_filename = filename
        self.doc_text = self.files[filename]
        self.ensure_file_blame(filename, self.doc_text)
        self.version += 1
        self.persist_state()
        await self.broadcast({
            "type": "doc_full",
            "doc": {"text": self.doc_text, "version": self.version},
            "filename": self.current_filename,
        })
        await self.broadcast({"type": "files", "current": self.current_filename, "files": list(self.files.keys())})

    async def terminate_running_process(self):
        proc = self.running_process
        if not proc or proc.returncode is not None:
            return

        try:
            if os.name == "posix":
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            return
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()

        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=1.5)

    async def stop_running_code(self):
        self.stop_requested = True
        if self.run_task and not self.run_task.done():
            self.run_task.cancel()
        await self.terminate_running_process()

    async def close_room(self, reason: str):
        await self.stop_running_code()

        if self.run_task and not self.run_task.done():
            self.run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.run_task

        await self.broadcast({"type": "room_closed", "message": reason})
        for client in list(self.clients.values()):
            with contextlib.suppress(Exception):
                await client.ws.close()

        self.clients.clear()
        self.ip_map.clear()
        self.active_editor_id = None
        self.host_id = None
        self.persist_state()

        if self.running_tmpdir:
            shutil.rmtree(self.running_tmpdir, ignore_errors=True)
            self.running_tmpdir = None

    async def apply_patch(self, client_id: str, base_version: int, start: int, end: int, insert_text: str) -> Tuple[bool, str]:
        async with self.lock:
            if base_version != self.version:
                return False, "Версия документа изменилась. Нужна полная синхронизация."

            client = self.clients.get(client_id)
            if client is None:
                return False, "Неизвестный клиент."

            if start < 0 or end < start or end > len(self.doc_text):
                return False, "Некорректные границы патча."
            if start == end and not insert_text:
                return True, ""

            if client.role != "host":
                if not client.can_edit or self.active_editor_id != client_id:
                    return False, "Редактирование сейчас недоступно."
                if client.region is not None:
                    start_line, end_line = self.range_to_lines(self.doc_text, start, end)
                    region_start, region_end = client.region
                    if start_line < region_start or end_line > region_end:
                        return False, f"Редактирование вне разрешённого диапазона [{region_start}-{region_end}]."

            old_text = self.doc_text
            new_text = self.doc_text[:start] + insert_text + self.doc_text[end:]
            if utf8_size(new_text) > MAX_DOCUMENT_BYTES:
                return False, "Размер документа превышает 1 МБ."
            self.doc_text = new_text
            self.files[self.current_filename] = new_text
            self.version += 1

            completed_changed = False
            changed = start != end or bool(insert_text)
            if client.role == "student":
                metrics = self.upsert_student_metric(client)
                metrics["last_edit_at"] = utc_iso()
                if changed:
                    if not client.completed:
                        client.completed = True
                        completed_changed = True
                    self.mark_blame_lines(self.current_filename, old_text, start, end, insert_text, client)
                    self.save_student_snapshot(client)
            else:
                self.mark_blame_lines(self.current_filename, old_text, start, end, insert_text, client)

            self.audit_log.append({
                "at": utc_iso(),
                "event": "patch",
                "room": self.room_id,
                "file": self.current_filename,
                "client": client.name,
                "client_role": client.role,
                "start": start,
                "end": end,
                "insert_length": len(insert_text),
                "version": self.version,
            })
            self.persist_state()

            await self.broadcast({
                "type": "doc_update",
                "version": self.version,
                "patch": {"start": start, "end": end, "text": insert_text},
                "by": client.name,
                "by_id": client.id,
                "filename": self.current_filename,
            })

            if completed_changed:
                await self.broadcast_participants()

            return True, ""

    def register_access_grant(self, target: Client):
        metrics = self.upsert_student_metric(target)
        metrics["access_grants"] = int(metrics.get("access_grants") or 0) + 1
        target.access_grants = metrics["access_grants"]
        self.audit_log.append({
            "at": utc_iso(),
            "event": "grant_edit",
            "room": self.room_id,
            "student": target.name,
            "access_grants": metrics["access_grants"],
        })
        self.persist_state()

    def access_rows(self) -> List[dict]:
        rows = []
        for name, metrics in sorted(self.student_metrics.items(), key=lambda x: (-int(x[1].get("access_grants") or 0), x[0])):
            rows.append({
                "student": name,
                "access_grants": int(metrics.get("access_grants") or 0),
                "last_edit_at": metrics.get("last_edit_at"),
            })
        return rows

    async def schedule_host_expiration(self):
        if self.host_reconnect_task and not self.host_reconnect_task.done():
            self.host_reconnect_task.cancel()
        self.host_reconnect_deadline = time.time() + HOST_RECONNECT_TIMEOUT_SECONDS

        async def _runner():
            try:
                await asyncio.sleep(HOST_RECONNECT_TIMEOUT_SECONDS)
                if self.host_id is None and not any(c.role == "host" for c in self.clients.values()):
                    await self.close_room("Преподаватель не восстановил соединение в течение 5 минут. Комната закрыта.")
                    sessions.pop(self.room_id, None)
            except asyncio.CancelledError:
                return

        self.host_reconnect_task = asyncio.create_task(_runner())
        deadline_human = datetime.fromtimestamp(self.host_reconnect_deadline).strftime("%H:%M:%S")
        await self.broadcast({
            "type": "host_disconnected",
            "message": "Преподаватель временно отключился. Сессия сохранена и ожидает его возвращения в течение 5 минут.",
            "deadline_human": deadline_human,
        })

    async def restore_host(self):
        had_reconnect_window = self.host_reconnect_deadline is not None or (
            self.host_reconnect_task is not None and not self.host_reconnect_task.done()
        )
        if self.host_reconnect_task and not self.host_reconnect_task.done():
            self.host_reconnect_task.cancel()
        self.host_reconnect_deadline = None
        if had_reconnect_window:
            await self.broadcast({"type": "host_restored"})


sessions: Dict[str, Session] = {}


def can_resume_existing_host_session(session: Optional[Session], username: str) -> bool:
    if session is None:
        return False
    if session.host_id and session.host_id in session.clients:
        return False
    if session.host_reconnect_deadline is None:
        return False
    return not session.host_username or secure_text_equal(session.host_username, username)


async def stream_pipe(session: Session, stream: Optional[asyncio.StreamReader], stream_name: str):
    if stream is None:
        return
    while True:
        chunk = await stream.read(1024)
        if not chunk:
            break
        text = chunk.decode("utf-8", "replace")
        await session.broadcast({"type": "run_output", "stream": stream_name, "text": text})


async def run_python_streaming(session: Session, code: str, timeout_s: float):
    tmpdir = tempfile.mkdtemp(prefix=f"runpy_{session.room_id}_")
    filename = sanitize_filename(session.current_filename)
    path = os.path.join(tmpdir, filename)

    with open(path, "w", encoding="utf-8") as f:
        f.write(code)

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    session.running_tmpdir = tmpdir
    session.stop_requested = False

    await session.broadcast({"type": "run_state", "running": True, "filename": filename, "clear": True})

    start_ts = time.perf_counter()
    stdout_task: Optional[asyncio.Task] = None
    stderr_task: Optional[asyncio.Task] = None

    try:
        proc = await create_subprocess_exec(
            sys.executable,
            "-I",
            "-u",
            path,
            cwd=tmpdir,
            stdout=PIPE,
            stderr=PIPE,
            env=env,
            preexec_fn=make_preexec(),
        )
        session.running_process = proc

        stdout_task = asyncio.create_task(stream_pipe(session, proc.stdout, "stdout"))
        stderr_task = asyncio.create_task(stream_pipe(session, proc.stderr, "stderr"))

        await asyncio.wait_for(proc.wait(), timeout=timeout_s)
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

        elapsed_ms = int((time.perf_counter() - start_ts) * 1000)
        await session.broadcast({"type": "run_result", "ok": proc.returncode == 0, "timeout": False, "returncode": proc.returncode, "elapsed_ms": elapsed_ms})
    except asyncio.TimeoutError:
        await session.terminate_running_process()
        elapsed_ms = int((time.perf_counter() - start_ts) * 1000)
        await session.broadcast({"type": "run_output", "stream": "stderr", "text": "\n[Timed out]\n"})
        await session.broadcast({"type": "run_result", "ok": False, "timeout": True, "returncode": None, "elapsed_ms": elapsed_ms})
    except asyncio.CancelledError:
        await session.terminate_running_process()
        await session.broadcast({"type": "run_output", "stream": "stderr", "text": "\n[Execution stopped]\n"})
        await session.broadcast({"type": "run_result", "ok": False, "timeout": False, "returncode": None, "elapsed_ms": int((time.perf_counter() - start_ts) * 1000)})
        raise
    except Exception as e:
        await session.broadcast({"type": "run_output", "stream": "stderr", "text": f"\n[Runner error] {e}\n"})
        await session.broadcast({"type": "run_result", "ok": False, "timeout": False, "returncode": None, "elapsed_ms": int((time.perf_counter() - start_ts) * 1000)})
    finally:
        session.stop_requested = False
        session.running_process = None
        session.run_task = None
        if session.running_tmpdir:
            shutil.rmtree(session.running_tmpdir, ignore_errors=True)
            session.running_tmpdir = None
        await session.broadcast({"type": "run_state", "running": False, "filename": filename, "clear": False})


def render_blame_html(room: str, filename: str, rows: List[dict]) -> str:
    room_safe = escape(room)
    filename_safe = escape(filename)
    csv_href = f"/api/rooms/{quote(room, safe='')}/reports/blame?filename={quote(filename, safe='')}&format=csv"
    json_href = f"/api/rooms/{quote(room, safe='')}/reports/blame?filename={quote(filename, safe='')}&format=json"
    body_rows = "\n".join(
        f'<tr><td>{row["line"]}</td><td>{escape(str(row["author"]))}</td><td>{escape(str(row.get("edited_at") or ""))}</td><td>{int(row.get("access_grant_no", 0))}</td><td><pre class="code-cell">{highlight_python_html(str(row["text"]))}</pre></td></tr>'
        for row in rows
    )
    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>Blame report</title>
<style>body{{font-family:Inter,Arial,sans-serif;margin:24px}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #d0d7e2;padding:8px;vertical-align:top}}th{{background:#eef4ff}}pre{{margin:0;white-space:pre-wrap;font-family:Consolas,monospace}}.code-cell{{background:#0f172a;color:#e5eefc;border-radius:10px;padding:10px 12px}}.token-keyword{{color:#93c5fd;font-weight:700}}.token-string{{color:#86efac}}.token-comment{{color:#94a3b8;font-style:italic}}.token-number{{color:#fca5a5}}.token-name{{color:#e5eefc}}.report-toolbar{{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0 20px}}.report-toolbar a{{display:inline-flex;align-items:center;min-height:40px;padding:0 14px;border-radius:12px;border:1px solid #d7e2f2;background:#eef4ff;color:#275efe;text-decoration:none;font-weight:600}}.report-note{{color:#5c6d89;margin:0 0 12px}}</style>
</head><body class="report-page">
<h1>Blame-отчёт по файлу {filename_safe}</h1>
<p>Комната: {room_safe}</p>
<p class="report-note">Открыта отдельная страница отчёта. Отсюда можно просмотреть содержимое документа и скачать эти данные в формате CSV.</p>
<div class="report-toolbar"><a href="{csv_href}">Скачать CSV</a><a href="{json_href}" target="_blank" rel="noopener">Открыть JSON</a></div>
<table><thead><tr><th>Строка</th><th>Автор последнего изменения</th><th>Время</th><th>№ выдачи доступа</th><th>Содержимое строки</th></tr></thead><tbody>{body_rows}</tbody></table>
</body></html>"""


def render_access_html(room: str, rows: List[dict]) -> str:
    room_safe = escape(room)
    csv_href = f"/api/rooms/{quote(room, safe='')}/reports/access?format=csv"
    json_href = f"/api/rooms/{quote(room, safe='')}/reports/access?format=json"
    body_rows = "\n".join(
        f"<tr><td>{escape(str(row['student']))}</td><td>{int(row['access_grants'])}</td><td>{escape(str(row.get('last_edit_at') or ''))}</td></tr>"
        for row in rows
    )
    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>Access report</title>
<style>body{{font-family:Inter,Arial,sans-serif;margin:24px}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #d0d7e2;padding:8px}}th{{background:#eef4ff}}.report-toolbar{{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0 20px}}.report-toolbar a{{display:inline-flex;align-items:center;min-height:40px;padding:0 14px;border-radius:12px;border:1px solid #d7e2f2;background:#eef4ff;color:#275efe;text-decoration:none;font-weight:600}}.report-note{{color:#5c6d89;margin:0 0 12px}}</style>
</head><body class="report-page">
<h1>Отчёт по выдачам доступа</h1>
<p>Комната: {room_safe}</p>
<p class="report-note">Открыта отдельная страница отчёта. Отсюда можно просмотреть данные по выдачам доступа и скачать таблицу в формате CSV.</p>
<div class="report-toolbar"><a href="{csv_href}">Скачать CSV</a><a href="{json_href}" target="_blank" rel="noopener">Открыть JSON</a></div>
<table><thead><tr><th>Студент</th><th>Количество выдач доступа</th><th>Последнее редактирование</th></tr></thead><tbody>{body_rows}</tbody></table>
</body></html>"""


@app.get("/")
@app.get("/onlinecompile")
async def index(_: Request):
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/health")
async def health():
    return JSONResponse({"ok": True, "sessions": len(sessions)})


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/api/rooms/{room_id}/download")
async def download_current_file(room_id: str, filename: str = "main.py"):
    room = sanitize_room_id(room_id)
    safe_filename = sanitize_filename(filename)
    if not can_download_room(room):
        return JSONResponse({"ok": False, "message": "Время скачивания истекло. Повторно откройте комнату."}, status_code=410)
    session = sessions.get(room) or Session(room)
    content = str(session.files.get(safe_filename, session.doc_text if session.current_filename == safe_filename else ""))
    if safe_filename not in session.files and not content:
        return JSONResponse({"ok": False, "message": "Файл не найден."}, status_code=404)
    return Response(content=content, media_type="text/x-python; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{safe_filename}"'})


@app.get("/api/rooms/{room_id}/reports/blame")
async def blame_report(room_id: str, filename: str = "main.py", format: str = "json"):
    room = sanitize_room_id(room_id)
    session = sessions.get(room) or Session(room)
    rows = session.build_blame_rows(filename)

    if format == "html":
        return HTMLResponse(render_blame_html(room, sanitize_filename(filename), rows), media_type="text/html; charset=utf-8")
    if format == "csv":
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=["line", "author", "edited_at", "access_grant_no", "text"])
        writer.writeheader()
        writer.writerows(rows)
        return Response(content=output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{room}_{sanitize_filename(filename)}_blame.csv"'})
    return JSONResponse({"room": room, "filename": sanitize_filename(filename), "rows": rows})


@app.get("/api/rooms/{room_id}/reports/access")
@app.get("/api/rooms/{room_id}/reports/scores")
async def access_report(room_id: str, format: str = "json"):
    room = sanitize_room_id(room_id)
    session = sessions.get(room) or Session(room)
    rows = session.access_rows()

    if format == "html":
        return HTMLResponse(render_access_html(room, rows), media_type="text/html; charset=utf-8")
    if format == "csv":
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=["student", "access_grants", "last_edit_at"])
        writer.writeheader()
        writer.writerows(rows)
        return Response(content=output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{room}_access_report.csv"'})
    return JSONResponse({"room": room, "rows": rows})


async def read_lsp_message(stream: asyncio.StreamReader) -> Optional[dict]:
    headers = {}
    while True:
        line = await stream.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        raw = line.decode("utf-8", "replace").strip()
        if ":" in raw:
            key, value = raw.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length") or 0)
    if length <= 0:
        return None
    body = await stream.readexactly(length)
    return json.loads(body.decode("utf-8", "replace"))


async def write_lsp_message(writer, payload: dict):
    body = json.dumps(payload).encode("utf-8")
    data = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
    if hasattr(writer, "write"):
        writer.write(data)
        if hasattr(writer, "drain"):
            await writer.drain()
    else:
        await writer.send_bytes(data)


@app.websocket("/lsp/{room_id}/{filename}")
async def lsp_proxy(ws: WebSocket, room_id: str, filename: str):
    await ws.accept()
    session = sessions.get(sanitize_room_id(room_id))
    document_uri = f"file:///{sanitize_filename(filename)}"

    try:
        proc = await create_subprocess_exec(
            "pyright-langserver",
            "--stdio",
            stdin=PIPE,
            stdout=PIPE,
            stderr=PIPE,
            cwd=session.saved_dir if session else BASE_DIR,
        )
    except FileNotFoundError:
        await ws.send_text(json.dumps({
            "jsonrpc": "2.0",
            "method": "window/logMessage",
            "params": {"type": 1, "message": "pyright-langserver не установлен на сервере. Установите пакет pyright."},
        }, ensure_ascii=False))
        await ws.close()
        return

    async def ws_to_proc():
        while True:
            message = await ws.receive_text()
            payload = json.loads(message)
            if payload.get("method") == "initialize":
                payload.setdefault("params", {})
                payload["params"].setdefault("rootUri", f"file:///{sanitize_room_id(room_id)}")
                payload["params"].setdefault("workspaceFolders", [{"uri": f"file:///{sanitize_room_id(room_id)}", "name": sanitize_room_id(room_id)}])
                payload["params"].setdefault("initializationOptions", {"python": {"pythonPath": "python3"}})
            if payload.get("method") == "textDocument/didOpen":
                payload.setdefault("params", {}).setdefault("textDocument", {}).setdefault("uri", document_uri)
            await write_lsp_message(proc.stdin, payload)

    async def proc_to_ws():
        while True:
            payload = await read_lsp_message(proc.stdout)
            if payload is None:
                break
            await ws.send_text(json.dumps(payload, ensure_ascii=False))

    tasks = [asyncio.create_task(ws_to_proc()), asyncio.create_task(proc_to_ws())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            await ws.close()


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()

    session: Optional[Session] = None
    client: Optional[Client] = None
    client_ip = ws.client.host if ws.client else "127.0.0.1"

    try:
        raw = await ws.receive_text()
        hello = json.loads(raw)

        if hello.get("type") != "hello":
            await ws.send_text(json.dumps({"type": "auth_error", "message": "Ожидалось приветственное сообщение hello."}, ensure_ascii=False))
            await ws.close()
            return

        role = "host" if hello.get("role") == "host" else "student"
        name = sanitize_personal_name(hello.get("name") or ("Ведущий" if role == "host" else "Student"))
        room = sanitize_room_id(hello.get("room") or "default")
        room_action = (hello.get("room_action") or ("join" if role == "student" else "create")).lower()

        if role == "host":
            username = str(hello.get("username") or "").strip()
            password = str(hello.get("password") or "")

            auth_ok, auth_message = authenticate_host(username, password)
            if not auth_ok:
                await ws.send_text(json.dumps({"type": "auth_error", "message": auth_message}, ensure_ascii=False))
                await ws.close()
                return

            existing = sessions.get(room)
            if room_action == "join":
                if existing is None:
                    if room_has_persisted_data(room):
                        existing = Session(room)
                        sessions[room] = existing
                    else:
                        await ws.send_text(json.dumps({"type": "auth_error", "message": f"Комната '{room}' недоступна."}, ensure_ascii=False))
                        await ws.close()
                        return
                if existing.host_id and existing.host_id in existing.clients:
                    await ws.send_text(json.dumps({"type": "auth_error", "message": "В комнате уже есть активный ведущий."}, ensure_ascii=False))
                    await ws.close()
                    return
                session = existing
            else:
                if existing is not None:
                    if can_resume_existing_host_session(existing, username):
                        session = existing
                    else:
                        await ws.send_text(json.dumps({"type": "auth_error", "message": f"Комната '{room}' уже существует."}, ensure_ascii=False))
                        await ws.close()
                        return
                elif room_has_persisted_data(room):
                    await ws.send_text(json.dumps({"type": "auth_error", "message": f"Комната '{room}' уже существует в сохранённых данных."}, ensure_ascii=False))
                    await ws.close()
                    return
                else:
                    session = Session(room)
                    sessions[room] = session

            session.host_username = username
        else:
            session = sessions.get(room)
            if session is None and room_has_persisted_data(room):
                session = Session(room)
                sessions[room] = session
            if session is None or (session.host_id is None and session.host_reconnect_deadline is None and not room_has_persisted_data(room)):
                await ws.send_text(json.dumps({"type": "auth_error", "message": f"Комната '{room}' недоступна."}, ensure_ascii=False))
                await ws.close()
                return
            if client_ip in session.ip_map:
                await ws.send_text(json.dumps({"type": "auth_error", "message": "С одного устройства разрешён только один студент."}, ensure_ascii=False))
                await ws.close()
                return

        color = session.next_color()
        client = Client(ws=ws, name=name, role=role, color=color, can_edit=(role == "host"))
        client.username = str(hello.get("username") or "")
        session.clients[client.id] = client

        if role == "host":
            session.host_id = client.id
            await session.restore_host()
        else:
            session.ip_map[client_ip] = client.id
            session.upsert_student_metric(client)

        await client.ws.send_text(json.dumps({
            "type": "welcome",
            "you": {"id": client.id, "name": client.name, "role": client.role, "color": client.color},
            "room": session.room_id,
            "doc": {"text": session.doc_text, "version": session.version},
            "filename": session.current_filename,
            "files": list(session.files.keys()),
            "participants": session.list_clients(),
            "active_editor_id": session.active_editor_id,
        }, ensure_ascii=False))

        await session.broadcast_participants()

        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "cursor":
                if client.role != "host" and (not client.can_edit or session.active_editor_id != client.id):
                    continue
                client.cursor_line = max(1, int(msg.get("line", 1)))
                client.cursor_col = max(1, int(msg.get("col", 1)))
                await session.broadcast({
                    "type": "cursor",
                    "id": client.id,
                    "name": client.name,
                    "color": client.color,
                    "line": client.cursor_line,
                    "col": client.cursor_col,
                }, exclude={client.id})

            elif msg_type == "request_full":
                await client.ws.send_text(json.dumps({
                    "type": "doc_full",
                    "doc": {"text": session.doc_text, "version": session.version},
                    "filename": session.current_filename,
                }, ensure_ascii=False))

            elif msg_type == "patch":
                ok, error = await session.apply_patch(
                    client.id,
                    int(msg.get("baseVersion", 0)),
                    int(msg.get("start", 0)),
                    int(msg.get("end", 0)),
                    str(msg.get("text", "")),
                )
                if not ok:
                    await client.ws.send_text(json.dumps({"type": "error", "message": error}, ensure_ascii=False))

            elif msg_type == "grant_edit":
                if client.role != "host":
                    continue
                target_id = msg.get("target_id")
                if session.active_editor_id and session.active_editor_id in session.clients:
                    session.clients[session.active_editor_id].can_edit = False
                session.active_editor_id = None
                if target_id in session.clients and session.clients[target_id].role == "student":
                    session.active_editor_id = target_id
                    target = session.clients[target_id]
                    target.can_edit = True
                    session.register_access_grant(target)
                await session.broadcast_participants()

            elif msg_type == "revoke_edit":
                if client.role != "host":
                    continue
                target_id = msg.get("target_id")
                if target_id in session.clients:
                    session.clients[target_id].can_edit = False
                    if session.active_editor_id == target_id:
                        session.active_editor_id = None
                await session.broadcast_participants()

            elif msg_type == "set_region":
                if client.role != "host":
                    continue
                target_id = msg.get("target_id")
                start_line = max(1, int(msg.get("start_line", 1)))
                end_line = max(1, int(msg.get("end_line", 1)))
                if target_id in session.clients:
                    session.clients[target_id].region = (min(start_line, end_line), max(start_line, end_line))
                await session.broadcast_participants()

            elif msg_type == "clear_region":
                if client.role != "host":
                    continue
                target_id = msg.get("target_id")
                if target_id in session.clients:
                    session.clients[target_id].region = None
                await session.broadcast_participants()

            elif msg_type == "chat":
                text = str(msg.get("text") or "").strip()
                if text:
                    text = text[:MAX_CHAT_MESSAGE_CHARS]
                    await session.broadcast({"type": "chat", "from": client.name, "text": text})

            elif msg_type == "check_syntax":
                if client.role != "host":
                    continue
                code = str(msg.get("code") or session.doc_text)
                ok, error = check_syntax(code)
                await session.broadcast({"type": "syntax_result", "ok": ok, "error": error, "by": client.name})

            elif msg_type == "run_code":
                if client.role != "host":
                    continue
                if session.run_task and not session.run_task.done():
                    await client.ws.send_text(json.dumps({"type": "error", "message": "Код уже выполняется."}, ensure_ascii=False))
                    continue
                code = str(msg.get("code") or session.doc_text)
                if utf8_size(code) > MAX_DOCUMENT_BYTES:
                    await client.ws.send_text(json.dumps({"type": "error", "message": "Размер кода превышает 1 МБ."}, ensure_ascii=False))
                    continue
                timeout_s = max(1.0, min(float(msg.get("timeout", 5.0)), 30.0))
                session.files[session.current_filename] = code
                session.doc_text = code
                session.persist_state()
                session.run_task = asyncio.create_task(run_python_streaming(session, code, timeout_s))

            elif msg_type == "stop_code":
                if client.role != "host":
                    continue
                await session.stop_running_code()

            elif msg_type in {"save_py", "autosave"}:
                if client.role != "host":
                    if msg_type == "save_py":
                        await client.ws.send_text(json.dumps({"type": "save_result", "ok": False, "error": "Сохранять общий файл может только преподаватель."}, ensure_ascii=False))
                    continue

                code = str(msg.get("code") or session.doc_text)
                if utf8_size(code) > MAX_DOCUMENT_BYTES:
                    await client.ws.send_text(json.dumps({"type": "save_result", "ok": False, "error": "Размер файла превышает 1 МБ."}, ensure_ascii=False))
                    continue
                requested_name = msg.get("filename") or session.current_filename
                filename = sanitize_filename(str(requested_name))
                os.makedirs(session.saved_dir, exist_ok=True)
                path = os.path.join(session.saved_dir, filename)
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(code)
                    session.files[filename] = code
                    if filename == session.current_filename:
                        session.doc_text = code
                    session.persist_state()
                    await client.ws.send_text(json.dumps({"type": "save_result", "ok": True, "filename": filename}, ensure_ascii=False))
                except Exception as e:
                    await client.ws.send_text(json.dumps({"type": "save_result", "ok": False, "error": str(e)}, ensure_ascii=False))

            elif msg_type == "switch_file":
                if client.role != "host":
                    continue
                await session.set_current_file(msg.get("filename") or "main.py")

            elif msg_type == "create_file":
                if client.role != "host":
                    continue
                filename = sanitize_filename(msg.get("filename") or f"file_{int(time.time())}.py")
                if filename not in session.files:
                    session.files[filename] = ""
                    session.ensure_file_blame(filename, "")
                    session.persist_state()
                await session.set_current_file(filename)

            elif msg_type == "import_file":
                if client.role != "host":
                    continue
                await client.ws.send_text(json.dumps({
                    "type": "error",
                    "message": "Импорт по имени файла отключён. Используйте загрузку файла с устройства преподавателя через интерфейс.",
                }, ensure_ascii=False))

            elif msg_type == "import_file_content":
                if client.role != "host":
                    continue
                filename = sanitize_filename(msg.get("filename") or session.current_filename or "main.py")
                content = str(msg.get("content") or "")
                if utf8_size(content) > MAX_DOCUMENT_BYTES:
                    await client.ws.send_text(json.dumps({"type": "error", "message": "Размер импортируемого файла превышает 1 МБ."}, ensure_ascii=False))
                    continue
                session.replace_file_text(filename, content, client)
                await session.broadcast({
                    "type": "doc_full",
                    "doc": {"text": session.doc_text, "version": session.version},
                    "filename": session.current_filename,
                })
                await session.broadcast({"type": "files", "current": session.current_filename, "files": list(session.files.keys())})
                await client.ws.send_text(json.dumps({"type": "save_result", "ok": True, "filename": filename}, ensure_ascii=False))

            elif msg_type == "ping":
                await client.ws.send_text(json.dumps({"type": "pong", "ts": msg.get("ts"), "server_ts": int(time.time() * 1000)}, ensure_ascii=False))

            elif msg_type == "latency_update":
                try:
                    client.latency_ms = max(0, min(int(msg.get("latency_ms")), 9999))
                except Exception:
                    client.latency_ms = None
                await session.broadcast_participants()

    except WebSocketDisconnect:
        pass
    except Exception:
        with contextlib.suppress(Exception):
            await ws.close()
    finally:
        if session and client:
            if session.active_editor_id == client.id:
                session.active_editor_id = None

            session.clients.pop(client.id, None)

            if client.role == "student":
                session.ip_map.pop(client_ip, None)
                session.save_student_snapshot(client)

            if client.role == "host" and session.host_id == client.id:
                session.host_id = None
                session.persist_state()
                await session.schedule_host_expiration()
            elif session.clients:
                await session.broadcast_participants()
            else:
                if session.host_reconnect_deadline is None:
                    sessions.pop(session.room_id, None)
                else:
                    session.persist_state()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--hash-password":
        try:
            print(hash_password_bcrypt(" ".join(sys.argv[2:])))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
    else:
        port = 8000
        reload_enabled = os.environ.get("ONLINECOMPILE_RELOAD", "").strip().lower() in {"1", "true", "yes", "on"}
        print("Сервер onlinecompile запускается. Открыть можно по адресам:", file=sys.stderr)
        for url in get_local_access_urls(port):
            print(f" - {url}", file=sys.stderr)
        if reload_enabled:
            print("Автоперезагрузка включена через ONLINECOMPILE_RELOAD=1. Папка room_data исключена из наблюдения.", file=sys.stderr)
        else:
            print("Автоперезагрузка отключена по умолчанию, чтобы автосохранение не вызывало перезапуск сервера.", file=sys.stderr)
        uvicorn.run(
            "server:app",
            host="0.0.0.0",
            port=port,
            reload=reload_enabled,
            reload_includes=["*.py", "*.html", "*.css", "*.js", "*.json"],
            reload_excludes=["room_data/*", "room_data/**/*", "*.csv", "*.tmp"],
        )