"""lti identities and contexts

Adds the two tables the Moodle/LTI launch needs:

``user_identities``
    One row per external account a user signs in with. An LTI ``sub``
    is only unique within one platform, so the identity is the triple
    (provider, issuer, subject).

``lti_contexts``
    Moodle courses, recorded as their own entity. ``course_id`` is
    nullable on purpose — a Moodle course and a Studiengruppe are not
    the same thing, and the mapping is a deliberate act, not a default.

Revision ID: b988e7ef2934
Revises: 8a6766f6326b
Create Date: 2026-09-12 10:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b988e7ef2934'
down_revision: Union[str, None] = '8a6766f6326b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_identities',
        sa.Column('identityId', sa.UUID(), nullable=False),
        sa.Column('userId', sa.UUID(), nullable=False),
        sa.Column(
            'provider',
            sa.Enum('KEYCLOAK', 'LTI', name='identityprovider'),
            nullable=False,
        ),
        sa.Column('issuer', sa.String(), nullable=False),
        sa.Column('subject', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('last_login_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['userId'], ['users.userId'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('identityId'),
        sa.UniqueConstraint(
            'provider', 'issuer', 'subject',
            name='uq_user_identity_provider_subject',
        ),
    )
    op.create_index(
        op.f('ix_user_identities_userId'), 'user_identities', ['userId'], unique=False
    )

    op.create_table(
        'lti_contexts',
        sa.Column('ltiContextId', sa.UUID(), nullable=False),
        sa.Column('issuer', sa.String(), nullable=False),
        sa.Column('context_id', sa.String(), nullable=False),
        sa.Column('title', sa.String(), nullable=True),
        sa.Column('label', sa.String(), nullable=True),
        sa.Column('courseId', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['courseId'], ['courses.courseId'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('ltiContextId'),
        sa.UniqueConstraint(
            'issuer', 'context_id', name='uq_lti_context_issuer_context'
        ),
    )
    op.create_index(
        op.f('ix_lti_contexts_courseId'), 'lti_contexts', ['courseId'], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_lti_contexts_courseId'), table_name='lti_contexts')
    op.drop_table('lti_contexts')
    op.drop_index(op.f('ix_user_identities_userId'), table_name='user_identities')
    op.drop_table('user_identities')
    # The enum type is created implicitly with the table above but is
    # not dropped with it — Postgres keeps it around and the next
    # upgrade would fail with "type already exists".
    sa.Enum(name='identityprovider').drop(op.get_bind(), checkfirst=True)
