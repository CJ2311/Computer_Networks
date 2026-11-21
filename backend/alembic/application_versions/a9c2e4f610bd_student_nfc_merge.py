"""Merge additive student NFC baseline revision with the verified application chain."""
revision = "a9c2e4f610bd"
down_revision = ("c3a7e9b120df", "e4b7c9d210af")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    raise RuntimeError("Student NFC registration requires a reviewed forward migration")
