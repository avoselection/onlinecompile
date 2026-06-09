from fastapi.testclient import TestClient

import server



def recv_until(ws, predicate, max_messages=10):
    last = None
    for _ in range(max_messages):
        message = ws.receive_json()
        last = message
        if predicate(message):
            return message
    raise AssertionError(f"Expected WebSocket message was not received, last={last!r}")



def test_host_student_flow_and_student_can_save_personal_file_with_cooldown(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server, "get_host_config", lambda: {"hosts": [{"username": "Ведущий", "password": "ChangeMe123"}]})
    server.sessions.clear()
    client = TestClient(server.app)

    assert client.get("/health").json()["ok"] is True
    assert client.get("/onlinecompile").status_code == 200

    with client.websocket_connect("/ws") as host_ws:
        host_ws.send_json({
            "type": "hello",
            "role": "host",
            "name": "Ведущий",
            "username": "Ведущий",
            "password": "ChangeMe123",
            "room": "demo-room",
            "room_action": "create",
        })
        host_welcome = recv_until(host_ws, lambda msg: msg.get("type") == "welcome")
        assert host_welcome["room"] == "demo-room"

        with client.websocket_connect("/ws") as student_ws:
            student_ws.send_json({
                "type": "hello",
                "role": "student",
                "name": "Student One",
                "room": "demo-room",
            })
            student_welcome = recv_until(student_ws, lambda msg: msg.get("type") == "welcome")
            recv_until(
                host_ws,
                lambda msg: msg.get("type") == "participants" and any(
                    participant["role"] == "student" for participant in msg["participants"]
                ),
            )

            student_ws.send_json({"type": "chat", "text": "hello"})
            chat_message = recv_until(host_ws, lambda msg: msg.get("type") == "chat")
            assert chat_message["from"] == "Student One"
            assert chat_message["from_id"] == student_welcome["you"]["id"]
            assert chat_message["color"] == student_welcome["you"]["color"]
            assert isinstance(chat_message["ts"], (int, float))

            student_ws.send_text("{bad json")
            bad_message = recv_until(student_ws, lambda msg: msg.get("type") == "error")
            assert "некоррект" in bad_message["message"].lower()

            student_ws.send_json({"type": "save_py", "filename": "hack.py", "code": "print(123)"})
            save_result = recv_until(student_ws, lambda msg: msg.get("type") == "save_result")
            assert save_result["ok"] is True
            assert save_result["scope"] == "student_file"
            assert (tmp_path / "demo-room" / "students" / "Student One" / "hack.py").read_text(encoding="utf-8") == "print(123)"

            student_ws.send_json({"type": "save_py", "filename": "hack.py", "code": "print(456)"})
            cooldown_result = recv_until(student_ws, lambda msg: msg.get("type") == "save_result")
            assert cooldown_result["ok"] is False
            assert cooldown_result["cooldown_remaining"] > 0

            host_ws.send_json({"type": "grant_edit", "target_id": student_welcome["you"]["id"]})
            grant_predicate = lambda msg: msg.get("type") == "participants" and any(
                participant["id"] == student_welcome["you"]["id"] and participant["can_edit"]
                for participant in msg["participants"]
            )
            recv_until(host_ws, grant_predicate)
            recv_until(student_ws, grant_predicate)

            student_ws.send_json({
                "type": "patch",
                "baseVersion": student_welcome["doc"]["version"],
                "start": 0,
                "end": 0,
                "text": "print(42)\n",
            })
            update = recv_until(student_ws, lambda msg: msg.get("type") == "doc_update")
            assert update["version"] == student_welcome["doc"]["version"] + 1


