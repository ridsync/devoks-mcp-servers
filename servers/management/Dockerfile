# syntax=docker/dockerfile:1
#
# Management MCP server — production image (TASK-030, REQ-007).
#
# ============================================================================
# BUILD CONTEXT MUST BE THE REPOSITORY ROOT, NOT servers/management/:
#
#     docker buildx build --platform linux/arm64 \
#         -f servers/management/Dockerfile -t <tag> .
#
# This server lives in a uv *workspace* (DSN-008, FRD §4.4): the single
# lockfile `uv.lock` and the workspace root `pyproject.toml`
# ([tool.uv.workspace] members = ["servers/*"]) both live at the repository
# root, not under servers/management/. Every `uv sync` below resolves
# against that root lockfile — if the build context is servers/management/
# instead, `uv.lock` simply is not there and the very first `uv sync` fails
# immediately. TASK-031 (CI) must invoke `docker build`/`buildx build` with
# `.` (or an equivalent absolute repo-root path) as the context and
# `-f servers/management/Dockerfile` to point at this file.
#
# TARGET ARCHITECTURE (CTR-010 = linux/arm64, Fargate Graviton):
# This Dockerfile is architecture-neutral on purpose — nothing below pins a
# platform. The base images referenced here (`python:3.14-slim-trixie`,
# `ghcr.io/astral-sh/uv:0.11.4`) both publish linux/amd64 *and* linux/arm64
# manifests (verified against the Docker Hub / ghcr.io registry APIs before
# writing this file — not assumed), and every native-extension dependency in
# `uv.lock` (cryptography, pydantic-core, rpds-py, cffi) ships prebuilt
# manylinux wheels for both `aarch64` and `x86_64` (verified against the
# lockfile's own recorded wheel URLs) — so there is no compiler-dependent
# step whose behavior would differ per architecture. TASK-031 selects the
# target architecture at *build* time via `--platform`, as in the command
# above. Hardcoding `--platform=linux/arm64` into a `FROM` line here was
# considered and rejected: it would force every `docker build .` — including
# a plain local build on a non-Apple-Silicon dev machine — onto emulated
# arm64 regardless of intent. Leaving it to the build invocation keeps this
# Dockerfile usable for quick local builds on either architecture while
# TASK-031's CI build targets arm64 explicitly.
#
# WHY python:3.14 SPECIFICALLY (not just SDK's own >=3.10 floor):
# `adapters/knowledge/github/client.py` uses PEP 758 syntax (parenthesis-free
# multi-exception `except`). On Python 3.13 this is a SyntaxError at import
# time — verified locally against the installed 3.13 interpreter — so a
# 3.13 base image would fail to even start the container, not just behave
# differently. `python:3.14-slim-trixie` pins the minor version (and the
# Debian release, for a stable set of system packages) but floats the patch
# version deliberately, so this image keeps receiving upstream CPython
# security patches on rebuild without a Dockerfile edit; exact patch pinning
# (`python:3.14.2-slim-trixie`, matching the locally verified dev interpreter
# exactly) or full digest pinning are both available if stronger build
# reproducibility is wanted later — traded off here in favor of not needing
# to remember to bump this file for every point release.
# ============================================================================


# ---------------------------------------------------------------------------
# Stage 1/2 — builder: resolves the workspace lockfile and installs the
# server into a virtual environment. Nothing from this stage other than the
# finished `.venv/` survives into the runtime image below — no uv binary, no
# dev dependencies (pytest/ruff/pyright), no source tree, no package cache.
# ---------------------------------------------------------------------------
FROM python:3.14-slim-trixie AS builder

# Pin the uv version to the one this workspace was authored and verified
# against (locally installed `uv 0.11.4`) rather than `:latest`, so a CI
# build months from now can't silently pick up a newer uv with different
# resolver/sync behavior. Existence + linux/amd64+arm64 support verified via
# the ghcr.io registry API before writing this line.
COPY --from=ghcr.io/astral-sh/uv:0.11.4 /uv /uvx /bin/

