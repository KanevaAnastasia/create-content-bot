"""Keep editorial evidence separate from approved voice and generation direction."""
from alembic import op
import sqlalchemy as sa

revision = '20261123_0030'
down_revision = '20261123_0029'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('discipline_ideas', sa.Column('review_plan', sa.Text(), nullable=False, server_default=''))


def downgrade():
    op.drop_column('discipline_ideas', 'review_plan')
