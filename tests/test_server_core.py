import asyncio
import json

import pytest

import server


class DummyWebSocket:
    def __init__(self):
        self.messages = []

    async def send_text(self, message: str):
        self.messages.append(json.loads(message))

    async def close(self):
        return None


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DATA_DIR", str(tmp_path))
    server.sessions.clear()
    yield
    server.sessions.clear()


def test_sanitize_filename_blocks_paths_and_keeps_python_suffix():
    assert server.sanitize_filename("../../evil") == "evil.py"
    assert server.sanitize_filename("folder\\script.py") == "script.py"
    assert server.sanitize_filename("CON.py") == "CON_file.py"
    assert server.sanitize_filename("x" * 200).endswith(".py")
    assert len(server.sanitize_filename("x" * 200)) <= server.MAX_FILENAME_LENGTH


def test_empty_room_directories_are_not_persisted_data(tmp_path):
    room_dir = tmp_path / server.sanitize_room_id("demo")
    (room_dir / "reports").mkdir(parents=True)
    (room_dir / "students").mkdir(parents=True)
    assert not server.room_has_persisted_data("demo")


def test_syntax_check_rejects_excessive_bracket_depth():
    code = "x = " + ("(" * (server.MAX_AST_BRACKET_DEPTH + 1)) + "1" + (")" * (server.MAX_AST_BRACKET_DEPTH + 1))
    ok, error = server.check_syntax(code)

    assert not ok
    assert "вложенность" in error.lower()


def test_run_validation_blocks_unsafe_file_and_process_access():
    unsafe_samples = [
        "import os\nos.system('echo unsafe')\n",
        "print(open('config.json').read())\n",
        "name = input('Введите имя: ')\n",
        "__builtins__.__dict__['open']('config.json').read()\n",
    ]

    for code in unsafe_samples:
        ok, error = server.validate_code_for_run(code)
        assert not ok, f"Expected block for: {code!r}"
        assert error


def test_run_validation_allows_simple_python_code():
    ok, error = server.validate_code_for_run("import math\nprint(math.sqrt(9))\n")

    assert ok, error


@pytest.mark.asyncio
async def test_student_patch_requires_granted_active_editor():
    session = server.Session("demo")
    student = server.Client(ws=DummyWebSocket(), name="Student", role="student", color="#000")
    session.clients[student.id] = student

    ok, error = await session.apply_patch(student.id, session.version, 0, 0, "print(1)\n")
    assert not ok
    assert "недоступно" in error.lower()

    student.can_edit = True
    session.active_editor_id = student.id
    version_before = session.version
    ok, error = await session.apply_patch(student.id, version_before, 0, 0, "print(1)\n")

    assert ok, error
    assert session.version == version_before + 1
    assert session.doc_text.startswith("print(1)")
    assert session.student_metrics[student.name]["last_edit_at"]


@pytest.mark.asyncio
async def test_patch_size_limit_is_enforced():
    session = server.Session("demo-size")
    host = server.Client(ws=DummyWebSocket(), name="Host", role="host", color="#000", can_edit=True)
    session.clients[host.id] = host

    ok, error = await session.apply_patch(
        host.id, session.version, 0, 0, "x" * (server.MAX_DOCUMENT_BYTES + 1)
    )
    assert not ok
    assert "1 МБ" in error


@pytest.mark.asyncio
async def test_stop_running_code_terminates_active_process(tmp_path):
    session = server.Session("stop-demo")
    host = server.Client(ws=DummyWebSocket(), name="Host", role="host", color="#000", can_edit=True)
    session.clients[host.id] = host

    session.run_task = asyncio.create_task(
        server.run_python_streaming(
            session, "import time\nwhile True:\n    time.sleep(0.1)\n", 30
        )
    )

    for _ in range(50):
        if session.running_process is not None:
            break
        await asyncio.sleep(0.05)

    assert session.running_process is not None
    assert await session.stop_running_code() is True
    await asyncio.wait_for(session.run_task or asyncio.sleep(0), timeout=3)

    run_results = [m for m in host.ws.messages if m.get("type") == "run_result"]
    assert any(m.get("stopped") is True for m in run_results)
    assert session.running_process is None


@pytest.mark.asyncio
async def test_host_delete_patch_restores_prior_text_and_broadcasts_deletion():
    session = server.Session("delete-demo")
    host = server.Client(ws=DummyWebSocket(), name="Host", role="host", color="#000", can_edit=True)
    student = server.Client(ws=DummyWebSocket(), name="Student", role="student", color="#111")
    session.clients[host.id] = host
    session.clients[student.id] = student

    original_text = session.doc_text
    inserted = "temporary line\n"

    ok, error = await session.apply_patch(host.id, session.version, 0, 0, inserted)
    assert ok, error
    version_after_insert = session.version
    assert session.doc_text.startswith(inserted)

    ok, error = await session.apply_patch(host.id, version_after_insert, 0, len(inserted), "")
    assert ok, error
    assert session.doc_text == original_text

    deletion_updates = [
        m for m in student.ws.messages
        if m.get("type") == "doc_update" and m.get("version") == version_after_insert + 1
    ]
    assert deletion_updates
    assert deletion_updates[-1]["patch"] == {"start": 0, "end": len(inserted), "text": ""}


@pytest.mark.asyncio
async def test_next_color_wraps_around_and_never_raises():
    """Color cycle should wrap instead of raising StopIteration."""
    session = server.Session("color-demo")
    # Request more colours than the palette contains.
    colors = [session.next_color() for _ in range(len(server._CLIENT_COLORS) * 2 + 1)]
    assert all(isinstance(c, str) and c.startswith("#") for c in colors)


@pytest.mark.asyncio
async def test_persist_state_debounce_coalesces_rapid_edits(tmp_path, monkeypatch):
    """schedule_persist should result in at most one disk write per debounce window."""
    monkeypatch.setattr(server, "DATA_DIR", str(tmp_path))
    write_count = 0
    original_persist = server.Session.persist_state

    def counting_persist(self):
        nonlocal write_count
        write_count += 1
        original_persist(self)

    monkeypatch.setattr(server.Session, "persist_state", counting_persist)

    session = server.Session("debounce-demo")
    host = server.Client(ws=DummyWebSocket(), name="Host", role="host", color="#000", can_edit=True)
    session.clients[host.id] = host

    write_count = 0  # reset after __init__

    for i in range(5):
        await session.apply_patch(host.id, session.version, 0, 0, f"# line {i}\n")

    # Allow the debounce timer to fire.
    await asyncio.sleep(server.PERSIST_DEBOUNCE_SECONDS + 0.1)

    # Five rapid patches should produce far fewer than 5 disk writes.
    assert write_count < 5
