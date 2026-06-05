import ast
import asyncio
import base64
import contextlib
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
import tokenize
import zipfile
from html import escape
from urllib.parse import parse_qs, quote
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
MIN_STUDENT_SAVE_COOLDOWN_SECONDS = 60
MAX_STUDENT_SAVE_COOLDOWN_SECONDS = 5 * 60
DEFAULT_STUDENT_SAVE_COOLDOWN_SECONDS = 60
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_CHAT_MESSAGE_CHARS = 1000
# Chat anti-spam: a burst of CHAT_RAPID_THRESHOLD messages within
# CHAT_RAPID_WINDOW_SECONDS arms a penalty for CHAT_SPAM_DURATION_SECONDS, during
# which each message must be at least CHAT_SPAM_COOLDOWN_SECONDS apart.
CHAT_RAPID_WINDOW_SECONDS = 10.0
CHAT_RAPID_THRESHOLD = 10
CHAT_SPAM_DURATION_SECONDS = 120.0
CHAT_SPAM_COOLDOWN_SECONDS = 5.0
MAX_FILENAME_LENGTH = 120
MAX_RUN_OUTPUT_CHARS = 240_000
RUN_OUTPUT_CHUNK_BYTES = 4096
MAX_CODE_LINES = 10_000
MAX_CODE_LINE_CHARS = 20_000
MAX_AST_BRACKET_DEPTH = 180
# Debounce interval (seconds) between automatic persist_state calls during
# active editing.  A forced save still happens on disconnect and explicit save.
PERSIST_DEBOUNCE_SECONDS = 0.5
WINDOWS_RESERVED_FILENAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

PYTHON_KEYWORDS = {
    "False", "None", "True", "and", "as", "assert", "async", "await", "break",
    "class", "continue", "def", "del", "elif", "else", "except", "finally",
    "for", "from", "global", "if", "import", "in", "is", "lambda", "nonlocal",
    "not", "or", "pass", "raise", "return", "try", "while", "with", "yield",
    "match", "case",
}

BLOCKED_IMPORT_ROOTS = {
    "builtins", "ctypes", "fcntl", "glob", "http", "importlib", "multiprocessing",
    "os", "pathlib", "pickle", "pkgutil", "platform", "posix", "resource", "shlex",
    "shutil", "signal", "socket", "subprocess", "sys", "tempfile", "urllib", "venv",
}
BLOCKED_CALL_NAMES = {
    "__import__", "breakpoint", "compile", "delattr", "eval", "exec", "getattr",
    "globals", "input", "locals", "open", "setattr", "vars",
}
BLOCKED_ATTRIBUTE_CALLS = {
    "chmod", "chown", "connect", "execv", "execve", "fork", "kill", "killpg",
    "mkdir", "open", "popen", "remove", "rename", "replace", "rmdir", "rmtree",
    "spawnl", "spawnlp", "spawnv", "spawnvp", "system", "unlink", "write",
}

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
os.makedirs(DATA_DIR, exist_ok=True)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Add baseline hardening headers suitable for school/corporate deployments."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
    )
    return response


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def bounded_int_from_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


STUDENT_SAVE_COOLDOWN_SECONDS = bounded_int_from_env(
    "ONLINECOMPILE_STUDENT_SAVE_COOLDOWN_SECONDS",
    DEFAULT_STUDENT_SAVE_COOLDOWN_SECONDS,
    MIN_STUDENT_SAVE_COOLDOWN_SECONDS,
    MAX_STUDENT_SAVE_COOLDOWN_SECONDS,
)


def get_local_access_urls(port: int) -> List[str]:
    urls = [f"http://127.0.0.1:{port}"]
    with contextlib.suppress(Exception):
        hostname = socket.gethostname()
        seen = {url.split("//", 1)[1].split(":", 1)[0] for url in urls}
        for _family, _, _, _, sockaddr in socket.getaddrinfo(hostname, None, family=socket.AF_INET):
            host = sockaddr[0]
            if host and not host.startswith("127.") and host not in seen:
                urls.append(f"http://{host}:{port}")
                seen.add(host)
    return urls


def preferred_ws_protocol() -> str:
    try:
        import wsproto  # noqa: F401
        return "wsproto"
    except ImportError:
        return "websockets"


def configure_websocket_logging(ws_protocol: str) -> None:
    """Suppress noisy transport tracebacks from abrupt disconnects."""
    if ws_protocol != "websockets":
        return
    logging.getLogger("websockets.protocol").setLevel(logging.CRITICAL)
    logging.getLogger("websockets.server").setLevel(logging.CRITICAL)


# ---------------------------------------------------------------------------
# Configuration / authentication
# ---------------------------------------------------------------------------

def load_host_config() -> dict:
    default_config: dict = {
        "hosts": [{"username": "HOST", "password": "Example1"}]
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
    if not isinstance(hosts, list):
        return default_config

    normalized: List[dict] = []
    for item in hosts:
        if not isinstance(item, dict):
            continue
        username = str(item.get("username") or "").strip()
        password = str(item.get("password") or "")
        password_hash = str(item.get("password_hash") or "")
        if not username:
            continue
        normalized_item: dict = {"username": username}
        if password_hash:
            normalized_item["password_hash"] = password_hash
        elif password:
            normalized_item["password"] = password
        else:
            continue
        normalized.append(normalized_item)

    return {"hosts": normalized} if normalized else default_config


def get_host_config() -> dict:
    """Re-reads config.json on every call to support hot-reload."""
    return load_host_config()


def normalize_secret_for_bcrypt(secret: str) -> bytes:
    """bcrypt silently truncates at 72 bytes; hash long secrets first."""
    data = (secret or "").encode("utf-8")
    if len(data) <= 72:
        return data
    return base64.b64encode(hashlib.sha256(data).digest())


def hash_password_bcrypt(password: str) -> str:
    if bcrypt is None:
        raise RuntimeError(
            "Модуль bcrypt не установлен. Добавьте зависимость и выполните poetry install."
        )
    return bcrypt.hashpw(normalize_secret_for_bcrypt(password), bcrypt.gensalt()).decode("utf-8")


def verify_password_bcrypt(password: str, password_hash: str) -> bool:
    normalized_hash = str(password_hash or "").strip()
    if bcrypt is None or not normalized_hash:
        return False
    hash_bytes = normalized_hash.encode("utf-8")
    # Accept $2y$ / $2a$ hashes produced by PHP/older tooling.
    if hash_bytes.startswith((b"$2y$", b"$2a$")):
        hash_bytes = b"$2b$" + hash_bytes[4:]
    try:
        return bcrypt.checkpw(normalize_secret_for_bcrypt(password), hash_bytes)
    except ValueError:
        return False


def secure_text_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(
        str(left or "").encode("utf-8"),
        str(right or "").encode("utf-8"),
    )


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
                return False, (
                    "Зависимость bcrypt не установлена. "
                    "Выполните poetry install и перезапустите сервер."
                )
            if verify_password_bcrypt(password, password_hash):
                return True, ""
            return False, "Неверный пароль преподавателя."

        legacy_password = str(host.get("password") or "")
        if legacy_password and secure_text_equal(password, legacy_password):
            return True, ""
        return False, "Неверный пароль преподавателя."

    return False, "Пользователь преподавателя не найден в config.json."


# ---------------------------------------------------------------------------
# Sanitisation helpers
# ---------------------------------------------------------------------------

def sanitize_room_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", str(value or "").strip())
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


def sanitize_personal_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9А-Яа-яЁё _.-]", "", str(name or "").strip())
    return cleaned[:60] or "Guest"


def utf8_size(value: str) -> int:
    return len(str(value or "").encode("utf-8"))


