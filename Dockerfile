# Single image, single origin. The frontend is built here and served by the
# same FastAPI process that serves the API, which is why there is no CORS
# middleware and no API base URL anywhere in the client: api.js uses relative
# paths and ws.js derives the WebSocket URL from window.location, so both
# resolve to whatever host this container is reachable at.

# ---- stage 1: build the frontend -------------------------------------------
FROM node:20-slim AS frontend

WORKDIR /app/frontend
# Copy manifests first so `npm ci` is cached and only re-runs when deps change.
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


# ---- stage 2: the runtime ---------------------------------------------------
FROM python:3.12-slim AS runtime

# Unbuffered so uvicorn's logs reach Railway's log view as they happen rather
# than being held in a pipe buffer; no .pyc for a container that never reruns.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ ./backend/
# Only the built assets, not the frontend source or node_modules.
COPY --from=frontend /app/frontend/dist ./frontend/dist

# The event log lives on the container filesystem, which is ephemeral here.
# That is deliberate for a demo: every redeploy comes back to the seeded six
# hospitals with an empty log, and there is nothing to clean up afterwards.
ENV DB_PATH=/tmp/diverttrack.db

WORKDIR /app/backend
# Railway injects $PORT; the default keeps `docker run -p 8000:8000` working.
# Shell form on purpose — $PORT has to be expanded at runtime, and exec form
# would pass the literal string "$PORT" to uvicorn.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
