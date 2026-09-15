# syntax=docker/dockerfile:1
#
# Slackbot container image (TASK-020, FRD §4.4/§4.5, DSN-SB-006).
#
# ============================================================================
# BUILD CONTEXT MUST BE THE REPOSITORY ROOT, NOT servers/slackbot/:
#
#     docker buildx build --platform linux/arm64 \
#         -f servers/slackbot/Dockerfile -t <tag> .
#
# Same reason as servers/management/Dockerfile (read that file's own
# top-of-file comment first — DSN-SB-006 says to clone its pattern, and this
# header only calls out where this file must differ): this package is a
# member of the *same* uv workspace ([tool.uv.workspace] members =
# ["servers/*"] at the repo root). The lockfile is `uv.lock` at the
# repository root, not under servers/slackbot/ — build with servers/slackbot/
# as context and the first `uv sync` below has no lockfile to read.
#
# 🔴 ONE IMAGE, TWO LAMBDAS (PLAN §1 "진입점 분기 메커니즘", FRD §4.4):
# `handler.py` and `worker.py` each expose a zero-argument ASGI factory
# (`create_app`, *not* management's `create_app_from_env` — do not port that
# name here, it does not exist in this package). TASK-023 provisions two
# Lambdas from this one image and points each at a different factory via
# `ImageConfig.Command`, which replaces this file's `CMD` outright. The
# default `CMD` below runs the handler factory; TASK-023's worker Lambda
# overrides it with the worker factory (see the CMD block's own comment for
# why the override is a clean argv swap, not a shell-string edit).
#
# 🔴 THIS WORKSPACE HAS TWO MEMBERS NOW — `uv sync` NEEDS `--package` (found
# while writing this file, not assumed):
# `servers/management/Dockerfile` runs a plain `uv sync ... --no-dev
# --no-editable` with no `--package` flag. That was correct when this
# workspace had one member. It silently stopped being correct the moment
# `servers/slackbot` was added: this repository's root `pyproject.toml` has
# no `[project]` table (a "virtual" workspace root), so a `uv sync` with no
# `--package`/`--all-packages` selector resolves and installs *every*
# workspace member, unscoped. Verified locally (`uv sync --frozen --no-dev
# --no-editable --dry-run` at the repo root, this workspace, right now):
# it lists both `devoks-mcp-management` and `devoks-slackbot` as targets.
# Left uncorrected, this Dockerfile would ship `anthropic`/`boto3`/`httpx2`
# *and* management's `mcp`/`cryptography`/`pyjwt` bundled together in one
# runtime image — exactly the cross-contamination FRD §4.4 ("코드 의존
# 없음") rules out. `--package devoks-slackbot` on both `uv sync` calls below
# is what keeps this image scoped to this package alone; verified in an
# isolated clean-room copy of this repo (no pre-existing `.venv`, no
# workspace member directories present for the frozen layer) that the
# resulting `.venv` contains exactly this package's 25 third-party
# dependencies (starlette, uvicorn, boto3, httpx2, anthropic + transitives)
# and neither `devoks-mcp-management` nor any dev dependency.
# NOTE FOR WHOEVER TOUCHES `servers/management/Dockerfile` NEXT: the same gap
# now exists there (this task's scope is this file only, so it is reported
# here rather than fixed there — see this task's own delegation report).
#
# TARGET ARCHITECTURE (CTR-010 = linux/arm64, Fargate Graviton's Lambda
# equivalent) — same reasoning as servers/management/Dockerfile, re-verified
# for this package's own dependency set rather than assumed transferable:
# nothing below pins a platform, so the target is chosen at *build* time via
# `--platform` (TASK-021's CI build targets arm64; a bare local `docker build`
# stays usable on either architecture). `python:3.14-slim-trixie` and
# `ghcr.io/astral-sh/uv:0.11.4` both publish linux/amd64 *and* linux/arm64
# manifests (already verified for servers/management/Dockerfile; same
# registry entries, so the same fact applies here unchanged). This package's
# own dependency closure has exactly one native-extension wheel — `pydantic-core`
# (pulled in transitively via `anthropic`) — and `uv.lock` records prebuilt
# manylinux wheels for both `aarch64` and `x86_64` for it (checked against the
# lockfile's own recorded wheel URLs, same method servers/management/Dockerfile
# used for its own native deps). No compiler-dependent step exists here either.
#
# WHY python:3.14-slim-trixie (not a syntax-forced floor here, unlike
# management's PEP 758 case — checked, this package's own source uses no
# 3.14-only syntax): `servers/slackbot/pyproject.toml` itself declares
# `requires-python = ">=3.14"`, and the one shared `uv.lock` this workspace
# uses was resolved against that floor for every member — a lower base image
# would have no compatible locked wheels to install at all. Floating the
# patch version (not `python:3.14.2-slim-trixie`) for the same trade-off
# servers/management/Dockerfile documents: security patches on rebuild
# without a Dockerfile edit, at the cost of not being pinned to the exact
# locally-verified patch release.
# ============================================================================