def clamp_int(
    value: object,
    default: int,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def clamp_float(
    value: object,
    default: float,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

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
    for _root, _dirs, files in os.walk(saved_dir):
        for name in files:
            if name.startswith(".") or name.endswith(".tmp"):
                continue
            return True
    return False


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
    if sessions.get(room) is not None:
        return True
    ts = room_last_saved_at(room)
    return ts is not None and (time.time() - ts) <= DOWNLOAD_TTL_SECONDS


def attachment_headers(filename: str) -> dict:
    visible_name = str(filename or "download").replace("\r", "").replace("\n", "")
    ascii_fallback = re.sub(r"[^A-Za-z0-9._-]", "_", visible_name).strip("._") or "download"
    return {
        "Content-Disposition": (
            f'attachment; filename="{ascii_fallback}"; '
            f"filename*=UTF-8''{quote(visible_name, safe='')}"
        ),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    }


def atomic_write_text(path: str, content: str) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.{secrets.token_hex(6)}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(str(content))
        os.replace(tmp_path, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp_path)


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

def format_role_label(role: str) -> str:
    return "Ведущий" if role == "host" else "Студент"


def highlight_python_html(code_line: str) -> str:
    """Minimal syntax highlighting for blame-report HTML output."""
    text = str(code_line or "")
    token_re = re.compile(
        r"(#[^\n]*|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|\b\d+(?:\.\d+)?\b|\b[A-Za-z_][A-Za-z0-9_]*\b)",
        re.DOTALL,
    )
    parts: List[str] = []
    pos = 0
    for match in token_re.finditer(text):
        start, end = match.span()
        if start > pos:
            parts.append(escape(text[pos:start]))
        token = match.group(0)
        if token.startswith("#"):
            cls = "token-comment"
        elif token[:1] in {'"', "'"}:
            cls = "token-string"
        elif re.fullmatch(r"\d+(?:\.\d+)?", token):
            cls = "token-number"
        elif token in PYTHON_KEYWORDS:
            cls = "token-keyword"
        else:
            cls = "token-name"
        parts.append(f'<span class="{cls}">{escape(token)}</span>')
        pos = end
    if pos < len(text):
        parts.append(escape(text[pos:]))
    return "".join(parts)


def render_blame_html(room: str, filename: str, rows: List[dict]) -> str:
    room_safe = escape(room)
    filename_safe = escape(filename)
    csv_href = (
        f"/api/rooms/{quote(room, safe='')}/reports/blame"
        f"?filename={quote(filename, safe='')}&format=csv"
    )
    json_href = (
        f"/api/rooms/{quote(room, safe='')}/reports/blame"
        f"?filename={quote(filename, safe='')}&format=json"
    )
    body_rows = "\n".join(
        f"<tr>"
        f"<td>{row['line']}</td>"
        f"<td>{escape(str(row['author']))}</td>"
        f"<td>{escape(str(row.get('edited_at') or ''))}</td>"
        f"<td>{int(row.get('access_grant_no', 0))}</td>"
        f"<td><pre class=\"code-cell\">{highlight_python_html(str(row['text']))}</pre></td>"
        f"</tr>"
        for row in rows
    )
    style = (
        "body{font-family:Inter,Arial,sans-serif;margin:24px;color:#10213b;background:#fff}"
        "table{border-collapse:collapse;width:100%}th,td{border:1px solid #d0d7e2;padding:8px;vertical-align:top}"
        "th{background:#eef4ff}pre{margin:0;white-space:pre-wrap;font-family:Consolas,monospace}"
        ".code-cell{background:transparent;color:#10213b;border-radius:0;padding:0}"
        ".token-keyword{color:#275efe;font-weight:700}.token-string{color:#16803d}"
        ".token-comment{color:#64748b;font-style:italic}.token-number{color:#b45309}"
        ".token-name{color:#10213b}"
        ".report-toolbar{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0 20px}"
        ".report-toolbar a{display:inline-flex;align-items:center;min-height:40px;padding:0 14px;"
        "border-radius:12px;border:1px solid #d7e2f2;background:#eef4ff;color:#275efe;"
        "text-decoration:none;font-weight:600}"
        ".report-note{color:#5c6d89;margin:0 0 12px}"
    )
    return (
        f'<!DOCTYPE html>\n<html lang="ru"><head><meta charset="utf-8">'
        f"<title>Blame report</title><style>{style}</style></head>"
        f'<body class="report-page">'
        f"<h1>Blame-отчёт по файлу {filename_safe}</h1>"
        f"<p>Комната: {room_safe}</p>"
        f'<p class="report-note">Открыта отдельная страница отчёта. '
        f"Отсюда можно просмотреть содержимое документа и скачать эти данные в формате CSV.</p>"
        f'<div class="report-toolbar">'
        f'<a href="{csv_href}">Скачать CSV</a>'
        f'<a href="{json_href}" target="_blank" rel="noopener">Открыть JSON</a>'
        f"</div>"
        f"<table><thead><tr>"
        f"<th>№</th><th>Автор</th>"
        f"<th>Время</th><th>№ выдачи доступа</th><th>Содержимое строки</th>"
        f"</tr></thead><tbody>{body_rows}</tbody></table>"
        f"</body></html>"
    )


def render_access_html(room: str, rows: List[dict]) -> str:
    room_safe = escape(room)
    csv_href = f"/api/rooms/{quote(room, safe='')}/reports/access?format=csv"
    json_href = f"/api/rooms/{quote(room, safe='')}/reports/access?format=json"
    body_rows = "\n".join(
        f"<tr>"
        f"<td>{escape(str(row['student']))}</td>"
        f"<td>{int(row['access_grants'])}</td>"
        f"<td>{escape(str(row.get('last_edit_at') or ''))}</td>"
        f"</tr>"
        for row in rows
    )
    style = (
        "body{font-family:Inter,Arial,sans-serif;margin:24px}"
        "table{border-collapse:collapse;width:100%}th,td{border:1px solid #d0d7e2;padding:8px}"
        "th{background:#eef4ff}"
        ".report-toolbar{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0 20px}"
        ".report-toolbar a{display:inline-flex;align-items:center;min-height:40px;padding:0 14px;"
        "border-radius:12px;border:1px solid #d7e2f2;background:#eef4ff;color:#275efe;"
        "text-decoration:none;font-weight:600}"
        ".report-note{color:#5c6d89;margin:0 0 12px}"
    )
    return (
        f'<!DOCTYPE html>\n<html lang="ru"><head><meta charset="utf-8">'
        f"<title>Access report</title><style>{style}</style></head>"
        f'<body class="report-page">'
        f"<h1>Отчёт по выдачам доступа</h1>"
        f"<p>Комната: {room_safe}</p>"
        f'<p class="report-note">Открыта отдельная страница отчёта. '
        f"Отсюда можно просмотреть данные по выдачам доступа и скачать таблицу в формате CSV.</p>"
        f'<div class="report-toolbar">'
        f'<a href="{csv_href}">Скачать CSV</a>'
        f'<a href="{json_href}" target="_blank" rel="noopener">Открыть JSON</a>'
        f"</div>"
        f"<table><thead><tr>"
        f"<th>Студент</th><th>Количество выдач доступа</th><th>Последнее редактирование</th>"
        f"</tr></thead><tbody>{body_rows}</tbody></table>"
        f"</body></html>"
    )


# ---------------------------------------------------------------------------
# Code safety
# ---------------------------------------------------------------------------

class CodeSafetyError(ValueError):
    pass


def validate_code_shape(code: str) -> None:
    text = str(code or "")
    if utf8_size(text) > MAX_DOCUMENT_BYTES:
        raise CodeSafetyError("Размер кода превышает 1 МБ.")

    lines = text.splitlines()
    if len(lines) > MAX_CODE_LINES:
        raise CodeSafetyError(f"Слишком много строк кода: максимум {MAX_CODE_LINES}.")
    if any(len(line) > MAX_CODE_LINE_CHARS for line in lines):
        raise CodeSafetyError(f"Слишком длинная строка кода: максимум {MAX_CODE_LINE_CHARS} символов.")

    depth = 0
    max_depth = 0
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for token in tokens:
            if token.type != tokenize.OP:
                continue
            if token.string in "([{":
                depth += 1
                max_depth = max(max_depth, depth)
                if max_depth > MAX_AST_BRACKET_DEPTH:
                    raise CodeSafetyError(
                        f"Слишком глубокая вложенность скобок: максимум {MAX_AST_BRACKET_DEPTH}."
                    )
            elif token.string in ")]}":
                depth = max(0, depth - 1)
    except tokenize.TokenError as exc:
        raise CodeSafetyError(
            str(exc.args[0]) if exc.args else "Ошибка токенизации кода."
        ) from exc


def parse_code_safely(code: str) -> ast.AST:
    validate_code_shape(code)
    try:
        return ast.parse(str(code or ""))
    except SyntaxError as exc:
        raise CodeSafetyError(f"{exc.msg} (line {exc.lineno}, col {exc.offset})") from exc
    except (MemoryError, RecursionError) as exc:
        raise CodeSafetyError("Код слишком сложный для безопасной проверки синтаксиса.") from exc


class RunSafetyVisitor(ast.NodeVisitor):
    def _fail(self, node: ast.AST, message: str) -> None:
        line = getattr(node, "lineno", "?")
        raise CodeSafetyError(f"{message} (line {line})")

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".", 1)[0]
            if root in BLOCKED_IMPORT_ROOTS:
                self._fail(node, f"Импорт модуля '{root}' запрещён в учебном запуске.")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        root = (node.module or "").split(".", 1)[0]
        if root in BLOCKED_IMPORT_ROOTS:
            self._fail(node, f"Импорт модуля '{root}' запрещён в учебном запуске.")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id in BLOCKED_CALL_NAMES:
            if func.id == "input":
                self._fail(node, "Интерактивный ввод input() пока не поддерживается.")
            self._fail(node, f"Вызов '{func.id}()' запрещён в учебном запуске.")
        if isinstance(func, ast.Attribute) and func.attr in BLOCKED_ATTRIBUTE_CALLS:
            self._fail(node, f"Вызов '.{func.attr}()' запрещён в учебном запуске.")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "__builtins__":
            self._fail(node, "Доступ к __builtins__ запрещён в учебном запуске.")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("__"):
            self._fail(node, "Доступ к dunder-атрибутам запрещён в учебном запуске.")
        self.generic_visit(node)


def validate_code_for_run(code: str) -> Tuple[bool, str]:
    try:
        tree = parse_code_safely(code)
        RunSafetyVisitor().visit(tree)
        return True, ""
    except CodeSafetyError as exc:
        return False, str(exc)


def check_syntax(code: str) -> Tuple[bool, str]:
    try:
        parse_code_safely(code)
        return True, ""
    except CodeSafetyError as exc:
        return False, str(exc)


def make_preexec():
    if os.name != "posix":
        return None

    import resource

    def _limit() -> None:
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
            resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024 * 1024, 2 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
            if hasattr(resource, "RLIMIT_NPROC"):
                resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))
        except Exception:
            pass

    return _limit


# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------

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
    # Chat anti-spam bookkeeping
    chat_times: List[float] = field(default_factory=list)
    chat_spam_until: float = 0.0
    chat_last_at: float = 0.0


# 30 visually distinct colours that work on both light and dark backgrounds.
_CLIENT_COLORS = [
    "#f44336", "#e91e63", "#9c27b0", "#673ab7", "#3f51b5", "#2196f3",
    "#03a9f4", "#00bcd4", "#009688", "#4caf50", "#8bc34a", "#cddc39",
    "#ff9800", "#ff5722", "#795548", "#607d8b", "#16a085", "#27ae60",
    "#2980b9", "#8e44ad", "#2c3e50", "#e67e22", "#e74c3c", "#f1c40f",
    "#6c5ce7", "#00cec9", "#fd79a8", "#0984e3", "#00b894", "#e17055",
]


class Session:
    def __init__(self, room_id: str) -> None:
        self.room_id = sanitize_room_id(room_id)
        self.clients: Dict[str, Client] = {}
        self.ip_map: Dict[str, str] = {}
        self.host_id: Optional[str] = None
        self.host_username: Optional[str] = None
        self.host_reconnect_deadline: Optional[float] = None
        self.host_reconnect_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]
        self.lock = asyncio.Lock()
        self.fs_lock = threading.RLock()

        self.doc_text: str = INITIAL_DOC
        self.version: int = 1
        self.active_editor_id: Optional[str] = None
        self.files: Dict[str, str] = {"main.py": INITIAL_DOC}
        self.current_filename: str = "main.py"

        self.run_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]
        self.running_process: Optional[Process] = None
        self.running_tmpdir: Optional[str] = None
        self.stop_requested: bool = False

        # Debounce state for persist_state
        self._persist_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]
        self._pending_persist: bool = False

        self.saved_dir = os.path.join(DATA_DIR, self.room_id)
        self.students_dir = os.path.join(self.saved_dir, "students")
        self.reports_dir = os.path.join(self.saved_dir, "reports")
        os.makedirs(self.students_dir, exist_ok=True)
        os.makedirs(self.reports_dir, exist_ok=True)

        self.blame_by_file: Dict[str, List[dict]] = {}
        self.audit_log: List[dict] = []
        self.student_metrics: Dict[str, dict] = {}
        self.last_saved_at: Optional[str] = None

        # Cyclic colour iterator – wraps around instead of falling back to hash.
        self._color_index: int = 0

        self.load_state()

    def next_color(self) -> str:
        color = _CLIENT_COLORS[self._color_index % len(_CLIENT_COLORS)]
        self._color_index += 1
        return color

    def session_state_path(self) -> str:
        return os.path.join(self.saved_dir, SESSION_STATE_FILENAME)

    def autosave_path(self) -> str:
        return os.path.join(self.saved_dir, AUTOSAVE_FILENAME)

    def line_count(self) -> int:
        return max(1, len(self.doc_text.splitlines()))

    def ensure_file_blame(self, filename: str, text: Optional[str] = None) -> None:
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
            blame = blame[: len(lines)]
        for idx in range(len(lines)):
            blame[idx]["line"] = idx + 1
        self.blame_by_file[filename] = blame

    def replace_file_text(
        self,
        filename: str,
        text: str,
        author: Optional[Client] = None,
    ) -> None:
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

    def load_state(self) -> None:
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
        self.doc_text = str(
            data.get("doc_text") or self.files.get(self.current_filename, INITIAL_DOC)
        )
        try:
            self.version = max(1, int(data.get("version") or 1))
        except (TypeError, ValueError):
            self.version = 1
        self.host_username = str(data.get("host_username") or "") or None
        self.student_metrics = (
            data.get("student_metrics")
            if isinstance(data.get("student_metrics"), dict)
            else {}
        )
        self.audit_log = (
            data.get("audit_log") if isinstance(data.get("audit_log"), list) else []
        )
        self.blame_by_file = (
            data.get("blame_by_file") if isinstance(data.get("blame_by_file"), dict) else {}
        )
        self.last_saved_at = data.get("last_saved_at")
        self.ensure_file_blame(self.current_filename, self.doc_text)

    def persist_state(self) -> None:
        """Write session state to disk immediately (synchronous)."""
        payload = {
            "room_id": self.room_id,
            "version": self.version,
            "doc_text": self.doc_text,
            "current_filename": self.current_filename,
            "files": self.files,
            "student_metrics": self.student_metrics,
            # Keep only the most recent 500 audit entries in the state file.
            # For full audit trails, wire an append-only external log instead.
            "audit_log": self.audit_log[-500:],
            "blame_by_file": self.blame_by_file,
            "last_saved_at": utc_iso(),
            "host_username": self.host_username,
        }
        with self.fs_lock:
            os.makedirs(self.saved_dir, exist_ok=True)
            state_tmp = self.session_state_path() + f".{secrets.token_hex(6)}.tmp"
            try:
                with open(state_tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                os.replace(state_tmp, self.session_state_path())
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.remove(state_tmp)
            atomic_write_text(self.autosave_path(), self.doc_text)
        self.last_saved_at = payload["last_saved_at"]

    def schedule_persist(self) -> None:
        """Debounced persist: coalesces rapid edits into one disk write."""
        self._pending_persist = True
        if self._persist_task is not None and not self._persist_task.done():
            return

        async def _runner() -> None:
            await asyncio.sleep(PERSIST_DEBOUNCE_SECONDS)
            if self._pending_persist:
                self._pending_persist = False
                self.persist_state()
            self._persist_task = None

        try:
            loop = asyncio.get_running_loop()
            self._persist_task = loop.create_task(_runner())
        except RuntimeError:
            # No running event loop (e.g. tests calling synchronously).
            self.persist_state()

    def upsert_student_metric(self, client: Client) -> dict:
        metrics = self.student_metrics.setdefault(client.name, {})
        metrics.update({
            "name": client.name,
            "access_grants": int(metrics.get("access_grants") or 0),
            "last_edit_at": metrics.get("last_edit_at"),
            "last_save_at": metrics.get("last_save_at"),
            "last_save_ts": float(metrics.get("last_save_ts") or 0),
            "last_save_filename": metrics.get("last_save_filename"),
        })
        metrics.pop("score", None)
        client.access_grants = metrics["access_grants"]
        return metrics

    def list_clients(self) -> List[dict]:
        items: List[dict] = []
        for client in self.clients.values():
            metrics = (
                self.upsert_student_metric(client)
                if client.role == "student"
                else {"access_grants": 0}
            )
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
                "cursor": {"line": client.cursor_line, "col": client.cursor_col},
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

    def mark_blame_lines(
        self,
        filename: str,
        text_before: str,
        start_idx: int,
        end_idx: int,
        insert_text: str,
        client: Client,
    ) -> None:
        filename = sanitize_filename(filename)
        before_lines = text_before.splitlines() or [""]
        new_text = text_before[:start_idx] + insert_text + text_before[end_idx:]
        after_lines = new_text.splitlines() or [""]
        blame = list(self.blame_by_file.get(filename) or [])
        while len(blame) < len(before_lines):
            blame.append({
                "line": len(blame) + 1,
                "author": "Система",
                "timestamp": utc_iso(),
                "access_grant_no": 0,
            })

        start_line, end_line = self.range_to_lines(text_before, start_idx, end_idx)
        inserted_line_count = max(
            1,
            insert_text.count("\n") + 1 if insert_text else max(1, end_line - start_line + 1),
        )
        new_end_line = start_line + inserted_line_count - 1

        replacement = [
            {
                "line": line_no,
                "author": client.name,
                "timestamp": utc_iso(),
                "access_grant_no": client.access_grants,
            }
            for line_no in range(start_line, new_end_line + 1)
        ]

        blame[start_line - 1 : end_line] = replacement
        while len(blame) < len(after_lines):
            blame.append({
                "line": len(blame) + 1,
                "author": client.name,
                "timestamp": utc_iso(),
                "access_grant_no": client.access_grants,
            })
        blame = blame[: len(after_lines)]
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

    def save_student_snapshot(
        self,
        client: Client,
        text: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> Optional[str]:
        student_dir = os.path.join(self.students_dir, sanitize_personal_name(client.name))
        safe_filename = sanitize_filename(filename or self.current_filename)
        path = os.path.join(student_dir, safe_filename)
        try:
            with self.fs_lock:
                atomic_write_text(path, self.doc_text if text is None else str(text))
            return safe_filename
        except Exception:
            return None

    def student_save_cooldown_remaining(self, client: Client) -> int:
        metrics = self.upsert_student_metric(client)
        last_save_ts = float(metrics.get("last_save_ts") or 0)
        elapsed = time.time() - last_save_ts
        return max(0, int(STUDENT_SAVE_COOLDOWN_SECONDS - elapsed + 0.999))

    def save_student_file(self, client: Client, filename: str, code: str) -> str:
        safe_filename = self.save_student_snapshot(client, code, filename)
        if safe_filename is None:
            raise OSError("Не удалось сохранить личную копию студента.")

        metrics = self.upsert_student_metric(client)
        metrics["last_save_at"] = utc_iso()
        metrics["last_save_ts"] = time.time()
        metrics["last_save_filename"] = safe_filename
        self.audit_log.append({
            "at": utc_iso(),
            "event": "student_save",
            "room": self.room_id,
            "student": client.name,
            "file": safe_filename,
        })
        self.persist_state()
        return safe_filename

    def save_room_file(self, filename: str, code: str) -> str:
        safe_filename = sanitize_filename(filename)
        path = os.path.join(self.saved_dir, safe_filename)
        with self.fs_lock:
            atomic_write_text(path, code)
        self.files[safe_filename] = code
        if safe_filename == self.current_filename:
            self.doc_text = code
        self.persist_state()
        return safe_filename

    async def send_to(self, client_id: str, payload: dict) -> None:
        client = self.clients.get(client_id)
        if not client:
            return
        with contextlib.suppress(Exception):
            await client.ws.send_text(json.dumps(payload, ensure_ascii=False))

    async def broadcast(self, payload: dict, exclude: Optional[set] = None) -> None:
        exclude = exclude or set()
        message = json.dumps(payload, ensure_ascii=False)

        async def send_one(client_id: str, target: Client) -> Optional[str]:
            try:
                await asyncio.wait_for(target.ws.send_text(message), timeout=1.0)
                return None
            except Exception:
                return client_id

        tasks = [
            send_one(cid, target)
            for cid, target in list(self.clients.items())
            if cid not in exclude
        ]
        if not tasks:
            return

        for failed_id in await asyncio.gather(*tasks):
            if failed_id is not None:
                self.clients.pop(failed_id, None)

    async def broadcast_participants(self) -> None:
        await self.broadcast({"type": "participants", "participants": self.list_clients()})

    async def set_current_file(self, filename: str) -> None:
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
        await self.broadcast({
            "type": "files",
            "current": self.current_filename,
            "files": list(self.files.keys()),
        })

    async def terminate_running_process(self) -> None:
        proc = self.running_process
        if not proc or proc.returncode is not None:
            return

        def signal_process(sig: Optional[signal.Signals] = None, force: bool = False) -> None:
            if os.name == "posix" and sig is not None:
                try:
                    os.killpg(proc.pid, sig)
                    return
                except ProcessLookupError:
                    return
                except Exception:
                    with contextlib.suppress(Exception):
                        os.kill(proc.pid, sig)
                    return
            if force:
                proc.kill()
            else:
                proc.terminate()

        with contextlib.suppress(ProcessLookupError, PermissionError, RuntimeError):
            signal_process(signal.SIGTERM if os.name == "posix" else None, force=False)

        try:
            await asyncio.wait_for(proc.wait(), timeout=0.4)
            return
        except asyncio.TimeoutError:
            pass
        except ProcessLookupError:
            return

        sigkill = getattr(signal, "SIGKILL", None)
        with contextlib.suppress(ProcessLookupError, PermissionError, RuntimeError):
            signal_process(sigkill if os.name == "posix" else None, force=True)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=1.0)

    async def stop_running_code(self) -> bool:
        task = self.run_task
        proc = self.running_process
        if (task is None or task.done()) and (proc is None or proc.returncode is not None):
            return False

        self.stop_requested = True
        # Kill the process (group) immediately — a Stop must not wait on the run.
        await self.terminate_running_process()

        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
            except asyncio.TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            except (asyncio.CancelledError, Exception):
                pass
        return True

    async def close_room(self, reason: str) -> None:
        await self.stop_running_code()

        if self.run_task and not self.run_task.done():
            self.run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.run_task

        # Flush any pending debounced write before closing.
        if self._persist_task and not self._persist_task.done():
            self._persist_task.cancel()
        self.persist_state()

        await self.broadcast({"type": "room_closed", "message": reason})
        for client in list(self.clients.values()):
            with contextlib.suppress(Exception):
                await client.ws.close()

        self.clients.clear()
        self.ip_map.clear()
        self.active_editor_id = None
        self.host_id = None

        if self.running_tmpdir:
            shutil.rmtree(self.running_tmpdir, ignore_errors=True)
            self.running_tmpdir = None

    async def apply_patch(
        self,
        client_id: str,
        base_version: int,
        start: int,
        end: int,
        insert_text: str,
    ) -> Tuple[bool, str]:
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
                        return (
                            False,
                            f"Редактирование вне разрешённого диапазона [{region_start}-{region_end}].",
                        )

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
                    self.mark_blame_lines(
                        self.current_filename, old_text, start, end, insert_text, client
                    )
                    self.save_student_snapshot(client)
            else:
                self.mark_blame_lines(
                    self.current_filename, old_text, start, end, insert_text, client
                )

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
            # Use debounced persist during active editing to reduce disk I/O.
            self.schedule_persist()

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

    def register_access_grant(self, target: Client) -> None:
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
        return [
            {
                "student": name,
                "access_grants": int(metrics.get("access_grants") or 0),
                "last_edit_at": metrics.get("last_edit_at"),
            }
            for name, metrics in sorted(
                self.student_metrics.items(),
                key=lambda x: (-int(x[1].get("access_grants") or 0), x[0]),
            )
        ]

    async def schedule_host_expiration(self) -> None:
        if self.host_reconnect_task and not self.host_reconnect_task.done():
            self.host_reconnect_task.cancel()
        self.host_reconnect_deadline = time.time() + HOST_RECONNECT_TIMEOUT_SECONDS

        async def _runner() -> None:
            try:
                await asyncio.sleep(HOST_RECONNECT_TIMEOUT_SECONDS)
                if self.host_id is None and not any(
                    c.role == "host" for c in self.clients.values()
                ):
                    await self.close_room(
                        "Преподаватель не восстановил соединение в течение 5 минут. "
                        "Комната закрыта."
                    )
                    sessions.pop(self.room_id, None)
            except asyncio.CancelledError:
                return

        self.host_reconnect_task = asyncio.create_task(_runner())
        deadline_human = datetime.fromtimestamp(self.host_reconnect_deadline).strftime("%H:%M:%S")
        await self.broadcast({
            "type": "host_disconnected",
            "message": (
                "Преподаватель временно отключился. "
                "Сессия сохранена и ожидает его возвращения в течение 5 минут."
            ),
            "deadline_human": deadline_human,
            "deadline_ts": self.host_reconnect_deadline,
            "timeout_seconds": HOST_RECONNECT_TIMEOUT_SECONDS,
        })

    async def restore_host(self) -> None:
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


# ---------------------------------------------------------------------------
# Code runner
# ---------------------------------------------------------------------------

async def stream_pipe(
    session: Session,
    stream: Optional[asyncio.StreamReader],
    stream_name: str,
) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(RUN_OUTPUT_CHUNK_BYTES)
        if not chunk:
            break
        text = chunk.decode("utf-8", "replace")
        used = int(getattr(session, "run_output_chars", 0) or 0)
        remaining = MAX_RUN_OUTPUT_CHARS - used
        if remaining <= 0:
            if not getattr(session, "run_output_truncated", False):
                session.run_output_truncated = True  # type: ignore[attr-defined]
                await session.broadcast({
                    "type": "run_output",
                    "stream": "stderr",
                    "text": "\n[Output truncated: слишком много вывода, оставлена прокрутка терминала]\n",
                })
            await asyncio.sleep(0)
            continue
        if len(text) > remaining:
            text = text[:remaining]
            session.run_output_truncated = True  # type: ignore[attr-defined]
            await session.broadcast({
                "type": "run_output",
                "stream": "stderr",
                "text": "\n[Output truncated: слишком много вывода, оставлена прокрутка терминала]\n",
            })
        session.run_output_chars = used + len(text)  # type: ignore[attr-defined]
        await session.broadcast({"type": "run_output", "stream": stream_name, "text": text})
        await asyncio.sleep(0)


async def run_python_streaming(session: Session, code: str, timeout_s: float) -> None:
    tmpdir = tempfile.mkdtemp(prefix=f"runpy_{session.room_id}_")
    filename = sanitize_filename(session.current_filename)
    path = os.path.join(tmpdir, filename)

    with open(path, "w", encoding="utf-8") as f:
        f.write(code)

    env = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "PATH": os.environ.get("PATH", ""),
    }
    session.running_tmpdir = tmpdir
    session.stop_requested = False
    session.run_output_chars = 0  # type: ignore[attr-defined]
    session.run_output_truncated = False  # type: ignore[attr-defined]

    await session.broadcast({
        "type": "run_state",
        "running": True,
        "filename": filename,
        "clear": True,
    })

    start_ts = time.perf_counter()
    stdout_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]
    stderr_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]

    try:
        proc = await create_subprocess_exec(
            sys.executable,
            "-I",
            "-S",
            "-u",
            path,
            cwd=tmpdir,
            stdin=PIPE,
            stdout=PIPE,
            stderr=PIPE,
            env=env,
            preexec_fn=make_preexec(),
            start_new_session=(os.name == "posix"),
        )
        session.running_process = proc
        if proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.write_eof()
            with contextlib.suppress(Exception):
                proc.stdin.close()

        stdout_task = asyncio.create_task(stream_pipe(session, proc.stdout, "stdout"))
        stderr_task = asyncio.create_task(stream_pipe(session, proc.stderr, "stderr"))

        await asyncio.wait_for(proc.wait(), timeout=timeout_s)
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

        elapsed_ms = int((time.perf_counter() - start_ts) * 1000)
        stopped = session.stop_requested
        if stopped:
            await session.broadcast({
                "type": "run_output",
                "stream": "stderr",
                "text": "\n[Execution stopped]\n",
            })
        await session.broadcast({
            "type": "run_result",
            "ok": proc.returncode == 0 and not stopped,
            "timeout": False,
            "stopped": stopped,
            "returncode": proc.returncode,
            "elapsed_ms": elapsed_ms,
        })
    except asyncio.TimeoutError:
        await session.terminate_running_process()
        elapsed_ms = int((time.perf_counter() - start_ts) * 1000)
        await session.broadcast({
            "type": "run_output",
            "stream": "stderr",
            "text": "\n[Timed out]\n",
        })
        await session.broadcast({
            "type": "run_result",
            "ok": False,
            "timeout": True,
            "stopped": False,
            "returncode": None,
            "elapsed_ms": elapsed_ms,
        })
    except asyncio.CancelledError:
        await session.terminate_running_process()
        await session.broadcast({
            "type": "run_output",
            "stream": "stderr",
            "text": "\n[Execution stopped]\n",
        })
        await session.broadcast({
            "type": "run_result",
            "ok": False,
            "timeout": False,
            "stopped": True,
            "returncode": None,
            "elapsed_ms": int((time.perf_counter() - start_ts) * 1000),
        })
        raise
    except Exception as exc:
        await session.broadcast({
            "type": "run_output",
            "stream": "stderr",
            "text": f"\n[Runner error] {exc}\n",
        })
        await session.broadcast({
            "type": "run_result",
            "ok": False,
            "timeout": False,
            "stopped": False,
            "returncode": None,
            "elapsed_ms": int((time.perf_counter() - start_ts) * 1000),
        })
    finally:
        session.stop_requested = False
        session.running_process = None
        session.run_task = None
        if session.running_tmpdir:
            shutil.rmtree(session.running_tmpdir, ignore_errors=True)
            session.running_tmpdir = None
        await session.broadcast({
            "type": "run_state",
            "running": False,
            "filename": filename,
            "clear": False,
        })


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------

