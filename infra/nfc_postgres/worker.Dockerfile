FROM python:3.12-slim-bookworm
WORKDIR /work
COPY infra/nfc_postgres/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
RUN groupadd --gid 18084 nfc-secrets \
    && groupadd --gid 18085 nfc-worker \
    && useradd --uid 18085 --gid 18085 --groups nfc-secrets --no-create-home --shell /usr/sbin/nologin nfc-worker
COPY backend/app/base.py backend/app/base.py
COPY backend/app/models backend/app/models
COPY backend/app/services backend/app/services
COPY backend/app/schemas backend/app/schemas
COPY backend/alembic backend/alembic
COPY backend/alembic_nfc_demo/env.py backend/alembic_nfc_demo/env.py
COPY alembic_nfc_demo.ini alembic_nfc_demo.ini
COPY alembic_nfc_application.ini alembic_nfc_application.ini
COPY scripts/deploy_nfc_demo.py scripts/deploy_nfc_demo.py
COPY infra/nfc_postgres infra/nfc_postgres
USER nfc-worker
ENTRYPOINT ["python", "-B", "-m", "infra.nfc_postgres.worker"]
