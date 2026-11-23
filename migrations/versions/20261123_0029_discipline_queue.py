"""Persistent manual Discipline idea queue, separate from paid night generation."""
from alembic import op
import sqlalchemy as sa

revision = "20261123_0029"
down_revision = "20261115_0028"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('discipline_queue_schedule', sa.Column('id', sa.Integer(), primary_key=True),
                    sa.Column('enabled', sa.Boolean(), nullable=False),
                    sa.Column('next_run_at', sa.DateTime(timezone=True), nullable=False))
    op.create_table('discipline_ideas',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('slug', sa.String(100), nullable=False, unique=True),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('mode', sa.String(64), nullable=False),
        sa.Column('series', sa.Text(), nullable=False),
        sa.Column('episode', sa.Integer(), nullable=False),
        *[sa.Column(n, sa.Text(), nullable=False) for n in ('script','direction','problem','hook','payoff')],
        sa.Column('sources', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('offered_at', sa.DateTime(timezone=True)),
        sa.Column('completed_at', sa.DateTime(timezone=True)),
        sa.Column('run_id', sa.String(64)),
        sa.Column('delivery_index', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index('ix_discipline_ideas_status','discipline_ideas',['status'])


def downgrade():
    op.drop_table('discipline_ideas')
    op.drop_table('discipline_queue_schedule')
