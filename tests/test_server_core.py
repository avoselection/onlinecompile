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

    ok, error = await session.apply_patch(host.id, session.version, 0, 0, "x" * (server.MAX_DOCUMENT_BYTES + 1))
    assert not ok
    assert "1 МБ" in error
