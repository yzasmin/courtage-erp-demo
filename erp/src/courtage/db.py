"""Moteur, sessions, et la seule difference assumee entre SQLite et PostgreSQL.

Le projet fait tourner le meme code sur deux moteurs. Une seule divergence demande du code specifique, et
elle est importante : **SQLite n'applique pas les cles etrangeres par defaut.** Sans la directive
`PRAGMA foreign_keys = ON` posee a chaque connexion, un test passerait en local en inserant un contrat
rattache a un adherent inexistant, puis echouerait en production. Le reste du schema, contraintes de
verification comprises, se comporte a l'identique sur les deux moteurs.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from courtage.config import parametres


def active_cles_etrangeres_sqlite(moteur: Engine) -> None:
    """Fait appliquer les cles etrangeres par SQLite, a chaque nouvelle connexion.

    A ne pas oublier : SQLite accepte la declaration des cles etrangeres mais ne les verifie pas tant que
    ce pragma n'est pas pose, et il se repose par connexion, non par base.
    """

    @event.listens_for(moteur, "connect")
    def _pose_le_pragma(connexion_dbapi, _enregistrement):
        curseur = connexion_dbapi.cursor()
        curseur.execute("PRAGMA foreign_keys = ON")
        curseur.close()


def cree_moteur(url: str | None = None, echo: bool | None = None) -> Engine:
    """Construit un moteur SQLAlchemy pour l'adresse donnee, ou celle des parametres."""
    url_effective = url or parametres.base_url
    moteur = create_engine(
        url_effective,
        echo=parametres.echo_sql if echo is None else echo,
        future=True,
        # Les inserts du generateur passent par lots : un cache de requetes compilees plus grand evite
        # de recompiler le meme insert des milliers de fois.
        query_cache_size=1200,
    )
    if moteur.dialect.name == "sqlite":
        active_cles_etrangeres_sqlite(moteur)
    return moteur


_moteur: Engine | None = None
_fabrique_session: sessionmaker[Session] | None = None


def moteur() -> Engine:
    """Moteur partage du processus, cree a la premiere demande."""
    global _moteur
    if _moteur is None:
        _moteur = cree_moteur()
    return _moteur


def fabrique_session() -> sessionmaker[Session]:
    global _fabrique_session
    if _fabrique_session is None:
        _fabrique_session = sessionmaker(bind=moteur(), expire_on_commit=False, future=True)
    return _fabrique_session


@contextmanager
def session_portee() -> Iterator[Session]:
    """Session transactionnelle : valide a la sortie normale, annule a la moindre exception.

    C'est la frontiere transactionnelle du projet. Aucune ecriture partielle ne survit a une erreur, ce
    qui est l'exigence posee par la specification pour les traitements par lot.
    """
    session = fabrique_session()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def session_api() -> Iterator[Session]:
    """Dependance FastAPI. Ne valide pas : ce sont les services qui decident de valider."""
    session = fabrique_session()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