# ---------------------------------------------------------------------------
# Stage 1/2 — builder: resolves the workspace lockfile, scoped to this
# package only (see the `--package` note above), and installs it into a
# virtual environment. Only the finished `.venv/` survives into the runtime
# image below — no uv binary, no dev dependencies (pytest/pytest-asyncio/
# ruff/pyright/moto/boto3-stubs), no source tree, no package cache, and no
# trace of `servers/management`'s own dependencies.
# ---------------------------------------------------------------------------
FROM python:3.14-slim-trixie AS builder

# Pinned to the exact uv version this workspace was authored/verified against
# (locally installed `uv 0.11.4`), not `:latest` — same reasoning as
# servers/management/Dockerfile (a future CI run should not silently pick up
# a newer resolver/sync behavior). Existence + linux/amd64+arm64 support
# already verified for this exact tag while writing that file.
COPY --from=ghcr.io/astral-sh/uv:0.11.4 /uv /uvx /bin/

# Same three settings as servers/management/Dockerfile, same reasoning:
# UV_PYTHON_DOWNLOADS=0 — use the base image's own 3.14 interpreter instead of
# a second uv-managed one. UV_LINK_MODE=copy — avoids a hardlink warning
# against the BuildKit cache-mount filesystem boundary. UV_COMPILE_BYTECODE=1
# — precompile .pyc during sync rather than at first cold-start import
# (EDGE-SB-007's 3-second budget makes cold-start import cost matter here
# more than on a long-lived dev machine).
ENV UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

# --- Layer 1: third-party dependencies only, scoped to this package ---
# `--package devoks-slackbot` is the fix for the two-member-workspace gap
# documented at the top of this file — without it this layer would resolve
# and cache management's third-party dependencies too. `--no-install-workspace`
# additionally excludes every *workspace member itself* (this project
# included) from this sync; only `uv.lock` + the root `pyproject.toml` are
# needed to resolve which third-party wheels this package's dependency
# closure needs (`--package` narrows the closure, `--no-install-workspace`
# then keeps the members themselves out) — verified locally that this
# combination resolves correctly even with zero workspace-member directories
# on disk (a clean-room copy holding only `uv.lock` + root `pyproject.toml`,
# matching exactly what the two `--mount=type=bind` lines below expose).
# `--frozen` (not `--locked`) is required for the same reason
# servers/management/Dockerfile uses it here: workspace-member pyproject.toml
# files are not yet present in this layer, so uv cannot fully validate the
# lock against them yet. `--no-dev` drops pytest/pytest-asyncio/ruff/pyright/
# moto/boto3-stubs — this is a production image, not a dev environment.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-workspace --package devoks-slackbot

# --- Layer 2: the project itself, non-editable ---
# Only now is the full (dockerignore-filtered) context copied in, so only
# this layer — not Layer 1 above — is invalidated by a source change under
# servers/slackbot/src/.
COPY . /app

# `--locked` can now check `uv.lock` against every member's real
# pyproject.toml (present after the COPY above) and fail the build if it is
# stale, instead of trusting it blindly. `--no-editable` installs this
# package as a normal, copied package inside `.venv` rather than a symlink
# back to the source tree, so the runtime stage below can copy `.venv/` alone
# with no source tree ever present in the runtime image. `--package
# devoks-slackbot` again — without it this second sync would also install
# `devoks-mcp-management` into the same `.venv` (verified locally: the same
# clean-room copy, run without `--package`, lists `devoks-mcp-management` as
# an install target). Verified locally that the resulting install is
# non-editable (`devoks_slackbot.__file__` resolves inside `.venv/lib/
# python3.14/site-packages/devoks_slackbot/`, not `src/`) and that the
# `py.typed` marker (PEP 561) is carried into site-packages by hatchling's
# default package-data rules, same as management's own package.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package devoks-slackbot


# ---------------------------------------------------------------------------
# Stage 2/2 — runtime: no uv, no compiler, no dev dependencies, no source
# tree outside what's already packaged into `.venv/`, no `servers/management`
# code or dependencies (see the `--package` note above).
# ---------------------------------------------------------------------------
FROM python:3.14-slim-trixie AS runtime

