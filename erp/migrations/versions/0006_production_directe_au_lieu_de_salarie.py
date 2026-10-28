"""Le type d'apporteur « salarie » devient « direct »

Correction de vocabulaire metier, et elle porte sur des **donnees** deja ecrites, pas seulement sur le
code. Un salarie du cabinet n'est pas un apporteur : il ne detient pas de mandat, il n'est pas
immatricule a l'ORIAS, et il ne percoit pas de retrocession puisqu'il est paye par son salaire. Ce que
la table decrivait sur ces lignes est la production directe du cabinet, le canal par lequel une affaire
arrive sans intermediaire.

Pourquoi une migration et pas seulement un changement d'enumeration. La colonne est un `VARCHAR` sans
contrainte de domaine en base, donc une base deja alimentee garde la valeur `salarie`. L'ecriture ne
refuserait rien ; c'est la **lecture** qui echouerait, l'enumeration Python ne connaissant plus cette
valeur. Une base de demonstration se regenere, une base de client non.

Revision : 0006
Revise   : 0005
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE apporteur SET type_apporteur = 'direct' WHERE type_apporteur = 'salarie'")


def downgrade() -> None:
    op.execute("UPDATE apporteur SET type_apporteur = 'salarie' WHERE type_apporteur = 'direct'")
