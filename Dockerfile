FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends universal-ctags git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
COPY swagger_agent/ swagger_agent/
RUN pip install --no-cache-dir .

COPY tests/golden/ tests/golden/

VOLUME ["/app/.cache", "/app/outputs"]

ENTRYPOINT ["python", "-m", "swagger_agent"]
