FROM python:3.14.2-slim-bookworm@sha256:5b500436b051b112e0e834533a4d42a28e4a79c7a5c10f3fd44b47ba1b487995 AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
ENV APP_DATA_PROJECT_ROOT=/app
WORKDIR /app

FROM base AS dependencies
RUN --mount=type=bind,source=.docker-wheels,target=/wheels,readonly \
    --mount=type=bind,source=requirements-runtime.lock,target=/tmp/requirements.lock,readonly \
    python -m pip install --no-cache-dir --no-compile --no-index \
        --find-links=/wheels --prefix=/install -r /tmp/requirements.lock

FROM base AS application
RUN --mount=type=bind,source=.docker-wheels,target=/wheels,readonly \
    python -m pip install --no-cache-dir --no-index --find-links=/wheels setuptools==82.0.1
COPY pyproject.toml ./
COPY src ./src
RUN python -m pip install --no-deps --no-build-isolation --no-compile --prefix=/install .

FROM base AS runtime
RUN useradd --uid 10001 --no-create-home appdata \
    && mkdir -p /data/board-library /data/app-releases \
    && chown 10001:10001 /data/board-library /data/app-releases
COPY --link --from=dependencies /install/ /usr/local/
COPY --link --from=application /install/ /usr/local/
COPY --link pyproject.toml alembic.ini ./
COPY --link migrations ./migrations
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app_data_service.api:from_environment", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-proxy-headers"]
