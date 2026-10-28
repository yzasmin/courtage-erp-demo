"""Contraintes de porteur unique sur l'appel de cotisation et sur la commission

Trois contraintes declarees dans le modele mais absentes du schema reellement migre :

- `ck_appel_cotisation_destinataire_unique` : un appel s'adresse a un contrat individuel **ou** a une
  police collective, jamais aux deux, jamais a aucun ;
- `ck_appel_cotisation_au_moins_un_affilie` : un appel groupe couvre au moins un affilie ;
- `ck_commission_destinataire_unique` : la commission porte le meme porteur unique que l'appel qu'elle
  remunere.

Pourquoi elles manquaient. La revision 0003 a rendu `contrat_id` nullable et ajoute `police_id` sur
`appel_cotisation` et sur `commission`, en s'appuyant sur l'autogeneration d'Alembic. Or Alembic ne
compare pas les contraintes de verification : `compare_metadata` ignore les `CheckConstraint`. Les
regles ajoutees au modele au meme moment n'ont donc jamais ete emises, et le test de non-derive du
schema, qui repose sur cette meme comparaison, est reste vert. La consequence etait concrete : un
import ou un script de reprise pouvait ecrire un appel portant a la fois un contrat et une police, et
cet appel aurait ete quittance deux fois.

Sur SQLite, ajouter une contrainte de verification demande de reconstruire la table : le mode « batch »
configure dans `migrations/env.py` s'en charge, et les donnees sont recopiees. Sur PostgreSQL, c'est un
simple `ALTER TABLE ... ADD CONSTRAINT`.

Revision : 0005
Revise   : 0004
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Expression du porteur unique. Ecrite en `CASE` plutot qu'en addition de booleens, parce que SQLite
#: et PostgreSQL ne rendent pas le meme type pour un booleen et que seule cette forme passe sur les deux.
PORTEUR_UNIQUE = (
    "(CASE WHEN contrat_id IS NULL THEN 0 ELSE 1 END)"
    " + (CASE WHEN police_id IS NULL THEN 0 ELSE 1 END) = 1"
)


def upgrade() -> None:
    with op.batch_alter_table('appel_cotisation', schema=None) as batch_op:
        batch_op.create_check_constraint('destinataire_unique', PORTEUR_UNIQUE)
        batch_op.create_check_constraint('au_moins_un_affilie', 'nombre_affilies >= 1')

    with op.batch_alter_table('commission', schema=None) as batch_op:
        batch_op.create_check_constraint('destinataire_unique', PORTEUR_UNIQUE)


def downgrade() -> None:
    # Les noms sont donnes nus : la convention de nommage du projet, declaree dans
    # `src/courtage/modeles/base.py`, y prefixe « ck_<table>_ ».
    with op.batch_alter_table('commission', schema=None) as batch_op:
        batch_op.drop_constraint('destinataire_unique', type_='check')

    with op.batch_alter_table('appel_cotisation', schema=None) as batch_op:
        batch_op.drop_constraint('au_moins_un_affilie', type_='check')
        batch_op.drop_constraint('destinataire_unique', type_='check')
