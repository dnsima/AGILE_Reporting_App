# AGILE Reporting Platform
#
# Multi-stage so the runtime image carries the installed packages and the
# application, not a compiler toolchain.

FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /install
COPY requirements.txt .
# psycopg is not in requirements.txt because a local SQLite run does not need
# it; a deployment does.
RUN pip install --prefix=/install/deps -r requirements.txt "psycopg[binary]>=3.1"


FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/deps/bin:$PATH" \
    PYTHONPATH="/deps/lib/python3.12/site-packages:/app"

# curl is here for the container healthcheck and nothing else.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 agile

COPY --from=build /install/deps /deps

WORKDIR /app
COPY app ./app
COPY scripts ./scripts
COPY seeds ./seeds
COPY pyproject.toml ./

# Uploaded returns, evidence and generated reports live here. Mount a volume
# over it: a container is disposable and this is the record.
RUN mkdir -p /app/storage/uploads /app/storage/reports \
 && chown -R agile:agile /app/storage

USER agile
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/v1/health || exit 1

# Workers, not --reload. Two is right for a small VPS; raise it with the box.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
