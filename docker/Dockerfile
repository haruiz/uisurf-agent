FROM python:3.12-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV DISPLAY=:1
ENV UV_SYSTEM_PYTHON=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y \
    curl \
    chromium \
    xfce4 \
    xfce4-terminal \
    xfce4-goodies \
    thunar \
    mousepad \
    dbus-x11 \
    dbus \
    tigervnc-standalone-server \
    tigervnc-common \
    novnc \
    websockify \
    python3-venv \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

COPY pyproject.toml uv.lock README.md /app/
COPY src /app/src
COPY docker/start.sh /app/start.sh
COPY docker/start_browser.sh /app/start_browser.sh
COPY docker/xstartup /app/xstartup

RUN uv sync --frozen
RUN uv run playwright install --with-deps chromium

RUN chmod +x /app/start.sh /app/start_browser.sh /app/xstartup

EXPOSE 6080
EXPOSE 8001
EXPOSE 8002
EXPOSE 9222

CMD ["/app/start.sh"]
