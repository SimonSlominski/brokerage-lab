FROM python:3.14-slim AS runtime
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
COPY requirements/base.txt requirements/base.txt
RUN pip install --no-cache-dir -r requirements/base.txt
COPY pyproject.toml ./
COPY src ./src
COPY migrations ./migrations
COPY partner_migrations ./partner_migrations
COPY alembic.ini partner-alembic.ini ./
COPY docker/entrypoint.sh /app/docker/entrypoint.sh
RUN pip install --no-cache-dir --no-deps .
EXPOSE 8000
ENTRYPOINT ["/bin/sh", "/app/docker/entrypoint.sh"]
CMD ["uvicorn", "brokerage_lab.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS test
COPY requirements/dev.txt requirements/dev.txt
RUN pip install --no-cache-dir -r requirements/dev.txt
COPY tests ./tests
COPY ruff.toml ./
CMD ["python", "-m", "pytest", "--postgres", "-q"]