# PYTHONUNBUFFERED: TASK-008's observability records are JSON Lines on
# stdout, one per processed event; this is kept as a second, independent
# guarantee (beyond observability.py's own explicit flush) against a record
# lost to an unflushed write when the container is replaced.
# PYTHONDONTWRITEBYTECODE: the runtime `app` user below has no write access
# to /app (see the ownership note further down) — an attempted .pyc write
# would fail silently on every import anyway; this turns the attempt off
# instead of eating a wasted syscall+error per import.
#
# No `MCP_PORT`-equivalent env var here (deliberate difference from
# servers/management/Dockerfile): this package's own config.py has no port
# setting to mirror — the ASGI port is purely a deployment-wiring concern
# between this file's CMD/HEALTHCHECK and the AWS_LWA_PORT value below, not
# something either Lambda's own environment ever needs to read or override.
# Both entry points bind the same literal port (8080) instead, kept as one
# hardcoded number rather than an extra indirection with nothing on the
# application side to keep it in sync with.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ---------------------------------------------------------------------------
# Debian security updates at build time — same conclusion
# servers/management/Dockerfile's TASK-050 investigation reached, transferred
# rather than re-derived: identical base image (`python:3.14-slim-trixie`),
# so the same "0 upgraded, 0 newly installed" measured result applies today.
# Kept anyway for the same reason: this closes the window between an
# upstream trixie-security upload and the next base-image rebuild. See that
# file's own comment block for the full per-CVE investigation trail (perl
# Essential-package residual risk included) — not repeated here since none of
# it is specific to this package.
# ---------------------------------------------------------------------------
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# AC-SB-008-2: non-root, non-login system account. Same fixed uid/gid as
# servers/management/Dockerfile (10001) — reproducible across rebuilds, and
# referenceable directly from infra/07-slackbot-lambda.sh without first
# inspecting the built image.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

# Only the synced virtual environment crosses the stage boundary. No secret
# (Slack signing secret/bot token, Anthropic API key, per-person MCP tokens)
# is ever baked into an ENV/ARG or copied in from a `.env` file here — every
# one of them is read from the process environment at startup by config.py's
# Fail-Fast loaders, and `.env`/`.venv` are both excluded by `.dockerignore`.
#
# Ownership deliberately left root:root (COPY's default) — verified locally,
# same as servers/management/Dockerfile: `uv sync` produces `.venv` with
# directories `755` and files `644`/`755` (world-readable/executable), so the
# `app` user below needs no ownership to read and execute everything it
# needs. This package has no on-disk write path at runtime either
# (idempotency state goes to DynamoDB, observability records to stdout, no
# plugin-install step) — skipping a recursive `chown` avoids doubling this
# stage's on-disk size for no behavioral benefit.
COPY --from=builder /app/.venv /app/.venv

# ---------------------------------------------------------------------------
# CTR-011 / DSN-SB-006 — AWS Lambda Web Adapter (LWA), reused unchanged from
# servers/management/Dockerfile's already-verified pattern (same tag, same
# `/opt/extensions` placement, same "inert outside a Lambda runtime" property
# — see that file's own comment block for the full justification, not
# repeated here since none of it is specific to this package).
# ---------------------------------------------------------------------------
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:1.1.0 \
    /lambda-adapter /opt/extensions/lambda-adapter

