FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

RUN useradd --create-home --uid 10001 mileaf \
    && mkdir -p /data \
    && chown mileaf:mileaf /data
USER mileaf

VOLUME ["/data"]

CMD ["mileaf-agent", "run"]
