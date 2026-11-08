"""No connection or provisioning until explicitly opted-in test execution."""
import pytest
from infra.nfc_postgres.harness import Harness, IsolationError, require_opt_in


def pytest_collection_modifyitems(config, items):
    try:
        require_opt_in()
    except IsolationError:
        for item in items:
            item.add_marker(pytest.mark.skip(reason="Disposable-container opt-in required"))


@pytest.fixture(scope="session")
def instance():
    require_opt_in()
    harness = Harness()
    harness.bootstrap()
    try:
        yield harness
    finally:
        harness.dispose()


@pytest.fixture
def database(instance):
    instance.reset_database()
    try:
        yield instance
    finally:
        instance.dispose()
