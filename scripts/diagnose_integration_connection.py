"""Compare read-only connections to the dedicated PostgreSQL test database."""
import getpass
import os
import warnings
from contextlib import contextmanager


DATABASE = "inventory_checkout_test"
LOGIN = "inventory_checkout_test_login"
HOST = "127.0.0.1"
PORT = 5432
CLEARED_SETTINGS = {"PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS"}


def failure(category, error):
    underlying = getattr(error, "orig", error)
    if category == "connection open or initialization":
        tb = underlying.__traceback__
        while tb is not None:
            if tb.tb_frame.f_globals.get("__name__") == "psycopg.conninfo":
                category = "psycopg/libpq connection info parsing"
                break
            tb = tb.tb_next
    print(f"FAILURE: {category} ({type(underlying).__name__})")


@contextmanager
def sqlalchemy_connection(create_engine, url):
    engine = create_engine(url, connect_args={"connect_timeout": 5}, hide_parameters=True)
    try:
        with engine.connect() as connection:
            yield connection.exec_driver_sql
    finally:
        engine.dispose()


@contextmanager
def direct_connection(psycopg, password):
    with psycopg.connect(host=HOST, port=PORT, dbname=DATABASE, user=LOGIN, password=password) as connection:
        yield connection.execute


def probe(label, connect):
    print(label)
    results = dict.fromkeys(
        ("CONNECTION", "DATABASE IDENTITY", "ROLE IDENTITY", "SERVER VERSION"), "NOT RUN"
    )
    check = "CONNECTION"
    category = "connection open or initialization"
    try:
        with connect() as execute:
            results[check] = "PASS"
            for check, statement, expected in (
                ("DATABASE IDENTITY", "SELECT current_database()", DATABASE),
                ("ROLE IDENTITY", "SELECT current_user", LOGIN),
                ("SERVER VERSION", "SHOW server_version_num", None),
            ):
                category = check.lower()
                value, = execute(statement).fetchone()
                if expected is None:
                    if int(value) < 180000:
                        raise ValueError()
                elif value != expected:
                    raise ValueError()
                results[check] = "PASS"
            check = "CLEANUP"
            category = "connection cleanup"
    except Exception as error:
        results[check] = "FAIL"
        failure(category, error)

    passed = all(status == "PASS" for status in results.values())
    print(f"{label}: {'PASS' if passed else 'FAIL'}")
    for check, status in results.items():
        print(f"{check}: {status}")
    return passed


def main():
    settings = {name for name in os.environ if name.startswith("PG")}
    for name in CLEARED_SETTINGS:
        os.environ.pop(name, None)
    print("LIBPQ ENVIRONMENT (names only):", ", ".join(sorted(settings)) or "none")
    print("CLEARED (matches integration runner):", ", ".join(sorted(settings & CLEARED_SETTINGS)) or "none")

    try:
        import psycopg
        from sqlalchemy import create_engine
        from sqlalchemy.engine import URL, make_url
    except Exception as error:
        failure("dependency import", error)
        return 1

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Dedicated PostgreSQL test password: ")
        if not password:
            raise ValueError()
    except (Exception, KeyboardInterrupt) as error:
        failure("secure password prompt", error)
        return 1

    direct = probe("DIRECT PSYCOPG", lambda: direct_connection(psycopg, password))
    try:
        url = URL.create(
            "postgresql+psycopg", username=LOGIN, password=password,
            host=HOST, port=PORT, database=DATABASE,
        )
    except Exception as error:
        failure("SQLAlchemy URL construction", error)
        return 1
    sa_result = probe("SQLALCHEMY", lambda: sqlalchemy_connection(create_engine, url))

    # Reproduce the runner's serialization and fixture's make_url() parsing
    # in memory. The parsed URL must match before it can be used to connect.
    try:
        fixture_url = make_url(url.render_as_string(hide_password=False))
        if fixture_url != url:
            raise ValueError()
    except Exception as error:
        print("FIXTURE CONFIGURATION: FAIL")
        failure("fixture URL round trip", error)
        return 1

    fixture = probe("FIXTURE CONFIGURATION", lambda: sqlalchemy_connection(create_engine, fixture_url))
    return 0 if direct and sa_result and fixture else 1


if __name__ == "__main__":
    raise SystemExit(main())
