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



def test_host_student_flow_and_student_cannot_save_shared_file(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DATA_DIR", str(tmp_path))
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

            student_ws.send_json({"type": "save_py", "filename": "hack.py", "code": "print(123)"})
            save_result = recv_until(student_ws, lambda msg: msg.get("type") == "save_result")
            assert save_result["ok"] is False

            host_ws.send_json({"type": "grant_edit", "target_id": student_welcome["you"]["id"]})
            recv_until(
                student_ws,
                lambda msg: msg.get("type") == "participants" and any(
                    participant["id"] == student_welcome["you"]["id"] and participant["can_edit"]
                    for participant in msg["participants"]
                ),
            )

            student_ws.send_json({
                "type": "patch",
                "baseVersion": student_welcome["doc"]["version"],
                "start": 0,
                "end": 0,
                "text": "print(42)\n",
            })
            update = recv_until(student_ws, lambda msg: msg.get("type") == "doc_update")
            assert update["version"] == student_welcome["doc"]["version"] + 1
