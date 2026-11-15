"""Choose clip or single-image post for each queued advertising product."""
from alembic import op
import sqlalchemy as sa

revision = "20261115_0028"
down_revision = "20261113_0027"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("advertising_products", sa.Column("format", sa.String(16), nullable=False, server_default="clip"))


def downgrade():
    op.drop_column("advertising_products", "format")
