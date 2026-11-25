FROM python:3.13-slim

ARG BASE_COMMIT=""
COPY src/episteme /opt/episteme/src/episteme
COPY examples/evolution/episteme_evolution_worker.py /opt/episteme/worker/
RUN --network=none python -c "import os, sysconfig; from pathlib import Path; Path(sysconfig.get_path('purelib'), 'episteme-evolution.pth').write_text('/opt/episteme/src\n/opt/episteme/worker\n'); Path('/opt/episteme/base-commit').write_text(os.environ['BASE_COMMIT'])"
