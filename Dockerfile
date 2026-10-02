FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml ./
COPY ai_ops ./ai_ops
RUN pip install --no-cache-dir build && python -m build --wheel

FROM build AS test
COPY tests ./tests
RUN pip install --no-cache-dir '.[test]' && pytest -q

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 AI_OPS_HOST=0.0.0.0 AI_OPS_PORT=8765 AI_OPS_DB=/data/control.db
RUN groupadd --gid 10001 aiops && useradd --uid 10001 --gid 10001 --no-create-home aiops && mkdir /data && chown aiops:aiops /data
COPY --from=build /src/dist/*.whl /tmp/wheels/
RUN pip install --no-cache-dir /tmp/wheels/*.whl && rm -r /tmp/wheels
USER 10001:10001
EXPOSE 8765
HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz',timeout=2)" || exit 1
ENTRYPOINT ["ai-ops-service"]
