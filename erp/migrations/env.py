"""Environnement Alembic.

Deux reglages portent toute la portabilite des migrations entre PostgreSQL et SQLite.

`render_as_batch=True` : SQLite ne sait pas modifier ni supprimer une colonne ni une contrainte par un
`ALTER TABLE`. Alembic contourne en recreant la table et en recopiant les donnees, mais seulement si ce
mode est demande. Sans lui, les migrations monteraient et refuseraient de descendre sur SQLite, et le
critere d'acceptation du module 1 ne serait pas tenu.

`compare_type=True` : sans ce reglage, un changement de type de colonne passe inapercu a la generation,
et la migration oubliee se decouvre en production.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from courtage.config import parametres
from courtage.db import cree_moteur

# Importer le paquet des modeles remplit `Base.metadata` de toutes les entites du coeur ERP.
from courtage.modeles import Base

# Les tables de la chaine des flux financiers vivent dans leur propre paquet, pour que le
# module 2 n'ait pas a modifier les entites du module 1. Les importer ici est donc
# obligatoire : une table absente de `Base.metadata` serait absente des migrations generees.
import courtage.flux.modeles  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Produit le SQL sans se connecter, pour relecture ou pour un deploiement par script."""
    context.configure(
        url=parametres.base_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _pragma_cles_etrangeres(connexion, *, actif: bool) -> None:  # noqa: ANN001
    """Pose le pragma sur la connexion brute, sans passer par SQLAlchemy.

    Le detour par la connexion du pilote n'est pas un caprice. Emettre ce pragma par
    `exec_driver_sql` demarre une transaction que SQLAlchemy suit : la mise a jour de la table de
    version d'Alembic se retrouve alors dans cette transaction, qui n'est jamais validee, et la
    revision n'est pas enregistree. La migration suivante repart donc de zero et echoue sur une table
    qui existe deja. Le symptome, « table apporteur already exists » au passage de 0001 a 0002, ne
    designe evidemment pas sa cause.
    """
    brute = connexion.connection.dbapi_connection
    curseur = brute.cursor()
    curseur.execute(f"PRAGMA foreign_keys={'ON' if actif else 'OFF'}")
    curseur.close()


def run_migrations_online() -> None:
    """Applique les migrations sur la base configuree.

    Un detail propre a SQLite, et qui ne se voit qu'a la descente. Le mode « batch » recree une table
    pour la modifier : il en fabrique une copie, y transfere les donnees, supprime l'originale et
    renomme la copie. Pendant cette danse, les cles etrangeres des **autres** tables pointent un
    instant vers une table qui n'existe plus. Comme `courtage.db` fait appliquer les cles etrangeres
    par SQLite, la base refuse l'operation, et la migration s'arrete en laissant une table temporaire
    derriere elle.

    La verification est donc suspendue le temps des migrations, puis retablie. C'est ce que la
    documentation d'Alembic prescrit pour ce mode, et cela ne relache rien en exploitation : les
    migrations s'executent seules sur la base, et l'application, elle, garde le pragma actif.
    """
    moteur = cree_moteur(echo=False)
    sur_sqlite = moteur.dialect.name == "sqlite"
    with moteur.connect() as connexion:
        if sur_sqlite:
            _pragma_cles_etrangeres(connexion, actif=False)
        context.configure(
            connection=connexion,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        try:
            with context.begin_transaction():
                context.run_migrations()
        finally:
            if sur_sqlite:
                # Retabli meme en cas d'echec : la connexion peut etre rendue a la reserve.
                _pragma_cles_etrangeres(connexion, actif=True)
    moteur.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
