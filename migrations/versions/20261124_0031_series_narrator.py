"""Bind queued stories to editable series and their narrator voice."""
from alembic import op
import sqlalchemy as sa

revision = '20261124_0031'
down_revision = '20261123_0030'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('content_series', sa.Column('narrator_voice_id', sa.String(128), nullable=True))
    op.add_column('discipline_ideas', sa.Column('content_series_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('fk_discipline_idea_series', 'discipline_ideas', 'content_series', ['content_series_id'], ['id'], ondelete='SET NULL')


def downgrade():
    op.drop_constraint('fk_discipline_idea_series', 'discipline_ideas', type_='foreignkey')
    op.drop_column('discipline_ideas', 'content_series_id')
    op.drop_column('content_series', 'narrator_voice_id')
