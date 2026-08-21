FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml README.md ./
COPY src ./src

RUN uv pip install --system .

# NOTE: host.docker.internal resolves on Docker Desktop (Win/Mac) but NOT on Linux
# by default. On Linux, either override MEM0_BASE_URL at runtime or start the
# container with: --add-host=host.docker.internal:host-gateway
ENV PORT=8081 \
    MEM0_BASE_URL=http://host.docker.internal:8888

CMD ["python", "-m", "mem0_mcp_server.http_entry"]
