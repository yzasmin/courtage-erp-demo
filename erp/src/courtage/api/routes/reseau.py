"""Routes du reseau de distribution et de la delegation de gestion."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from courtage.api.schemas import ApporteurSortie, ListeApporteurs, PerimetreSortie, formate_taux
from courtage.api.transformation import perimetre_sortie
from courtage.db import session_api
from courtage.domaine.delegation import perimetre_delegue
from courtage.domaine.montants import formate_euros
from courtage.services import lecture

routeur = APIRouter(prefix="/api/v1", tags=["reseau et delegation"])

SessionDep = Annotated[Session, Depends(session_api)]


def _apporteur_sortie(apporteur, contrats: int, cotisation_annualisee: int, taux_bp: int | None):
    return ApporteurSortie(
        code=apporteur.code,
        nom=apporteur.nom,
        type_apporteur=apporteur.type_apporteur,
        numero_orias=apporteur.numero_orias,
        departement=apporteur.departement,
        date_entree=apporteur.date_entree,
        date_sortie=apporteur.date_sortie,
        actif=apporteur.actif,
        contrats_en_portefeuille=int(contrats),
        cotisation_annualisee_cents=int(cotisation_annualisee),
        cotisation_annualisee_affichee=formate_euros(int(cotisation_annualisee)),
        taux_retrocession_cadre=formate_taux(taux_bp),
    )


@routeur.get(
    "/apporteurs",
    response_model=ListeApporteurs,
    summary="Reseau d'apporteurs",
    description=(
        "Reseau de distribution du cabinet, avec pour chaque apporteur le nombre de contrats en"
        " portefeuille, la cotisation annualisee correspondante et son taux de retrocession cadre."
        "\n\nLe portefeuille retenu est l'ensemble des contrats en cours ou suspendus : un devis n'y"
        " compte pas, puisqu'il n'a jamais pris effet, et un contrat resilie en est sorti."
    ),
)
def apporteurs(session: SessionDep) -> ListeApporteurs:
    return ListeApporteurs(
        donnees=[
            _apporteur_sortie(apporteur, contrats, cotisation, taux_bp)
            for apporteur, contrats, cotisation, taux_bp in lecture.liste_apporteurs(session)
        ]
    )


@routeur.get(
    "/apporteurs/{code}",
    response_model=ApporteurSortie,
    summary="Fiche d'un apporteur",
    responses={status.HTTP_404_NOT_FOUND: {"description": "aucun apporteur ne porte ce code"}},
)
def apporteur(session: SessionDep, code: str) -> ApporteurSortie:
    for ligne in lecture.liste_apporteurs(session):
        if ligne[0].code == code:
            return _apporteur_sortie(*ligne)
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "apporteur_introuvable", "message": f"aucun apporteur ne porte le code {code}"},
    )


@routeur.get(
    "/produits/{code_produit}/delegation",
    response_model=PerimetreSortie,
    summary="Perimetre delegue sur un produit",
    description=(
        "Actes de gestion que la compagnie a delegues au cabinet sur ce produit, a la date demandee."
        "\n\nCe que l'ERP autorise depend de cette liste, et **uniquement** de cette liste. La portee"
        " declaree de la convention, totale ou partielle, est un libelle informatif : elle n'elargit"
        " aucun perimetre. Un produit sans convention en vigueur ne permet aucun acte delegue."
    ),
    responses={status.HTTP_404_NOT_FOUND: {"description": "aucun produit ne porte ce code"}},
)
def delegation(
    session: SessionDep,
    code_produit: str,
    a_la_date: Annotated[date | None, Query(description="date d'appreciation, au format AAAA-MM-JJ")] = None,
) -> PerimetreSortie:
    produit = lecture.produit_par_code(session, code_produit)
    if produit is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "produit_introuvable", "message": f"aucun produit ne porte le code {code_produit}"},
        )
    perimetre = perimetre_delegue(session, produit.id, a_la_date or date.today())
    return perimetre_sortie(produit.code, perimetre)
