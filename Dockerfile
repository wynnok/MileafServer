FROM python:3.12-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --no-cache

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
RUN groupadd --gid 10001 mileaf \
    && useradd --uid 10001 --gid mileaf --no-create-home mileaf \
    && mkdir /data && chown mileaf:mileaf /data && chmod 700 /data
COPY --from=builder /app/.venv /app/.venv
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; port=os.environ.get('MILEAF_LISTEN_ADDRESS','0.0.0.0:8080').rsplit(':',1)[1]; urllib.request.urlopen('http://localhost:'+port+'/v1/health', timeout=3).close()"
CMD ["mileaf-server"]