# AWS_LWA_PORT: both entry points' CMD (below) bind 8080 directly — literal
# match, not env-var-mirrored (see the ENV block above for why there is no
# equivalent to management's MCP_PORT here). CTR-011's own wording ("
# AWS_LWA_PORT = MCP_PORT") is written for management's specific settings
# key; the form of that same requirement that actually applies to this
# package is just "AWS_LWA_PORT matches whatever port the running CMD binds
# to" — satisfied here by both being the same literal 8080.
#
# AWS_LWA_READINESS_CHECK_PATH=/healthz: matches `HEALTHZ_PATH` in *both*
# handler.py and worker.py (identical string in both modules, confirmed by
# reading both files) — LWA holds the first invocation until this returns
# 2xx on whichever entry point TASK-023's `ImageConfig.Command` selects for
# that Lambda, so one value here correctly covers both.
#
# AWS_LWA_INVOKE_MODE=BUFFERED: correct for both entry points — handler
# returns an immediate ack (never more than a small JSON/plaintext body,
# CTR-SB-002's 3-second budget rules out anything that would need to stream),
# and worker's own response is a short fixed "ok" body (worker.py). Neither
# needs RESPONSE_STREAM, so BUFFERED avoids a split-brain configuration with
# the Function URL / API Gateway route (TASK-024) that would only surface
# under load.
#
# 🔴 AWS_LWA_PASS_THROUGH_PATH — this is the field that does not exist in
# servers/management/Dockerfile at all, because management has no
# asynchronous-invoke Lambda. The worker Lambda here is invoked with
# `InvocationType="Event"` (handler.py's async hand-off) rather than through
# an HTTP-fronted route; per the AWS Lambda Web Adapter's own README
# ("the path for receiving event payloads from non-http triggers", default
# `"/events"`), LWA POSTs the *raw* async-invoke payload to this path, which
# is exactly the route worker.py's own `EVENTS_PATH` constant serves.
# MUST equal worker.py's `EVENTS_PATH` value exactly, hardcoded rather than
# derived, because a mismatch here fails *silently before deployment* — no
# test in this repository exercises the Dockerfile/Lambda wiring itself, so
# a typo here would only surface as "worker never receives any event",
# discovered in production, not in CI (`uv run pytest`, ruff, pyright all
# stay green either way).
#
# Set here (image ENV), not left to a per-Lambda environment variable only —
# deliberate choice, not the default: it is harmless on the handler Lambda
# (handler.py's ASGI app has exactly two routes, `HEALTHZ_PATH` and
# `SLACK_EVENTS_PATH` — no route bound to `/events` — so LWA would 404
# against handler's app before ever reaching application code, not silently
# misbehave; handler is also never invoked with `InvocationType="Event"` by
# this design, so the value is simply never exercised there). Baking it into
# the image means TASK-023 does not have to additionally repeat this same
# key on both Lambdas' own environment variable definitions to get correct
# behavior on the worker Lambda — which matters concretely against
# TASK-023's own 4 KB total environment-variable budget check (EDGE-021):
# one shared image-level value costs that budget nothing per Lambda, versus
# ~35 bytes duplicated on every Lambda's own environment if set only there.
ENV AWS_LWA_PORT=8080 \
    AWS_LWA_READINESS_CHECK_PATH=/healthz \
    AWS_LWA_INVOKE_MODE=BUFFERED \
    AWS_LWA_PASS_THROUGH_PATH=/events

# Numeric form (not `USER app`) — same reasoning as
# servers/management/Dockerfile: resolvable by host-side tooling that
# inspects the image without running it (hadolint DL3066).
USER 10001:10001

# Documentation only, same as servers/management/Dockerfile — does not
# itself publish the port; matches the literal 8080 both entry points bind.
EXPOSE 8080

# Local `docker run`/`docker compose` iteration only — same as
# servers/management/Dockerfile, Lambda never consults this (LWA's own
# AWS_LWA_READINESS_CHECK_PATH probe above is what matters there). Uses the
# venv's own Python (no curl in the slim base image) so the process being
# checked and the process doing the checking run under the same interpreter.
# Checks whichever entry point this container's CMD (or an `ImageConfig.
# Command` override) is currently running — correct for either, since both
# expose `HEALTHZ_PATH="/healthz"` on the same port.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "import sys, urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2); sys.exit(0)"]

# Default CMD runs the slack-handler Lambda's entry point
# (`devoks_slackbot.handler:create_app`, a zero-argument factory — settings
# load from `os.environ` via `load_handler_settings`, Fail-Fast, when none is
# passed explicitly; see handler.py's own `create_app` docstring). TASK-023
# provisions the slack-worker Lambda by overriding `ImageConfig.Command` with
# the same argv shape below, swapping only `devoks_slackbot.handler:create_app`
# for `devoks_slackbot.worker:create_app` — `ImageConfig.Command` replaces
# this CMD outright, so the override only has to change the one module path.
#
# Plain exec-form JSON array, not servers/management/Dockerfile's `["sh",
# "-c", "exec ... $MCP_PORT"]` form — deliberately simpler, not a downgrade:
# that shell wrapper exists there solely to let `$MCP_PORT` be expanded at
# container-start time; this package has no equivalent env-driven port (see
# the ENV block above), so there is nothing to expand and no shell is needed.
# A bare JSON-array CMD already runs uvicorn as PID 1 directly (Docker execs
# the array's first element without an extra shell layer), so SIGTERM
# forwarding on `docker stop`/task replacement works the same way management
# achieves it via `exec`, with one less moving part — and a plain argv array
# is also the easiest possible shape for TASK-023 to override via
# `ImageConfig.Command` (swap one string, no shell-quoting to reproduce).
CMD ["/app/.venv/bin/uvicorn", "devoks_slackbot.handler:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
