FROM python:3.14-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# Import mounted development source before the installed package.
ENV PYTHONPATH=/app/src
COPY requirements/base.txt ./requirements/base.txt
COPY pyproject.toml ./
RUN pip install --no-cache-dir -r requirements/base.txt
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
COPY docker/entrypoint.sh /app/docker/entrypoint.sh
RUN pip install --no-cache-dir --no-deps .
EXPOSE 8000
ENTRYPOINT ["/bin/sh", "/app/docker/entrypoint.sh"]
CMD ["uvicorn", "brokerage_lab.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
