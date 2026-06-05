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
<<<<<<< HEAD
=======


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
>>>>>>> 100d6e0 (ver. 1.2: offline (no)payment terminal)