# UV_PYTHON_DOWNLOADS=0: use the base image's own Python interpreter for
# both stages instead of letting uv download/manage a second one — the image
# already ships exactly the version family this project requires (see the
# "WHY python:3.14" note above), so a second interpreter would be redundant
# network I/O and attack surface for no benefit.
# UV_LINK_MODE=copy: avoids a spurious hardlink-across-filesystem-boundary
# warning uv would otherwise emit when the package cache below is a
# BuildKit cache mount (a separate mounted filesystem from /app).
# UV_COMPILE_BYTECODE=1: precompile .pyc during `uv sync` rather than at
# first import — trades a few seconds of extra build time and a few MB of
# image size for faster cold starts, which matters more here than on a
# long-lived dev machine (ECS/Fargate task replacement is exactly a repeated
# cold start).
ENV UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

# --- Layer 1: third-party dependencies only (cacheable across source edits) ---
# `--no-install-workspace` excludes every workspace member (this project
# included) from this sync — only `uv.lock` + the *root* `pyproject.toml`
# are needed to resolve third-party dependencies (verified locally: this
# command succeeds with none of servers/management/'s own files present).
# That means this layer's cache is invalidated only when a *dependency*
# changes, never when application source under servers/management/src/
# changes — the expensive part (resolving + downloading ~30 packages) is
# skipped on every source-only edit, which is what keeps CI (TASK-031) fast.
# `--frozen` (not `--locked`) is required here specifically because
# workspace-member pyproject.toml files are not yet present, so uv cannot
# fully validate the lock against them yet — see uv's own docs on
# multi-stage workspace builds:
# https://docs.astral.sh/uv/guides/integration/docker/#intermediate-layers-in-workspaces
# `--no-dev` drops pytest/ruff/pyright — this is a production image, not a
# dev environment; test tooling has no reason to ship in it.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-workspace

# --- Layer 2: the project itself, non-editable ---
# Only now is the full (dockerignore-filtered) context copied in, so only
# *this* layer — not Layer 1 above — is invalidated by a source change.
# This also brings in the root `uv.lock`/`pyproject.toml` for real this time
# (Layer 1's copies above were ephemeral --mount=type=bind, not persisted),
# which the second `uv sync` below needs.
COPY . /app

# Now that every workspace member's pyproject.toml is present, `--locked`
# can do a check `--frozen` above could not: fail the build outright if
# `uv.lock` is stale relative to any member's pyproject.toml, instead of
# silently trusting it. `--no-editable` installs this project as a normal,
# copied package inside `.venv` rather than a symlink back to the source
# tree — required so that the runtime stage below can copy `.venv/` alone
# and run correctly without the source tree ever being present in the
# runtime image. Verified locally: after an equivalent `--no-editable`
# sync, `devoks_mcp_management.__file__` resolves inside
# `.venv/lib/python3.14/site-packages/` (not `src/`), and the `py.typed`
# marker (PEP 561) is carried into site-packages by hatchling's default
# package-data rules — confirmed by inspecting the built wheel's file list.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable


# ---------------------------------------------------------------------------
# Stage 2/2 — runtime: no uv, no compiler, no dev dependencies, no source
# tree outside what's already packaged into `.venv/`. Only the base image's
# own Python interpreter and the environment `builder` synced above.
# ---------------------------------------------------------------------------
FROM python:3.14-slim-trixie AS runtime

# AC-004-1: audit records are JSON Lines written to stdout, one per tool
# call; audit/logger.py already flushes each line itself, but
# PYTHONUNBUFFERED is kept as a second, independent guarantee that stdout is
# never buffered at the interpreter/libc level — the failure mode it guards
# against (a record lost because the container is SIGKILLed after an
# unflushed write) is exactly the kind of thing that should not depend on a
# single layer of defense.
# PYTHONDONTWRITEBYTECODE: the runtime `app` user below has no write access
# to /app (see the USER/ownership note further down), so an attempted .pyc
# write would just fail silently on every import anyway — turn the attempt
# off instead of eating a wasted syscall+error per module import.
# MCP_PORT: matches this project's own default (config.py's
# `_DEFAULT_PORT = 8000`, CTR-006) — set here so `$MCP_PORT` in CMD below
# always expands to a valid value even if an operator forgets to pass one
# explicitly (an ECS task definition can still override it via its own
# environment entries).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_PORT=8000