@app.get("/")
@app.get("/onlinecompile")
async def index(_: Request) -> FileResponse:
    return FileResponse(
        os.path.join(STATIC_DIR, "index.html"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True, "sessions": len(sessions)})


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)


@app.post("/api/download-text")
async def download_text_file(request: Request) -> Response:
    """Return an edited text file as a real HTTP attachment."""
    body = await request.body()
    if len(body) > MAX_DOCUMENT_BYTES * 8:
        return JSONResponse(
            {"ok": False, "message": "Файл слишком большой для скачивания."},
            status_code=413,
        )

    params = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    safe_filename = sanitize_filename((params.get("filename") or ["main.py"])[0])
    content = (params.get("content") or [""])[0]
    data = content.encode("utf-8")
    headers = attachment_headers(safe_filename)
    headers["Content-Length"] = str(len(data))
    return Response(content=data, media_type="application/octet-stream", headers=headers)


@app.get("/api/rooms/{room_id}/download")
async def download_current_file(room_id: str, filename: str = "main.py") -> Response:
    room = sanitize_room_id(room_id)
    safe_filename = sanitize_filename(filename)
    if not can_download_room(room):
        return JSONResponse(
            {"ok": False, "message": "Время скачивания истекло. Повторно откройте комнату."},
            status_code=410,
        )
    session = sessions.get(room) or Session(room)
    content = str(
        session.files.get(
            safe_filename,
            session.doc_text if session.current_filename == safe_filename else "",
        )
    )
    if safe_filename not in session.files and not content:
        return JSONResponse({"ok": False, "message": "Файл не найден."}, status_code=404)
    data = content.encode("utf-8")
    headers = attachment_headers(safe_filename)
    headers["Content-Length"] = str(len(data))
    return Response(content=data, media_type="application/octet-stream", headers=headers)


