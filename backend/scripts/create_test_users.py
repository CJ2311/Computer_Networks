from sqlalchemy import select

from backend.app.auth.config import is_dev_auth_enabled
from backend.app.database import SessionLocal
from backend.app.models.user import User, UserRole


TEST_USERS = [
    ("DEV-ADMIN", "Test Admin", "admin@example.test", UserRole.ADMIN),
    ("DEV-FACULTY", "Test Faculty", "faculty@example.test", UserRole.FACULTY),
    ("DEV-ASSISTANT", "Test Lab Assistant", "assistant@example.test", UserRole.LAB_ASSISTANT),
    ("DEV-STUDENT", "Test Student", "student@example.test", UserRole.STUDENT),
]


def create_test_users():
    if not is_dev_auth_enabled():
        raise RuntimeError("Development authentication must be enabled.")

    with SessionLocal() as db:
        for college_id, full_name, email, role in TEST_USERS:
            existing_user = db.scalar(
                select(User).where(User.college_id == college_id)
            )

            if existing_user:
                print(f"Already exists: {full_name}")
                continue

            user = User(
                college_id=college_id,
                full_name=full_name,
                email=email,
                role=role.value,
                is_active=True,
            )

            db.add(user)
            print(f"Created: {full_name} ({role.value})")

        db.commit()

    print("Development users are ready.")


if __name__ == "__main__":
    create_test_users()