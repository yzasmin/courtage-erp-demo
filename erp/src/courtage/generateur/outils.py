"""Outils du generateur : tirage pondere, dates, insertion par lots, recalage des sequences.

Une regle gouverne tout ce fichier : **rien ne doit dependre de l'ordre d'iteration d'un ensemble.**
L'ordre d'iteration d'un `set` de chaines varie d'un processus Python a l'autre, parce qu'il depend du
grain de hachage des chaines. Un generateur qui parcourt un ensemble produirait donc deux jeux de
donnees differents a graine egale, et l'empreinte reproductible annoncee serait fausse. Les tirages
passent donc par des listes triees, jamais par des ensembles.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import date, timedelta
from random import Random

from sqlalchemy import insert, inspect, select, text
from sqlalchemy.orm import Session

from courtage.generateur.parametres import TAILLE_LOT_INSERTION


def tire_pondere(alea: Random, poids: Mapping[str, int]) -> str:
    """Tire une cle selon des poids relatifs. Les cles sont triees, pour rester deterministe."""
    cles = sorted(poids)
    return alea.choices(cles, weights=[poids[cle] for cle in cles], k=1)[0]


def tire_pondere_indices(alea: Random, poids: Sequence[int]) -> int:
    """Tire un indice selon des poids relatifs positionnels."""
    return alea.choices(range(len(poids)), weights=list(poids), k=1)[0]


def premier_du_mois(jour: date) -> date:
    return jour.replace(day=1)


def ajoute_mois(jour: date, mois: int) -> date:
    """Decale une date d'un nombre de mois, en ramenant le jour au dernier jour du mois si besoin.

    Le 31 janvier plus un mois donne le 28 ou le 29 fevrier. Sans ce repli, la generation planterait sur
    les contrats a echeance en fin de mois.
    """
    total = jour.month - 1 + mois
    annee = jour.year + total // 12
    mois_cible = total % 12 + 1
    jours_dans_le_mois = (date(annee + (mois_cible // 12), mois_cible % 12 + 1, 1) - timedelta(days=1)).day
    return date(annee, mois_cible, min(jour.day, jours_dans_le_mois))


def dernier_jour_du_mois(jour: date) -> date:
    suivant = ajoute_mois(jour.replace(day=1), 1)
    return suivant - timedelta(days=1)


def age_a_la_date(naissance: date, a_la_date: date) -> int:
    """Age revolu a une date donnee."""
    return a_la_date.year - naissance.year - ((a_la_date.month, a_la_date.day) < (naissance.month, naissance.day))


def mois_entre(debut: date, fin: date) -> int:
    """Nombre de mois revolus entre deux dates."""
    return (fin.year - debut.year) * 12 + fin.month - debut.month


def par_lots[T](elements: Iterable[T], taille: int = TAILLE_LOT_INSERTION) -> Iterator[list[T]]:
    """Decoupe un flux en lots de taille fixe."""
    lot: list[T] = []
    for element in elements:
        lot.append(element)
        if len(lot) >= taille:
            yield lot
            lot = []
    if lot:
        yield lot


def insere_par_lots(session: Session, modele, lignes: Sequence[dict]) -> int:
    """Insere des lignes par lots, en passant par le noyau SQLAlchemy plutot que par l'ORM.

    L'ORM instancierait 80 000 objets Python et les suivrait dans sa session, pour rien : ces lignes ne
    seront ni modifiees ni relues pendant la generation. Le noyau envoie directement les valeurs, ce qui
    tient dans la memoire disponible sur ce poste.
    """
    total = 0
    for lot in par_lots(lignes):
        session.execute(insert(modele), lot)
        total += len(lot)
        # Vider la session entre les lots libere la memoire que SQLAlchemy garde pour le suivi des
        # identites, inutile ici puisque rien n'est relu.
        session.flush()
    return total


def recale_sequences(session: Session, modeles: Sequence[type]) -> list[str]:
    """Remet les sequences PostgreSQL au-dela du plus grand identifiant insere.

    Pourquoi c'est necessaire, et pourquoi c'est le genre d'oubli qui se paie cher. Le generateur
    attribue les identifiants lui-meme, ce qui le rend deterministe et rapide. Mais sur PostgreSQL, une
    insertion qui fournit explicitement la cle primaire **ne fait pas avancer la sequence**. La premiere
    souscription saisie ensuite dans l'interface reclamerait alors l'identifiant 1, deja pris, et
    echouerait sur une violation d'unicite. Le symptome apparait apres la reprise de donnees, jamais
    pendant, ce qui le rend difficile a relier a sa cause.

    SQLite n'est pas concerne : une colonne `INTEGER PRIMARY KEY` repart de `max(rowid) + 1`.
    Retourne la liste des sequences recalees, pour que la preuve puisse l'afficher.
    """
    if session.get_bind().dialect.name != "postgresql":
        return []

    recalees: list[str] = []
    for modele in modeles:
        table = inspect(modele).local_table
        colonne = "id"
        nom_sequence = session.scalar(text("SELECT pg_get_serial_sequence(:table, :colonne)"),
                                      {"table": table.name, "colonne": colonne})
        if nom_sequence is None:
            continue
        maximum = session.scalar(select(text(f"COALESCE(MAX({colonne}), 0)")).select_from(table))
        session.execute(text("SELECT setval(:sequence, :valeur)"),
                        {"sequence": nom_sequence, "valeur": max(int(maximum or 0), 1)})
        recalees.append(f"{table.name}.{colonne} -> {maximum}")
    return recalees
