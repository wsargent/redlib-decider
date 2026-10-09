#!/usr/bin/env python3
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException

CODEX_MODEL = os.getenv("CODEX_MODEL", "gpt-6-luna")
CODEX_TIMEOUT = int(os.getenv("CODEX_TIMEOUT", "120"))
SCHEMA = Path(__file__).with_name("decision_schema.json")
app = FastAPI(title="Codex Decision Adapter")


@app.get("/health")
def health():
    return {"status": "ok", "model": CODEX_MODEL}


@app.post("/decide")
def decide(payload: dict):
    posts = payload.get("posts")
    if not isinstance(posts, list) or not posts or any(
        not isinstance(post, dict)
        or not isinstance(post.get("id"), str)
        or not isinstance(post.get("text"), str)
        for post in posts
    ):
        raise HTTPException(400, "posts must be a non-empty list of {id, text} objects")

    items = "\n\n".join(f"POST {post['id']}:\n{post['text']}" for post in posts)
    prompt = (
        "Classify every Reddit post below. Return one decision for every supplied ID. "
        "allowed is true only when the post has meaningful substance and is not substantially "
        "about politics, death or grief, divorce or breakups, or cute animals. "
        "Do not omit, rename, or invent IDs.\n\n" + items
    )
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "result.json"
        command = [
            "codex",
            "exec",
            prompt,
            "--model",
            CODEX_MODEL,
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--output-schema",
            str(SCHEMA),
            "--output-last-message",
            str(output),
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=CODEX_TIMEOUT, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(504, "Codex timed out") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout)[-2000:]
            raise HTTPException(503, detail or "Codex failed")
        try:
            result = json.loads(output.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(502, f"Invalid Codex response: {exc}") from exc

    try:
        decisions = result["decisions"]
        expected = {post["id"] for post in posts}
        actual = {decision["id"] for decision in decisions}
        if actual != expected or len(decisions) != len(posts):
            raise ValueError("Codex returned an incomplete decision set")
        for decision in decisions:
            decision["probability"] = 1.0 if bool(decision["allowed"]) else 0.0
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(502, f"Invalid Codex response: {exc}") from exc
    return {
        "decisions": decisions,
        "model": CODEX_MODEL,
        "duration_seconds": time.perf_counter() - started,
    }