@app.get("/api/rooms/{room_id}/download-all")
async def download_all_room_files(room_id: str) -> Response:
    room = sanitize_room_id(room_id)
    if not can_download_room(room):
        return JSONResponse(
            {"ok": False, "message": "Время скачивания истекло. Повторно откройте комнату."},
            status_code=410,
        )

    session = sessions.get(room) or Session(room)
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for fname, content in sorted(session.files.items()):
            archive.writestr(sanitize_filename(fname), str(content))
    data = output.getvalue()
    headers = attachment_headers(f"{room}_files.zip")
    headers["Content-Length"] = str(len(data))
    return Response(content=data, media_type="application/zip", headers=headers)


@app.get("/api/rooms/{room_id}/reports/blame")
async def blame_report(
    room_id: str, filename: str = "main.py", format: str = "json"
) -> Response:
    room = sanitize_room_id(room_id)
    session = sessions.get(room) or Session(room)
    rows = session.build_blame_rows(filename)

    if format == "html":
        return HTMLResponse(
            render_blame_html(room, sanitize_filename(filename), rows),
            media_type="text/html; charset=utf-8",
        )
    if format == "csv":
        output = io.StringIO()
        writer = csv.DictWriter(
            output, fieldnames=["line", "author", "edited_at", "access_grant_no", "text"]
        )
        writer.writeheader()
        writer.writerows(rows)
        return Response(
            content=output.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{room}_{sanitize_filename(filename)}_blame.csv"'
                )
            },
        )
    return JSONResponse({"room": room, "filename": sanitize_filename(filename), "rows": rows})


