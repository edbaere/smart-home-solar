# Shared image for the controller, dry-run publisher, and day-ahead refresh -- same package,
# different entrypoints (set via `command:` per service in deploy/docker-compose.yml).
FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir '.[hw,mqtt]'

ENTRYPOINT ["python", "-m"]
