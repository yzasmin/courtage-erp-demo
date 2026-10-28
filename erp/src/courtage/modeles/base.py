"""Base declarative commune et conventions de nommage des contraintes.

Deux points meritent une explication, parce qu'ils conditionnent tout le reste du schema.

1. **La convention de nommage.** Sans elle, PostgreSQL et SQLite inventent chacun leurs noms de
   contraintes et d'index, et Alembic ne sait plus les redescendre. En la fixant ici, chaque contrainte
   porte un nom previsible, identique sur les deux moteurs, et une migration qui monte sait exactement
   quoi redescendre.

2. **Les montants sont des entiers de centimes.** Il n'y a aucun type a virgule flottante dans ce schema.
   Un bordereau de commissions qui ne tombe pas juste au centime est une anomalie comptable, et le
   flottant finit toujours par en produire une. Les taux suivent la meme logique : ils sont stockes en
   points de base, ou 10 000 points de base valent 100 pour cent.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Integer, MetaData
from sqlalchemy.orm import DeclarativeBase, mapped_column

# Les cles de ce dictionnaire sont celles attendues par SQLAlchemy : index, contrainte d'unicite,
# contrainte de verification, cle etrangere, cle primaire.
CONVENTION_NOMMAGE = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base declarative de toutes les entites de l'ERP."""

    metadata = MetaData(naming_convention=CONVENTION_NOMMAGE)


def cle_primaire():
    """Cle primaire technique, entiere et auto-incrementee."""
    return mapped_column(Integer, primary_key=True, autoincrement=True)


def centimes(**kwargs):
    """Colonne de montant, en centimes d'euro.

    `BigInteger` et non `Integer` : les totaux de bordereaux additionnent des milliers de lignes, et
    la borne des 21 millions d'euros d'un entier 32 bits serait atteignable sur un cumul annuel.
    """
    return mapped_column(BigInteger, **kwargs)


def points_de_base(**kwargs):
    """Colonne de taux, en points de base. 10 000 points de base valent 100 pour cent."""
    return mapped_column(Integer, **kwargs)
