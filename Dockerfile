FROM python:3.14.2-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
ENV APP_DATA_PROJECT_ROOT=/app
WORKDIR /app
COPY requirements-runtime.lock ./
RUN --mount=type=bind,source=.docker-wheels,target=/wheels,readonly python -m pip install --no-cache-dir --no-index --find-links=/wheels -r requirements-runtime.lock
COPY pyproject.toml alembic.ini ./
COPY src ./src
COPY migrations ./migrations
RUN python -m pip install --no-deps --no-build-isolation . && useradd --uid 10001 --no-create-home appdata \
    && mkdir -p /data/board-library && chown 10001:10001 /data/board-library
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app_data_service.api:from_environment", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-proxy-headers"]
