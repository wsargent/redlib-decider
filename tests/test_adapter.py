import json

from fastapi.testclient import TestClient

import adapter.codex_adapter as adapter


def test_adapter_validates_input():
    with TestClient(adapter.app) as client:
        response = client.post("/decide", json={"posts": []})
    assert response.status_code == 400


def test_adapter_returns_structured_decisions(monkeypatch):
    def fake_run(command, **kwargs):
        output = command[command.index("--output-last-message") + 1]
        with open(output, "w") as file:
            json.dump({"decisions": [{"id": "a", "allowed": True}]}, file)

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(adapter.subprocess, "run", fake_run)
    with TestClient(adapter.app) as client:
        response = client.post("/decide", json={"posts": [{"id": "a", "text": "A post"}]})
    assert response.status_code == 200
    assert response.json()["decisions"][0]["probability"] == 1.0


def test_adapter_rejects_incomplete_decisions(monkeypatch):
    def fake_run(command, **kwargs):
        output = command[command.index("--output-last-message") + 1]
        with open(output, "w") as file:
            json.dump({"decisions": []}, file)

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(adapter.subprocess, "run", fake_run)
    with TestClient(adapter.app) as client:
        response = client.post("/decide", json={"posts": [{"id": "a", "text": "A post"}]})
    assert response.status_code == 502
