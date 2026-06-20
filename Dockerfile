# ── Stage 1: build the React web UI ─────────────────────────────────────────
FROM node:22-slim AS web-builder

WORKDIR /web
# Install deps first (layer-cached separately from source)
COPY web/package.json web/package-lock.json ./
RUN npm ci --include=dev

# Copy source and build
COPY web/ ./
RUN npm run build
# Output: /web/dist/

# ── Stage 2: Python runtime with web assets ──────────────────────────────────
FROM python:3.12-slim AS runtime

WORKDIR /app

# Install Python package in editable mode (includes src/ + server/ + pyproject.toml)
COPY pyproject.toml ./
COPY src/ ./src/
COPY server/ ./server/
COPY alembic.ini ./

RUN pip install --no-cache-dir -e ".[server]"

# Copy the compiled web assets from the builder stage.
# FastAPI mounts web/dist at / via StaticFiles; the directory must exist.
COPY --from=web-builder /web/dist ./web/dist

# Non-root user for defence in depth
RUN useradd --no-create-home --shell /bin/false appuser && \
    chown -R appuser:appuser /app
USER appuser

# Expose the default port (Railway overrides via $PORT / CCSWITCH_PORT)
EXPOSE 8787

# Uvicorn serves both the API and the built web assets (served by FastAPI StaticFiles)
CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8787"]
