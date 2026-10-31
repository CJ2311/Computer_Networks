import os

from dotenv import load_dotenv

load_dotenv()


def is_dev_auth_enabled() -> bool:
    """Enable development authentication only when explicitly configured."""

    app_env = os.getenv("APP_ENV", "production").lower()
    dev_auth = os.getenv("DEV_AUTH_ENABLED", "false").lower()

    return app_env == "development" and dev_auth == "true"