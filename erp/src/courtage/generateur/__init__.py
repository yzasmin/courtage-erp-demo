"""Generateur du jeu de demonstration, deterministe a graine fixe.

L'enchainement : referentiel, puis conventions de delegation, puis portefeuille, puis flux. L'ordre
compte, chaque etape s'appuyant sur les identifiants de la precedente.

Un seul tirage aleatoire traverse toute la generation, cree a partir de la graine et consomme dans un
ordre fixe. C'est ce qui rend l'empreinte reproductible : deplacer un appel a `alea` deplacerait tout ce
qui suit, et l'empreinte changerait. Les tests de reproductibilite le detectent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from random import Random

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from courtage.generateur.delegation_datee import DelegationDatee, PerimetreDate
from courtage.generateur.financier import Flux, genere_flux
from courtage.generateur.outils import recale_sequences
from courtage.generateur.portefeuille import Portefeuille, genere_portefeuille
from courtage.generateur.referentiel import Referentiel, genere_referentiel
from courtage.modeles import (
    Adherent,
    AppelCotisation,
    Avenant,
    Contrat,
    Encaissement,
    EntrepriseSouscriptrice,
    Prestation,
)
from courtage.modeles.collectif import LigneAppelCotisation, PoliceCollective
from courtage.modeles.delegation import ActeDelegue, ConventionDelegation, ConventionProduit
from courtage.modeles.enums import ActeGestion

#: Tables dont le generateur attribue lui-meme les identifiants. Leurs sequences PostgreSQL doivent
#: etre recalees apres la generation, sinon la premiere ecriture par l'API echouerait.
TABLES_A_RECALER = (
    EntrepriseSouscriptrice,
    PoliceCollective,
    LigneAppelCotisation,
    Adherent,
    Contrat,
    Avenant,
    AppelCotisation,
    Encaissement,
    Prestation,
)


@dataclass
class ResultatGeneration:
    """Compte rendu de la generation, affiche par la commande et enregistre dans `results/`."""

    graine: int
    date_reference: date
    entreprises: int
    adherents: int
    contrats: int
    avenants: int
    polices: int
    appels: int
    lignes_appel: int
    encaissements: int
    prestations: int
    sequences_recalees: list[str]

    def en_dictionnaire(self) -> dict:
        return {
            "graine": self.graine,
            "date_reference": self.date_reference.isoformat(),
            "entreprises": self.entreprises,
            "adherents": self.adherents,
            "contrats": self.contrats,
            "avenants": self.avenants,
            "polices_collectives": self.polices,
            "appels_cotisation": self.appels,
            "lignes_appel_cotisation": self.lignes_appel,
            "encaissements": self.encaissements,
            "prestations": self.prestations,
            "sequences_recalees": self.sequences_recalees,
        }


def base_est_vide(session: Session) -> bool:
    """Dit si la base ne contient aucun adherent. Sert a l'option `--si-vide` du compose."""
    return session.scalar(select(func.count()).select_from(Adherent)) == 0


def _actes_delegues_par_produit(session: Session) -> DelegationDatee:
    """Perimetre delegue par produit et par periode de validite, lu une fois pour la generation.

    Le generateur applique ainsi les memes conventions que l'API, aux memes dates : un produit dont le
    quittancement n'est pas delegue ne recoit aucun appel de cotisation, un produit dont les prestations
    ne sont pas deleguees n'a aucune prestation, et un produit dont la convention a expire ne recoit
    plus rien a partir de ce jour.
    """
    from courtage.modeles.referentiel import Produit

    lignes = session.execute(
        select(
            Produit.code,
            ConventionDelegation.id,
            ConventionDelegation.date_debut_validite,
            ConventionDelegation.date_fin_validite,
            ActeDelegue.acte,
        )
        .join(ConventionProduit, ConventionProduit.produit_id == Produit.id)
        .join(ConventionDelegation, ConventionDelegation.id == ConventionProduit.convention_id)
        .join(ActeDelegue, ActeDelegue.convention_id == ConventionDelegation.id)
    ).all()

    par_convention: dict[tuple[str, int], tuple[date, date | None, set[ActeGestion]]] = {}
    for code_produit, convention_id, debut, fin, acte in lignes:
        cle = (code_produit, convention_id)
        if cle not in par_convention:
            par_convention[cle] = (debut, fin, set())
        par_convention[cle][2].add(ActeGestion(acte))

    periodes: dict[str, list[PerimetreDate]] = {}
    for (code_produit, _convention_id), (debut, fin, actes) in sorted(par_convention.items()):
        periodes.setdefault(code_produit, []).append(
            PerimetreDate(date_debut=debut, date_fin=fin, actes=frozenset(actes))
        )
    for liste in periodes.values():
        liste.sort(key=lambda p: p.date_debut)
    return DelegationDatee(periodes)


def genere_tout(session: Session, graine: int, date_reference: date) -> ResultatGeneration:
    """Genere l'ensemble du jeu de demonstration dans une base deja migree et vide."""
    alea = Random(graine)

    ref: Referentiel = genere_referentiel(session, alea, date_reference)
    delegation = _actes_delegues_par_produit(session)

    portefeuille: Portefeuille = genere_portefeuille(session, alea, ref, date_reference)
    flux: Flux = genere_flux(session, alea, ref, portefeuille, date_reference, delegation)

    sequences = recale_sequences(session, TABLES_A_RECALER)

    return ResultatGeneration(
        graine=graine,
        date_reference=date_reference,
        entreprises=portefeuille.entreprises,
        adherents=portefeuille.adherents,
        contrats=len(portefeuille.contrats or []),
        avenants=portefeuille.avenants,
        polices=len(portefeuille.polices or []),
        appels=flux.appels,
        lignes_appel=flux.lignes_appel,
        encaissements=flux.encaissements,
        prestations=flux.prestations,
        sequences_recalees=sequences,
    )
