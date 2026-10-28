"""Historisation par avenants et reconstitution de l'etat d'un contrat a une date quelconque.

C'est la brique que la specification resume par « reconstituer l'etat d'un contrat a n'importe quelle
date ». Elle sert trois usages tres differents, et c'est pourquoi elle merite d'etre exacte :

- **Un gestionnaire au telephone** : « quelle formule aviez-vous en mars 2025 ? »
- **Le calcul des commissions** : le taux et l'apporteur a retenir sont ceux en vigueur a la date
  d'echeance, pas ceux d'aujourd'hui. Sans cela, un changement d'apporteur reecrirait l'histoire des
  bordereaux deja regles.
- **Le module 5** : les variables du modele de resiliation se construisent a une date d'observation, sans
  aucune information posterieure. La reconstitution est ce qui garantit l'absence de fuite temporelle.

La regle d'ordre, quand deux avenants partagent une date d'effet : le numero d'ordre le plus eleve
l'emporte. Deux avenants le meme jour arrivent reellement, par exemple une revalorisation au 1er janvier
saisie avant un changement de formule demande par l'adherent pour ce meme 1er janvier. Le dernier saisi
est celui qui vaut.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from courtage.modeles.enums import Fractionnement, MotifResiliation, StatutContrat
from courtage.modeles.portefeuille import Avenant, Contrat


class EtatIntrouvable(Exception):
    """Le contrat n'existait pas encore a la date demandee."""


@dataclass(frozen=True, slots=True)
class EtatContrat:
    """Etat d'un contrat a une date donnee, tel qu'il resulte de l'avenant en vigueur ce jour-la."""

    numero_contrat: str
    a_la_date: date
    statut: StatutContrat
    formule_id: int
    apporteur_id: int
    cotisation_mensuelle_cents: int
    fractionnement: Fractionnement
    motif_resiliation: MotifResiliation | None
    #: Avenant qui porte cet etat : son rang, son type et sa date d'effet. Affiche sur la fiche contrat
    #: pour qu'un gestionnaire sache de quel acte vient la valeur qu'il lit.
    avenant_numero_ordre: int
    avenant_type: str
    avenant_date_effet: date

    @property
    def est_en_portefeuille(self) -> bool:
        """Le contrat compte-t-il dans le portefeuille a cette date.

        Un contrat suspendu compte : la garantie est suspendue, le contrat n'est pas sorti. Un devis ne
        compte pas, il n'a jamais pris effet.
        """
        return self.statut in (StatutContrat.EN_COURS, StatutContrat.SUSPENDU)

    @property
    def garantie_en_cours(self) -> bool:
        """La garantie court-elle a cette date, c'est-a-dire une prestation peut-elle etre servie.

        A ne pas confondre avec `est_en_portefeuille`, et la nuance n'est pas academique : un contrat
        **suspendu** compte toujours au portefeuille, puisqu'il n'est pas sorti, mais sa garantie ne
        court plus. Servir une prestation pendant cette periode est une anomalie de gestion que la
        compagnie reprend au cabinet.

        Le portefeuille repond a « combien de contrats ai-je », la garantie repond a « ce soin est-il
        couvert ». Deux questions, deux reponses.
        """
        return self.statut is StatutContrat.EN_COURS


def avenant_en_vigueur(session: Session, contrat_id: int, a_la_date: date) -> Avenant | None:
    """Avenant en vigueur sur un contrat a une date donnee, ou None si le contrat n'existait pas encore."""
    requete = (
        select(Avenant)
        .where(Avenant.contrat_id == contrat_id, Avenant.date_effet <= a_la_date)
        .order_by(Avenant.date_effet.desc(), Avenant.numero_ordre.desc())
        .limit(1)
    )
    return session.scalars(requete).first()


def etat_a_la_date(session: Session, contrat: Contrat, a_la_date: date) -> EtatContrat:
    """Reconstitue l'etat d'un contrat a une date quelconque.

    Leve `EtatIntrouvable` si la date precede la souscription : repondre « statut inconnu » serait plus
    commode, mais un ecran qui affiche un etat vide pour une date anterieure au contrat induit en erreur.
    """
    avenant = avenant_en_vigueur(session, contrat.id, a_la_date)
    if avenant is None:
        raise EtatIntrouvable(
            f"le contrat {contrat.numero_contrat} n'existait pas le {a_la_date:%d/%m/%Y} :"
            f" il a ete souscrit le {contrat.date_souscription:%d/%m/%Y}"
        )
    return EtatContrat(
        numero_contrat=contrat.numero_contrat,
        a_la_date=a_la_date,
        statut=avenant.statut,
        formule_id=avenant.formule_id,
        apporteur_id=avenant.apporteur_id,
        cotisation_mensuelle_cents=avenant.cotisation_mensuelle_cents,
        fractionnement=avenant.fractionnement,
        motif_resiliation=avenant.motif_resiliation,
        avenant_numero_ordre=avenant.numero_ordre,
        avenant_type=avenant.type_avenant.value,
        avenant_date_effet=avenant.date_effet,
    )


def applique_avenant_au_contrat(contrat: Contrat, avenant: Avenant) -> None:
    """Reporte sur le contrat l'etat porte par un avenant.

    Les colonnes d'etat courant de `contrat` sont un cache de l'avenant le plus recent. Cette fonction est
    le seul endroit qui le met a jour, pour qu'il n'existe qu'un chemin de synchronisation. Le test
    `test_cache_contrat_egale_reconstitution` verifie, sur l'ensemble du portefeuille genere, que ce cache
    correspond bien a la reconstitution.
    """
    contrat.statut = avenant.statut
    contrat.formule_id = avenant.formule_id
    contrat.apporteur_id = avenant.apporteur_id
    contrat.cotisation_mensuelle_cents = avenant.cotisation_mensuelle_cents
    contrat.fractionnement = avenant.fractionnement

    if avenant.statut is StatutContrat.RESILIE:
        contrat.date_resiliation = avenant.date_effet
        contrat.motif_resiliation = avenant.motif_resiliation
    else:
        contrat.date_resiliation = None
        contrat.motif_resiliation = None

    # La mise en vigueur est ce qui donne au contrat sa date d'effet. Les avenants suivants ne la
    # deplacent pas : la date d'effet du contrat est celle de son entree en garantie, pas celle de sa
    # derniere modification.
    if contrat.date_effet is None and avenant.statut is not StatutContrat.DEVIS:
        contrat.date_effet = avenant.date_effet


def prochain_numero_ordre(session: Session, contrat_id: int) -> int:
    """Rang du prochain avenant d'un contrat. Zero pour la souscription."""
    dernier = session.scalars(
        select(Avenant.numero_ordre)
        .where(Avenant.contrat_id == contrat_id)
        .order_by(Avenant.numero_ordre.desc())
        .limit(1)
    ).first()
    return 0 if dernier is None else dernier + 1