@app.get("/api/rooms/{room_id}/reports/access")
@app.get("/api/rooms/{room_id}/reports/scores")
async def access_report(room_id: str, format: str = "json") -> Response:
    room = sanitize_room_id(room_id)
    session = sessions.get(room) or Session(room)
    rows = session.access_rows()

    if format == "html":
        return HTMLResponse(
            render_access_html(room, rows), media_type="text/html; charset=utf-8"
        )
    if format == "csv":
        output = io.StringIO()
        writer = csv.DictWriter(
            output, fieldnames=["student", "access_grants", "last_edit_at"]
        )
        writer.writeheader()
        writer.writerows(rows)
        return Response(
            content=output.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{room}_access_report.csv"'
            },
        )
    return JSONResponse({"room": room, "rows": rows})


# ---------------------------------------------------------------------------
# LSP proxy
# ---------------------------------------------------------------------------

async def read_lsp_message(stream: asyncio.StreamReader) -> Optional[dict]:
    headers: dict = {}
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


async def write_lsp_message(writer: object, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    data = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
    if hasattr(writer, "write"):
        writer.write(data)  # type: ignore[union-attr]
        if hasattr(writer, "drain"):
            await writer.drain()  # type: ignore[union-attr]
    else:
        await writer.send_bytes(data)  # type: ignore[union-attr]


@app.websocket("/lsp/{room_id}/{filename}")
async def lsp_proxy(ws: WebSocket, room_id: str, filename: str) -> None:
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
        await ws.send_text(
            json.dumps({
                "jsonrpc": "2.0",
                "method": "window/logMessage",
                "params": {
                    "type": 1,
                    "message": "pyright-langserver не установлен на сервере. Установите пакет pyright.",
                },
            }, ensure_ascii=False)
        )
        await ws.close()
        return

    async def ws_to_proc() -> None:
        while True:
            message = await ws.receive_text()
            payload = json.loads(message)
            if payload.get("method") == "initialize":
                payload.setdefault("params", {})
                payload["params"].setdefault("rootUri", f"file:///{sanitize_room_id(room_id)}")
                payload["params"].setdefault(
                    "workspaceFolders",
                    [{"uri": f"file:///{sanitize_room_id(room_id)}", "name": sanitize_room_id(room_id)}],
                )
                payload["params"].setdefault(
                    "initializationOptions", {"python": {"pythonPath": "python3"}}
                )
            if payload.get("method") == "textDocument/didOpen":
                payload.setdefault("params", {}).setdefault("textDocument", {}).setdefault(
                    "uri", document_uri
                )
            await write_lsp_message(proc.stdin, payload)

    async def proc_to_ws() -> None:
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


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()

    session: Optional[Session] = None
    client: Optional[Client] = None
    client_ip = ws.client.host if ws.client else "127.0.0.1"

    async def receive_payload() -> Optional[dict]:
        raw_message = await ws.receive_text()
        try:
            payload = json.loads(raw_message)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    try:
        hello = await receive_payload()

        if not hello or hello.get("type") != "hello":
            await ws.send_text(
                json.dumps(
                    {"type": "auth_error", "message": "Ожидалось приветственное сообщение hello."},
                    ensure_ascii=False,
                )
            )
            await ws.close()
            return

        role = "host" if hello.get("role") == "host" else "student"
        name = sanitize_personal_name(
            hello.get("name") or ("Ведущий" if role == "host" else "Student")
        )
        room = sanitize_room_id(hello.get("room") or "default")
        room_action = str(
            hello.get("room_action") or ("join" if role == "student" else "create")
        ).lower()

        if role == "host":
            username = str(hello.get("username") or "").strip()
            password = str(hello.get("password") or "")

            auth_ok, auth_message = authenticate_host(username, password)
            if not auth_ok:
                await ws.send_text(
                    json.dumps({"type": "auth_error", "message": auth_message}, ensure_ascii=False)
                )
                await ws.close()
                return

            existing = sessions.get(room)
            if room_action == "join":
                if existing is None:
                    if room_has_persisted_data(room):
                        existing = Session(room)
                        sessions[room] = existing
                    else:
                        await ws.send_text(
                            json.dumps(
                                {"type": "auth_error", "message": f"Комната '{room}' недоступна."},
                                ensure_ascii=False,
                            )
                        )
                        await ws.close()
                        return
                if existing.host_id and existing.host_id in existing.clients:
                    await ws.send_text(
                        json.dumps(
                            {"type": "auth_error", "message": "В комнате уже есть активный ведущий."},
                            ensure_ascii=False,
                        )
                    )
                    await ws.close()
                    return
                session = existing
            else:
                if existing is not None:
                    if can_resume_existing_host_session(existing, username):
                        session = existing
                    else:
                        await ws.send_text(
                            json.dumps(
                                {"type": "auth_error", "message": f"Комната '{room}' уже существует."},
                                ensure_ascii=False,
                            )
                        )
                        await ws.close()
                        return
                elif room_has_persisted_data(room):
                    await ws.send_text(
                        json.dumps(
                            {
                                "type": "auth_error",
                                "message": f"Комната '{room}' уже существует в сохранённых данных.",
                            },
                            ensure_ascii=False,
                        )
                    )
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
            if session is None or (
                session.host_id is None
                and session.host_reconnect_deadline is None
                and not room_has_persisted_data(room)
            ):
                await ws.send_text(
                    json.dumps(
                        {"type": "auth_error", "message": f"Комната '{room}' недоступна."},
                        ensure_ascii=False,
                    )
                )
                await ws.close()
                return
            if client_ip in session.ip_map:
                await ws.send_text(
                    json.dumps(
                        {
                            "type": "auth_error",
                            "message": "С одного устройства разрешён только один студент.",
                        },
                        ensure_ascii=False,
                    )
                )
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

        await client.ws.send_text(
            json.dumps(
                {
                    "type": "welcome",
                    "you": {
                        "id": client.id,
                        "name": client.name,
                        "role": client.role,
                        "color": client.color,
                    },
                    "room": session.room_id,
                    "doc": {"text": session.doc_text, "version": session.version},
                    "filename": session.current_filename,
                    "files": list(session.files.keys()),
                    "participants": session.list_clients(),
                    "active_editor_id": session.active_editor_id,
                    "limits": {"student_save_cooldown_seconds": STUDENT_SAVE_COOLDOWN_SECONDS},
                },
                ensure_ascii=False,
            )
        )

        await session.broadcast_participants()

        while True:
            msg = await receive_payload()
            if msg is None:
                await client.ws.send_text(
                    json.dumps(
                        {"type": "error", "message": "Получено некорректное WebSocket-сообщение."},
                        ensure_ascii=False,
                    )
                )
                continue
            msg_type = msg.get("type")

            if msg_type == "cursor":
                if client.role != "host" and (
                    not client.can_edit or session.active_editor_id != client.id
                ):
                    continue
                client.cursor_line = clamp_int(msg.get("line"), 1, minimum=1)
                client.cursor_col = clamp_int(msg.get("col"), 1, minimum=1)
                await session.broadcast(
                    {
                        "type": "cursor",
                        "id": client.id,
                        "name": client.name,
                        "color": client.color,
                        "line": client.cursor_line,
                        "col": client.cursor_col,
                    },
                    exclude={client.id},
                )

            elif msg_type == "request_full":
                await client.ws.send_text(
                    json.dumps(
                        {
                            "type": "doc_full",
                            "doc": {"text": session.doc_text, "version": session.version},
                            "filename": session.current_filename,
                        },
                        ensure_ascii=False,
                    )
                )

            elif msg_type == "patch":
                ok, error = await session.apply_patch(
                    client.id,
                    clamp_int(msg.get("baseVersion"), 0, minimum=0),
                    clamp_int(msg.get("start"), 0, minimum=0),
                    clamp_int(msg.get("end"), 0, minimum=0),
                    str(msg.get("text", "")),
                )
                if not ok:
                    await client.ws.send_text(
                        json.dumps({"type": "error", "message": error}, ensure_ascii=False)
                    )
                    await client.ws.send_text(
                        json.dumps(
                            {
                                "type": "doc_full",
                                "doc": {"text": session.doc_text, "version": session.version},
                                "filename": session.current_filename,
                            },
                            ensure_ascii=False,
                        )
                    )

            elif msg_type == "grant_edit":
                if client.role != "host":
                    continue
                target_id = str(msg.get("target_id") or "")
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
                target_id = str(msg.get("target_id") or "")
                if target_id in session.clients:
                    session.clients[target_id].can_edit = False
                    if session.active_editor_id == target_id:
                        session.active_editor_id = None
                await session.broadcast_participants()

            elif msg_type == "set_region":
                if client.role != "host":
                    continue
                target_id = str(msg.get("target_id") or "")
                start_line = clamp_int(msg.get("start_line"), 1, minimum=1)
                end_line = clamp_int(msg.get("end_line"), 1, minimum=1)
                if target_id in session.clients:
                    session.clients[target_id].region = (
                        min(start_line, end_line),
                        max(start_line, end_line),
                    )
                await session.broadcast_participants()

            elif msg_type == "clear_region":
                if client.role != "host":
                    continue
                target_id = str(msg.get("target_id") or "")
                if target_id in session.clients:
                    session.clients[target_id].region = None
                await session.broadcast_participants()

            elif msg_type == "chat":
                text = str(msg.get("text") or "").strip()
                if not text:
                    continue
                now = time.time()
                # While a spam penalty is active, enforce a minimum gap.
                if now < client.chat_spam_until:
                    gap = now - client.chat_last_at
                    if gap < CHAT_SPAM_COOLDOWN_SECONDS:
                        retry_after = int(CHAT_SPAM_COOLDOWN_SECONDS - gap) + 1
                        await client.ws.send_text(json.dumps({
                            "type": "chat_throttled",
                            "retry_after": retry_after,
                            "until": client.chat_spam_until,
                            "message": (
                                f"Антиспам: слишком часто. Подождите {retry_after} с "
                                "перед следующим сообщением."
                            ),
                        }, ensure_ascii=False))
                        continue
                # Record the send and slide the rapid-detection window.
                client.chat_times.append(now)
                cutoff = now - CHAT_RAPID_WINDOW_SECONDS
                client.chat_times = [t for t in client.chat_times if t >= cutoff]
                # A fresh burst of rapid messages arms the 2-minute penalty.
                if len(client.chat_times) >= CHAT_RAPID_THRESHOLD and now >= client.chat_spam_until:
                    client.chat_spam_until = now + CHAT_SPAM_DURATION_SECONDS
                    await client.ws.send_text(json.dumps({
                        "type": "chat_throttled",
                        "retry_after": int(CHAT_SPAM_COOLDOWN_SECONDS),
                        "until": client.chat_spam_until,
                        "message": (
                            "Антиспам включён: обнаружено более "
                            f"{CHAT_RAPID_THRESHOLD} быстрых сообщений. Ближайшие 2 минуты "
                            f"между сообщениями нужно ждать {int(CHAT_SPAM_COOLDOWN_SECONDS)} с."
                        ),
                    }, ensure_ascii=False))
                client.chat_last_at = now
                text = text[:MAX_CHAT_MESSAGE_CHARS]
                await session.broadcast({
                    "type": "chat",
                    "from": client.name,
                    "from_id": client.id,
                    "color": client.color,
                    "text": text,
                })

            elif msg_type == "check_syntax":
                if client.role != "host":
                    continue
                code = str(msg.get("code") or session.doc_text)
                ok, error = check_syntax(code)
                await session.broadcast(
                    {"type": "syntax_result", "ok": ok, "error": error, "by": client.name}
                )

            elif msg_type == "run_code":
                if client.role != "host":
                    continue
                if session.run_task and not session.run_task.done():
                    await client.ws.send_text(
                        json.dumps(
                            {"type": "error", "message": "Код уже выполняется."},
                            ensure_ascii=False,
                        )
                    )
                    continue
                code = str(msg.get("code") or session.doc_text)
                if utf8_size(code) > MAX_DOCUMENT_BYTES:
                    await client.ws.send_text(
                        json.dumps(
                            {"type": "error", "message": "Размер кода превышает 1 МБ."},
                            ensure_ascii=False,
                        )
                    )
                    continue
                safe_to_run, safety_error = validate_code_for_run(code)
                if not safe_to_run:
                    await client.ws.send_text(
                        json.dumps({"type": "error", "message": safety_error}, ensure_ascii=False)
                    )
                    continue
                timeout_s = clamp_float(msg.get("timeout"), 5.0, minimum=1.0, maximum=30.0)
                session.files[session.current_filename] = code
                session.doc_text = code
                session.persist_state()
                session.run_task = asyncio.create_task(
                    run_python_streaming(session, code, timeout_s)
                )

            elif msg_type == "stop_code":
                if client.role != "host":
                    continue
                stopped = await session.stop_running_code()
                if not stopped:
                    await client.ws.send_text(
                        json.dumps(
                            {"type": "error", "message": "Нет активного запуска кода."},
                            ensure_ascii=False,
                        )
                    )

            elif msg_type in {"save_py", "autosave"}:
                code = str(msg.get("code") or session.doc_text)
                if utf8_size(code) > MAX_DOCUMENT_BYTES:
                    await client.ws.send_text(
                        json.dumps(
                            {"type": "save_result", "ok": False, "error": "Размер файла превышает 1 МБ."},
                            ensure_ascii=False,
                        )
                    )
                    continue

                requested_name = str(msg.get("filename") or session.current_filename)
                try:
                    if client.role == "host":
                        filename = session.save_room_file(requested_name, code)
                        payload_out: dict = {
                            "type": "save_result",
                            "ok": True,
                            "filename": filename,
                            "scope": "room_file",
                            "request": msg_type,
                        }
                    else:
                        remaining = session.student_save_cooldown_remaining(client)
                        if remaining > 0:
                            payload_out = {
                                "type": "save_result",
                                "ok": False,
                                "error": f"Личное сохранение доступно через {remaining} сек.",
                                "cooldown_remaining": remaining,
                                "cooldown_seconds": STUDENT_SAVE_COOLDOWN_SECONDS,
                                "scope": "student_file",
                                "request": msg_type,
                            }
                        else:
                            filename = session.save_student_file(client, requested_name, code)
                            payload_out = {
                                "type": "save_result",
                                "ok": True,
                                "filename": filename,
                                "scope": "student_file",
                                "request": msg_type,
                                "cooldown_seconds": STUDENT_SAVE_COOLDOWN_SECONDS,
                            }
                    await client.ws.send_text(json.dumps(payload_out, ensure_ascii=False))
                except Exception as exc:
                    await client.ws.send_text(
                        json.dumps(
                            {"type": "save_result", "ok": False, "error": str(exc), "request": msg_type},
                            ensure_ascii=False,
                        )
                    )

            elif msg_type == "switch_file":
                if client.role != "host":
                    continue
                await session.set_current_file(msg.get("filename") or "main.py")

            elif msg_type == "create_file":
                if client.role != "host":
                    continue
                new_filename = sanitize_filename(
                    msg.get("filename") or f"file_{int(time.time())}.py"
                )
                if new_filename not in session.files:
                    session.files[new_filename] = ""
                    session.ensure_file_blame(new_filename, "")
                    session.persist_state()
                await session.set_current_file(new_filename)

            elif msg_type == "import_file":
                if client.role != "host":
                    continue
                await client.ws.send_text(
                    json.dumps(
                        {
                            "type": "error",
                            "message": (
                                "Импорт по имени файла отключён. "
                                "Используйте загрузку файла с устройства преподавателя через интерфейс."
                            ),
                        },
                        ensure_ascii=False,
                    )
                )

            elif msg_type == "import_file_content":
                if client.role != "host":
                    continue
                imp_filename = sanitize_filename(
                    msg.get("filename") or session.current_filename or "main.py"
                )
                imp_content = str(msg.get("content") or "")
                if utf8_size(imp_content) > MAX_DOCUMENT_BYTES:
                    await client.ws.send_text(
                        json.dumps(
                            {"type": "error", "message": "Размер импортируемого файла превышает 1 МБ."},
                            ensure_ascii=False,
                        )
                    )
                    continue
                session.replace_file_text(imp_filename, imp_content, client)
                await session.broadcast({
                    "type": "doc_full",
                    "doc": {"text": session.doc_text, "version": session.version},
                    "filename": session.current_filename,
                })
                await session.broadcast({
                    "type": "files",
                    "current": session.current_filename,
                    "files": list(session.files.keys()),
                })
                await client.ws.send_text(
                    json.dumps(
                        {"type": "save_result", "ok": True, "filename": imp_filename},
                        ensure_ascii=False,
                    )
                )

            elif msg_type == "ping":
                await client.ws.send_text(
                    json.dumps(
                        {
                            "type": "pong",
                            "ts": msg.get("ts"),
                            "server_ts": int(time.time() * 1000),
                        },
                        ensure_ascii=False,
                    )
                )

            elif msg_type == "latency_update":
                client.latency_ms = clamp_int(
                    msg.get("latency_ms"), 0, minimum=0, maximum=9999
                )
                await session.broadcast_participants()

    except (WebSocketDisconnect, OSError, RuntimeError):
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
                if session.clients:
                    await session.schedule_host_expiration()
                else:
                    sessions.pop(session.room_id, None)
            elif session.clients:
                await session.broadcast_participants()
            else:
                if session.host_reconnect_deadline is None:
                    sessions.pop(session.room_id, None)
                else:
                    session.persist_state()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--hash-password":
        try:
            print(hash_password_bcrypt(" ".join(sys.argv[2:])))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
    else:
        port = 8000
        reload_enabled = (
            os.environ.get("ONLINECOMPILE_RELOAD", "").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        ws_protocol = preferred_ws_protocol()
        configure_websocket_logging(ws_protocol)
        print("Сервер onlinecompile запускается. Открыть можно по адресам:", file=sys.stderr)
        for url in get_local_access_urls(port):
            print(f" - {url}", file=sys.stderr)
        if reload_enabled:
            print(
                "Автоперезагрузка включена через ONLINECOMPILE_RELOAD=1. "
                "Папка room_data исключена из наблюдения.",
                file=sys.stderr,
            )
        else:
            print(
                "Автоперезагрузка отключена по умолчанию, "
                "чтобы автосохранение не вызывало перезапуск сервера.",
                file=sys.stderr,
            )
        uvicorn.run(
            "server:app",
            host="0.0.0.0",
            port=port,
            reload=reload_enabled,
            ws=ws_protocol,
            ws_ping_interval=20.0,
            ws_ping_timeout=20.0,
            reload_includes=["*.py", "*.html", "*.css", "*.js", "*.json"],
            reload_excludes=["room_data/*", "room_data/**/*", "*.csv", "*.tmp"],
        )