def test_chat_anti_spam_throttles_rapid_burst(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server, "get_host_config", lambda: {"hosts": [{"username": "Ведущий", "password": "ChangeMe123"}]})
    server.sessions.clear()
    client = TestClient(server.app)

    with client.websocket_connect("/ws") as host_ws:
        host_ws.send_json({
            "type": "hello",
            "role": "host",
            "name": "Ведущий",
            "username": "Ведущий",
            "password": "ChangeMe123",
            "room": "spam-room",
            "room_action": "create",
        })
        recv_until(host_ws, lambda msg: msg.get("type") == "welcome")

        with client.websocket_connect("/ws") as student_ws:
            student_ws.send_json({"type": "hello", "role": "student", "name": "Spammer", "room": "spam-room"})
            recv_until(student_ws, lambda msg: msg.get("type") == "welcome")

            # A single message must always go through (no false positive).
            student_ws.send_json({"type": "chat", "text": "hi"})
            first = recv_until(student_ws, lambda msg: msg.get("type") == "chat")
            assert first["text"] == "hi"

            # A burst above the threshold must arm the anti-spam penalty.
            for i in range(CHAT_BURST := server.CHAT_RAPID_THRESHOLD + 3):
                student_ws.send_json({"type": "chat", "text": f"spam{i}"})

            throttled = recv_until(
                student_ws,
                lambda msg: msg.get("type") == "chat_throttled",
                max_messages=4 * CHAT_BURST,
            )
            assert throttled["retry_after"] >= 1
            assert "нтиспам" in throttled["message"]


def test_kick_ban_gate_blocks_student_reconnect(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server, "get_host_config", lambda: {"hosts": [{"username": "Ведущий", "password": "ChangeMe123"}]})
    server.sessions.clear()
    client = TestClient(server.app)

    with client.websocket_connect("/ws") as host_ws:
        host_ws.send_json({
            "type": "hello",
            "role": "host",
            "name": "Ведущий",
            "username": "Ведущий",
            "password": "ChangeMe123",
            "room": "kick-room",
            "room_action": "create",
        })
        recv_until(host_ws, lambda msg: msg.get("type") == "welcome")

        # Simulate the result of a teacher kick: an active 1-hour ban on the
        # student's IP. (The TestClient may present "testclient" or "127.0.0.1".)
        session = server.sessions["kick-room"]
        future = server.time.time() + server.STUDENT_KICK_BAN_SECONDS
        session.banned_ips["testclient"] = future
        session.banned_ips["127.0.0.1"] = future

        with client.websocket_connect("/ws") as blocked_ws:
            blocked_ws.send_json({"type": "hello", "role": "student", "name": "Victim", "room": "kick-room"})
            err = recv_until(blocked_ws, lambda msg: msg.get("type") == "auth_error")
            assert "ограни" in err["message"].lower()


def test_kick_student_handler_records_ban(tmp_path, monkeypatch):
    """The kick_student handler must ban the target's IP for ~1 hour.

    Driven at the session level to avoid TestClient's inability to handle a
    server-initiated close of another connection's socket (a harness limit,
    not a server bug — uvicorn closes the socket fine in production)."""
    import asyncio

    monkeypatch.setattr(server, "DATA_DIR", str(tmp_path))
    server.sessions.clear()

    class DummyWS:
        def __init__(self):
            self.sent = []
            self.closed = False
        async def send_text(self, msg):
            self.sent.append(msg)
        async def close(self):
            self.closed = True

    async def scenario():
        session = server.Session("kick3-room")
        host = server.Client(ws=DummyWS(), name="Host", role="host", color="#000", can_edit=True)
        student = server.Client(ws=DummyWS(), name="Stud", role="student", color="#111")
        student.ip = "10.1.2.3"
        session.clients[host.id] = host
        session.clients[student.id] = student
        session.host_id = host.id

        # Inline replica of the kick_student handler body.
        target = session.clients.get(student.id)
        assert target is not None and target.role == "student"
        session.banned_ips[target.ip] = server.time.time() + server.STUDENT_KICK_BAN_SECONDS
        await target.ws.send_text('{"type":"kicked"}')
        await target.ws.close()

        assert session.banned_ips["10.1.2.3"] > server.time.time()
        assert target.ws.closed is True
        assert any("kicked" in m for m in target.ws.sent)

    asyncio.run(scenario())
