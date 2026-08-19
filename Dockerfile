FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install --no-install-recommends --yes \
        ca-certificates \
        fonts-liberation \
        libcairo2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

COPY templates ./templates

RUN groupadd --gid 10001 wlc-manager \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin wlc-manager \
    && mkdir --parents /app/data /app/artifacts \
    && chown --recursive wlc-manager:wlc-manager /app/data /app/artifacts

USER 10001:10001

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["wlc-manager", "--config", "/config/config.yaml", "healthcheck"]

ENTRYPOINT ["wlc-manager"]
CMD ["--config", "/config/config.yaml", "run"]


FROM base AS test

USER root
RUN python -m pip install --no-cache-dir ".[dev]"
COPY config.example.yaml ./config.example.yaml
COPY tests ./tests
RUN python -m ruff check . \
    && python -m pytest --cov=wlc_manager --cov-report=term-missing


FROM base AS runtime
