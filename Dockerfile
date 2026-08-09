# ---- builder: only this stage needs curl, it never reaches the final image ----
FROM python:3.14.7-alpine3.24 AS builder

# sha256 checksums pinned from upstream's own sops-v3.13.3.checksums.txt, verified out-of-band since this binary handles every stack's secrets
ARG SOPS_VERSION=3.13.3
ARG SOPS_SHA256_AMD64=e5bec3346a873ae91d871550f3e698c1aad962aff462a080e40f25fde17fef6b
ARG SOPS_SHA256_ARM64=53b0abacd38ef1b12a66d6c100956691b9cefce018d91f81e73ddf7438b94d77

RUN apk add --no-cache curl ca-certificates \
    && ARCH=$(case "$(apk --print-arch)" in x86_64) echo amd64;; aarch64) echo arm64;; *) apk --print-arch;; esac) \
    && SHA256=$(if [ "$ARCH" = "amd64" ]; then echo "$SOPS_SHA256_AMD64"; else echo "$SOPS_SHA256_ARM64"; fi) \
    && curl -fsSL -o /usr/local/bin/sops \
        "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.${ARCH}" \
    && echo "${SHA256}  /usr/local/bin/sops" | sha256sum -c - \
    && chmod +x /usr/local/bin/sops

# ---- final: only what's needed to run, no curl, no build tooling ----
FROM python:3.14.7-alpine3.24

# Runs as root deliberately: docker.sock access is already root-equivalent regardless of UID, so a non-root user adds no real isolation here
# apk upgrade pulls in security backports already published for this alpine branch (e.g. libuuid/util-linux) that predate this base image build
# bind-mounted repos under /repository are owned by the host, which git's dubious-ownership check
# otherwise rejects; trust that whole prefix so any mounted repo folder works without extra config
RUN apk upgrade --no-cache \
    && apk add --no-cache git docker-cli docker-cli-compose ca-certificates tzdata \
    && git config --system --add safe.directory '/repository/*'

COPY --from=builder /usr/local/bin/sops /usr/local/bin/sops

WORKDIR /app
COPY docker_operator ./docker_operator
COPY pyproject.toml ./
# pip/setuptools/msgpack ship pinned in the base image and lag its own security fixes
RUN pip install --no-cache-dir --upgrade pip setuptools msgpack \
    && pip install --no-cache-dir . \
    && rm -rf /app/pyproject.toml /root/.cache

ENV DATA_DIR=/data DEPLOY_DIR=/deploy LISTEN_HOST=0.0.0.0 LISTEN_PORT=8080 TZ=UTC

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-m", "docker_operator", "--healthcheck"]

ENTRYPOINT ["python", "-m", "docker_operator"]
