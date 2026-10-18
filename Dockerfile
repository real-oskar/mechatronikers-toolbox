# Root build (D77): what `docker build .` checks on a plain Linux machine with no secrets, no
# Podman and no model access. It fails if any CI check fails:
#
#   ui       frontend/: npm ci, ESLint, Vitest, tsc + Vite build (static review UI, D55)
#   check    ruff check, ruff format --check, mypy (strict), pytest: the same checks as CI
#   runtime  the session service: drawing-mcp, cad-mcp and circuit-mcp (streamable HTTP), review
#            API and review UI on port 8000. It copies a marker from `check`, so a builder that
#            skips unused stages (BuildKit) still runs every check.
#
# Tests run against scripted fake models: no API key, no network, no credits. When the checkout has
# only Git LFS pointers for data/ (no `git lfs pull`), the tests on the sample drawings skip with a
# reason, as in CI. The real pipeline runs the same service as a rootless Podman sandbox per session
# (deploy/containers/Containerfile) and the agent on the host; see README.md.
#
# CI builds this file too (.github/workflows/judge-build.yml).
#
#   docker build -t mechatronikers .
#   docker run --rm -p 127.0.0.1:8000:8000 mechatronikers      # then: curl localhost:8000/health
#
# The runtime image has no sandbox isolation (no auth, no gateway, script tools in a plain child
# process with network access): bind it to 127.0.0.1 and use it for inspection only.

FROM docker.io/library/node:22.23.2-alpine AS ui
WORKDIR /repo/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# The UI tests share this recorded run with the Python tests (tests/test_run_table.py).
COPY tests/fixtures/events /repo/tests/fixtures/events
RUN npm run lint && npm test && npm run build

FROM docker.io/library/python:3.13-slim-bookworm AS base
# OpenCascade (OCP wheels) needs these shared libraries even when nothing is rendered.
# ngspice: operating-point check of exported netlists (circuit add-on, docs/12).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libxrender1 libxext6 libsm6 \
       ngspice \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

FROM base AS check
# git: the test bench and session manager record the commit (there is none here, which they allow).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
RUN uv sync --locked --no-install-project
COPY src ./src
COPY tests ./tests
# The Fusion 360 add-in is not part of the package; tests/test_fusion_addin.py loads it from here.
COPY F360_extension ./F360_extension
COPY scripts ./scripts
COPY data ./data
RUN uv sync --locked
RUN ruff check . && ruff format --check . && mypy
# timeout: a rare pytest hang under load was seen twice (pyproject.toml); end the build instead.
RUN if grep -qs "git-lfs.github.com/spec" data/Fuss/Fuss_dwg.pdf; then \
      echo "NOTE: data/ holds Git LFS pointers (no 'git lfs pull'): tests on the sample drawings skip."; \
    fi \
    && timeout 1800 pytest \
    && echo "ruff, ruff format, mypy and pytest passed" > /checks-passed

FROM base AS runtime
ENV UV_COMPILE_BYTECODE=1
COPY src ./src
RUN uv sync --locked --no-dev --no-editable
COPY --from=ui /repo/frontend/dist /opt/review-ui
COPY --from=check /checks-passed /opt/checks-passed
ENV MT_REVIEW_UI=/opt/review-ui

RUN useradd --uid 10001 --create-home app \
    && mkdir -p /session/input /session/builds /session/artifacts \
    && chown -R app:app /session
USER 10001
WORKDIR /session
ENV XDG_CACHE_HOME=/tmp/.cache

EXPOSE 8000
CMD ["python", "-m", "mechatronikers.sandbox"]
