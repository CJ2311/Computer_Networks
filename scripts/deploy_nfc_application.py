"""Explicit extension deployment; never initializes the baseline or falls back."""
import argparse
import logging
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from scripts import deploy_nfc_demo as baseline
from backend.alembic.application_safety import apply_application


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database",required=True,choices=[baseline.DATABASE])
    parser.add_argument("--server-address",required=True)
    parser.add_argument("--port",required=True,type=int)
    parser.add_argument("--administrator",required=True)
    parser.add_argument("--schema",required=True)
    args=parser.parse_args()
    logging.disable(logging.CRITICAL)
    engine=None
    try:
        settings=baseline.Settings(**vars(args));settings.validate()
        if not sys.stdin.isatty() or input("Type EXTEND inventory_nfc_demo to permit application migration writes: ")!="EXTEND inventory_nfc_demo":
            raise ValueError("Extension not confirmed")
        password=baseline.secure_password("Deployment administrator password (hidden): ")
        if not password:
            raise ValueError("Administrator credential required")
        engine=baseline.make_engine(settings,settings.administrator,password);password=None
        with engine.connect() as connection:
            apply_application(connection,settings)
        print("Application extension revision and protections verified.")
        return 0
    except (Exception,KeyboardInterrupt):
        print("Application extension stopped; details withheld. Reconcile database state before retrying.",file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__=="__main__":
    raise SystemExit(main())
