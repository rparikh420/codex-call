FROM ghcr.io/astral-sh/uv:0.9.24 AS uv
FROM python:3.12-slim-bookworm

# The installed SDK CLI can import sounddevice, which loads system PortAudio.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libportaudio2 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_CACHE_DIR=/tmp/uv-cache \
    PATH="/app/.venv/bin:$PATH" \
    CODEX_CALL_DATABASE_PATH=/data/calls.sqlite3
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ ./src/
RUN uv sync --locked --no-dev \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin caller \
    && mkdir /data \
    && chown caller:caller /data \
    && chmod 700 /data
USER caller
EXPOSE 8000
CMD ["codex-call-api", "--host", "0.0.0.0", "--port", "8000"]
