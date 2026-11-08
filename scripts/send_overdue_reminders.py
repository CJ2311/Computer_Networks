"""Explicit scheduled command; never runs on import or application startup."""
import argparse
import json
import logging
import os


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database",required=True)
    parser.add_argument("--limit",type=int,default=100,choices=range(1,101))
    args=parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        from backend.app.services.reminders import SMTPConfig,process_reminders
        config=SMTPConfig.load()
        if config is None:
            print("Overdue email processing is disabled")
            return 0
        if args.database in {"inventory_db","inventory_checkout_test"} or os.environ.get("INVENTORY_DATABASE_NAME")!=args.database:
            raise ValueError("Explicit reminder database selection required")
        from backend.app.database import engine,SessionLocal
        from sqlalchemy import text
        with engine.connect() as c:
            actual=c.execute(text("SELECT current_database(),session_user,current_user,current_schema(),current_schemas(true)")).one()
            if tuple(actual[:4])!=(args.database,"inventory_app","inventory_app","public") or list(actual[4])!=["pg_catalog","public"]:
                raise ValueError("Reminder connection identity mismatch")
        print(json.dumps(process_reminders(SessionLocal,config,args.limit)))
        return 0
    except Exception:
        print("Reminder processing stopped; details withheld. Reconcile uncertain deliveries before retrying.")
        return 1


if __name__=="__main__":
    raise SystemExit(main())
