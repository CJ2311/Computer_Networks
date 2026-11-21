# NFC demonstration migration environment

Ordinary commands continue to use alembic.ini and backend/alembic/env.py.
The NFC deployment entry point is alembic_nfc_demo.ini. It loads its own env.py
and discovers revisions in backend/alembic/versions. The staged demo runner is
pinned to the original seven-revision baseline `d6f2b8a931c0`; that directory
also contains the later additive student NFC revision. The application runner
uses both version directories and targets `f6c1a8d420be`.
No revisions are copied and no production ownership checks are changed.

Programmatic callers must select Config("alembic_nfc_demo.ini") and supply:

- config.attributes["connection"]: an open SQLAlchemy Connection.
- config.attributes["nfc_demo_identity"]: a dictionary with exactly database,
  session_user, current_user, and schema. database must be inventory_nfc_demo.

The environment checks the actual PostgreSQL identity and requires an effective
search path of pg_catalog followed by the expected application schema. It rejects
missing configuration, offline mode, and metadata_audit_isolated_test. It never
constructs an engine, loads .env, or falls back to the ordinary environment.
Application models load credential-free metadata from backend.app.base.

The caller retains the connection and any pre-existing transaction. Use an
outer engine.begin() scope and propagate failures so that scope rolls back.
The environment does not commit or roll back caller-owned transactions. Without
an existing transaction, Alembic manages its own transaction normally. Autocommit
connections are not an appropriate deployment configuration.

The staged deployment runner provides administrator authentication, live preflight,
privilege provisioning and deployment verification outside this environment module.
Fresh/populated upgrades passed the final isolated verification (312 passed,
0 failed, 0 skipped, exit code 0), as supplied by the project owner. Do not use an
unreviewed upgrade-to-head procedure for the ownership split. See the
[deployment guide](../../docs/backend/DEPLOYMENT.md) and
[handover](../../docs/backend/BACKEND_HANDOVER.md).