# AC-007-1: run as a non-root, non-login system account. A fixed numeric
# UID/GID (rather than letting `useradd --system` pick one from Debian's
# floating system-UID range) keeps the identity reproducible across rebuilds
# and lets it be referenced explicitly from an ECS task definition's `user`
# field or a `readOnlyRootFilesystem`-style policy later (Stage 2) without
# having to first inspect the built image to find out what got assigned.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

# Only the synced virtual environment crosses the stage boundary — no uv
# binary, no /root/.cache/uv package cache, no compiler, no source tree
# outside what `--no-editable` already packaged, no dev dependencies (none
# of that ever existed anywhere but `builder`). AC-007-4: nothing above ever
# wrote a secret (GitHub App private key, Bearer tokens) into any layer —
# every credential this server needs is read from the process environment
# at startup (config.py, Fail-Fast) and is never baked into an `ENV`/`ARG`
# or copied in from a `.env` file (also excluded by .dockerignore).
#
# Ownership is deliberately left as root:root — COPY's default, matching
# what `builder` produced under its default umask. Verified locally that
# `uv sync` creates `.venv` world-readable/executable (directories `755`,
# regular files `644`, executables `755`): the `app` user below can read and
# execute everything it needs without owning any of it. `.venv` is never
# written to at runtime — this server has no plugin-install step, no
# on-disk cache (audit logs go to stdout, not disk; PYTHONDONTWRITEBYTECODE
# above rules out incidental .pyc writes) — so there is nothing for `app` to
# need write access to, and skipping a recursive `chown` avoids an extra
# full-tree-rewrite layer that would otherwise roughly double this stage's
# on-disk image size (COW layer semantics: chown after COPY rewrites every
# file into a new layer) for no behavioral benefit.
COPY --from=builder /app/.venv /app/.venv

# ---------------------------------------------------------------------------
# CTR-011 — AWS Lambda Web Adapter (LWA), the deployment target's bridge.
#
# The deployment target is Lambda + Function URL (FRD §10 Stage 2). LWA is a
# Lambda *extension*: Lambda's init process starts anything under
# /opt/extensions, and LWA then polls the Lambda Runtime API and translates
# each invocation into a plain HTTP request against the web server this
# image already runs (the CMD below, unchanged).
#
# WHY THIS DOES NOT REQUIRE AN AWS BASE IMAGE:
# LWA ships the Lambda Runtime Interface Client itself, so a non-AWS base
# image is supported — `python:3.14-slim-trixie` stays exactly as it is
# (verified against aws/aws-lambda-web-adapter's own documentation). Without
# that property this stage would have had to move to
# public.ecr.aws/lambda/python, which does not publish a 3.14 tag and would
# have collided with the PEP 758 floor documented at the top of this file.
#
# WHY IT IS SAFE FOR NON-LAMBDA RUNTIMES:
# An extension is only started by the Lambda init process. `docker run` and
# any container orchestrator never look at /opt/extensions, so this binary
# is an inert ~10 MB file there — the same image keeps working unchanged for
# local iteration and for TASK-031's CI smoke test (which is what verifies
# that claim on every push).
#
# Tag 1.1.0 is pinned (not `:latest`) for the same reason uv 0.11.4 is
# pinned above. Existence and linux/amd64+arm64 manifest support verified
# against the ECR Public registry API before writing this line — CTR-010
# targets linux/arm64, so an amd64-only adapter would have silently produced
# an image whose extension cannot execute.
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:1.1.0 \
    /lambda-adapter /opt/extensions/lambda-adapter

