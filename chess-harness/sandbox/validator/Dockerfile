# Build from the repository root with a digest-pinned official Python 3.12 image:
# docker build -f sandbox/validator/Dockerfile --build-arg BASE_IMAGE=python:3.12-slim@sha256:... ...
ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS builder
ARG BASE_IMAGE
RUN case "$BASE_IMAGE" in *@sha256:*) ;; *) echo 'BASE_IMAGE must be digest-pinned' >&2; exit 1 ;; esac
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
COPY sandbox/validator/launcher.c /build/launcher.c
RUN gcc -O2 -Wall -Wextra -Werror -D_FORTIFY_SOURCE=2 -fstack-protector-strong -Wl,-z,relro,-z,now -o /build/launcher /build/launcher.c
COPY sandbox/validator/requirements.txt /build/requirements.txt
# Selective archive reading avoids installing build tooling or chess.engine.
RUN python - <<'PY'
import hashlib
from pathlib import Path
import re
import tarfile
import urllib.request

lock = Path('/build/requirements.txt').read_text()
match = re.search(r'^chess==([0-9.]+) --hash=sha256:([0-9a-f]{64})$', lock, re.MULTILINE)
if not match:
    raise SystemExit('invalid chess source lock')
version, expected = match.groups()
target = Path('/build/chess.tar.gz')
with urllib.request.urlopen(f'https://files.pythonhosted.org/packages/source/c/chess/chess-{version}.tar.gz', timeout=60) as response:
    data = response.read(4 * 1024 * 1024 + 1)
if len(data) > 4 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != expected:
    raise SystemExit('chess source archive does not match its lock')
target.write_bytes(data)
destination = Path('/build/api/chess')
destination.mkdir(parents=True)
with tarfile.open(target, 'r:gz') as archive:
    member = archive.getmember(f'chess-{version}/chess/__init__.py')
    if not member.isfile() or member.size > 1024 * 1024:
        raise SystemExit('invalid chess rules module')
    with archive.extractfile(member) as source:
        destination.joinpath('__init__.py').write_bytes(source.read())
PY

FROM ${BASE_IMAGE} AS runtime
ARG RUNTIME_SOURCE_SHA256
RUN test -n "$RUNTIME_SOURCE_SHA256"
LABEL org.llm-chess.validator-runtime="sandbox-v1" \
      org.llm-chess.contract="authored-validator-v1" \
      org.llm-chess.rules-api="rules-board-v1" \
      org.llm-chess.validator-source-sha256="${RUNTIME_SOURCE_SHA256}"
RUN apt-get update && apt-get install -y --no-install-recommends libseccomp2 && rm -rf /var/lib/apt/lists/*
RUN python -m pip uninstall -y pip
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONHASHSEED=0
WORKDIR /opt/validator
COPY --from=builder /build/launcher /opt/validator/launcher
COPY --from=builder /build/api/chess /opt/validator/api/chess
COPY src/chess_harness/validator_contract.py src/chess_harness/validator_rules.py /opt/validator/api/chess_harness/
COPY sandbox/validator/worker.py /opt/validator/worker.py
RUN printf 'llm-chess-validator-worker-v1\n' > /opt/validator/.worker-image-v1 \
    && touch /opt/validator/api/chess_harness/__init__.py \
    && chmod 0444 /opt/validator/.worker-image-v1 \
    && chmod -R go-w /opt/validator \
    && chmod 0555 /opt/validator/launcher
USER 65534:65534
ENTRYPOINT ["/opt/validator/launcher"]
CMD []
