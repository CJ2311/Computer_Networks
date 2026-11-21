# Computer_Networks

Laboratory inventory backend built with FastAPI and PostgreSQL. Start with the
[backend handover guide](docs/backend/BACKEND_HANDOVER.md) for setup, architecture,
configuration, workflows and safety boundaries. See the [API contract](docs/backend/API.md)
and [deployment guide](docs/backend/DEPLOYMENT.md).

Current application migration head: `f6c1a8d420be`. Final isolated PostgreSQL
verification supplied by the project owner: **312 passed, 0 failed, 0 skipped,
exit code 0**; fresh/populated upgrades and cleanup succeeded. Protected host
databases were untouched. Integration runs require separate authorization and the
reviewed disposable harness; do not run recursive pytest against host databases.
