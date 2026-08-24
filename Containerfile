FROM ghcr.io/astral-sh/uv:0.12.5-python3.12-trixie-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BOT_STATE_FILE=/data/split-bot.pickle \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./

RUN uv sync --locked --no-dev --no-install-project \
    && mkdir -p /data

COPY mrcga_bot.py ./

CMD ["/app/.venv/bin/python", "mrcga_bot.py"]
