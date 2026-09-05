"""FastAPI entrypoint. Routes are added phase by phase; see the API list in
the project README for the full surface built in Phase 8."""
from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="DivertTrack")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
