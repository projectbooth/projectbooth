# Single-stage, same as catalog-service/Dockerfile — no system deps beyond
# what python:3.12-slim already has (pyjwt[crypto] pulls in `cryptography`,
# which ships prebuilt wheels for this platform; no compiler needed).
FROM python:3.12-slim AS base

WORKDIR /srv

COPY pyproject.toml ./
COPY app ./app

RUN pip install --no-cache-dir --root-user-action=ignore .

# Runs as a non-root uid — no reason for this process to be root, and
# nothing here needs to bind under 1024 (uvicorn's default 8000, though
# gateway itself listens on 8080 — see gateway.yaml's Deployment/Service).
RUN useradd --system --uid 10001 gateway
USER gateway

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
