# DriftGuard — zero-dependency container.
# The package is pure Python stdlib; this image just provides a runtime.
# Build:  docker build -t driftguard:0.5.0 .
# Run:    docker run --rm -v "$PWD:/pipeline" driftguard:0.5.0 \
#             refactor plan /pipeline --max-risk suggested
FROM python:3.11-slim

WORKDIR /app

COPY driftguard/ ./driftguard/

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m", "driftguard"]
CMD ["--version"]