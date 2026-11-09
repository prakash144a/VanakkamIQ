FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install .
COPY alembic.ini ./
COPY alembic ./alembic

# --- test stage: the image only builds if every test passes ---
FROM base AS test
COPY tests ./tests
RUN pip install ".[dev]" && python -m pytest -q -p no:cacheprovider && touch /srv/.tests-passed

# --- runtime image ---
FROM base AS runtime
COPY --from=test /srv/.tests-passed /srv/.tests-passed
COPY scripts/start.sh ./start.sh
RUN chmod +x start.sh && useradd --create-home appuser
USER appuser

EXPOSE 8000
CMD ["./start.sh"]