# AWS_LWA_PORT: LWA defaults to 8080; this server listens on $MCP_PORT.
# The two MUST move together — overriding MCP_PORT in a Lambda function's
# environment without overriding AWS_LWA_PORT makes LWA poll a port nothing
# is bound to, and every invocation times out with no application log line
# to explain it. Both are 8000 here (config.py's `_DEFAULT_PORT`, CTR-006).
# AWS_LWA_READINESS_CHECK_PATH: CTR-001's health endpoint. LWA holds the
# first invocation until this returns 2xx, which is what makes the cold
# start (EDGE-016) wait for the MCP session manager's lifespan — including
# `_make_github_lifespan` — instead of racing it.
# AWS_LWA_INVOKE_MODE=BUFFERED: correct because MCP_JSON_RESPONSE defaults
# to true (CTR-011), so every response is a single complete JSON body.
# RESPONSE_STREAM would additionally require the Function URL itself to be
# created with InvokeMode=RESPONSE_STREAM; keeping both sides buffered
# avoids a split-brain configuration that only shows up under load.
ENV AWS_LWA_PORT=8000 \
    AWS_LWA_READINESS_CHECK_PATH=/healthz \
    AWS_LWA_INVOKE_MODE=BUFFERED

# Numeric form (not `USER app`) so the identity is resolvable by tooling
# that inspects the image without running it (host-side security scanners,
# `docker inspect`, an ECS task definition's own `user` field) — hadolint
# DL3066 flags the name form for exactly this reason. Still the same `app`
# account created above (uid=gid=10001); `/etc/passwd` inside the image
# resolves it back to that name for anything that *does* run in-container.
USER 10001:10001

# Documentation only — matches MCP_PORT's default above; does not itself
# publish the port (that's `docker run -p` / the ECS task definition's own
# port mapping).
EXPOSE 8000

# Useful for local `docker run`/`docker compose` iteration; TASK-031's own
# CI smoke test curls /healthz directly rather than relying on this. Not
# meaningful on Lambda (Stage 2's deployment target) — Lambda never consults
# a Dockerfile HEALTHCHECK; readiness there is LWA's own
# AWS_LWA_READINESS_CHECK_PATH probe against this same /healthz endpoint,
# configured above. Kept anyway because the cost is one instruction and the
# local-iteration value is real. Uses the venv's own Python (no curl in the slim base
# image, and installing one via apt would mean an extra layer purely for
# this) so the process being checked and the process doing the checking run
# under the exact same interpreter/environment.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "import os, sys, urllib.request; port = os.environ.get('MCP_PORT', '8000'); urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=2); sys.exit(0)"]

# Entry point per TASK-009/TASK-023's handover notes, verified again here:
# `create_app_from_env` is a *factory* (deliberately no module-level `app`
# attribute — see app.py's own docstring), hence `--factory`.
#
# Exec-form CMD (JSON array) is required so uvicorn receives SIGTERM
# directly as PID 1 — a container runtime sends SIGTERM then waits out a
# grace period before SIGKILL, and a shell-form CMD would leave a shell as
# PID 1 that swallows that signal instead of forwarding it, turning every
# replacement into a hard kill instead of a graceful shutdown. On Lambda the
# same exec form matters for a different reason: the process LWA proxies to
# must be the one that owns the port, not a child of a lingering shell. But
# `--port $MCP_PORT` needs *shell* expansion — a JSON-array CMD's arguments
# are passed to execve() verbatim, with no variable substitution performed.
# `sh -c '... exec ...'` gets both: Docker execs `sh` directly (JSON array,
# so no additional Docker-level shell wrapping is layered on top), `sh`
# expands `$MCP_PORT`, and the `exec` builtin then replaces `sh`'s own
# process image with uvicorn via execve() — uvicorn ends up as PID 1
# itself, not a child of a lingering shell.
CMD ["sh", "-c", "exec /app/.venv/bin/uvicorn devoks_mcp_management.app:create_app_from_env --factory --host 0.0.0.0 --port \"$MCP_PORT\""]
