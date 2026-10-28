"""Routes des adherents."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from courtage.api.schemas import (
    AdherentDetail,
    AdherentResume,
    ListeAdherents,
    Pagination,
)
from courtage.api.transformation import contrat_resume_depuis_lignes
from courtage.db import session_api
from courtage.services import lecture

routeur = APIRouter(prefix="/api/v1/adherents", tags=["adherents"])

SessionDep = Annotated[Session, Depends(session_api)]


@routeur.get(
    "",
    response_model=ListeAdherents,
    summary="Liste des adherents",
    description=(
        "Liste paginee des adherents. La recherche porte sur le nom, le prenom et le numero"
        " d'adherent, qui sont les trois entrees dont dispose un gestionnaire au telephone."
    ),
)
def liste(
    session: SessionDep,
    recherche: Annotated[str | None, Query(description="nom, prenom ou numero d'adherent")] = None,
    departement: Annotated[str | None, Query(description="code du departement, par exemple 34")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    par_page: Annotated[int, Query(ge=1, le=200)] = 25,
) -> ListeAdherents:
    resultat = lecture.liste_adherents(
        session, recherche=recherche, departement=departement, page=page, par_page=par_page
    )
    donnees = [
        AdherentResume(
            numero_adherent=adherent.numero_adherent,
            nom=adherent.nom,
            prenom=adherent.prenom,
            date_naissance=adherent.date_naissance,
            departement=adherent.departement,
            entreprise=raison_sociale,
            nombre_contrats=nombre,
        )
        for adherent, raison_sociale, nombre in resultat.lignes
    ]
    return ListeAdherents(
        donnees=donnees,
        pagination=Pagination(
            total=resultat.total, page=resultat.page, par_page=resultat.par_page, pages=resultat.pages
        ),
    )


@routeur.get(
    "/{numero_adherent}",
    response_model=AdherentDetail,
    summary="Fiche d'un adherent",
    responses={status.HTTP_404_NOT_FOUND: {"description": "aucun adherent ne porte ce numero"}},
)
def fiche(session: SessionDep, numero_adherent: str) -> AdherentDetail:
    ligne = lecture.adherent_par_numero(session, numero_adherent)
    if ligne is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "adherent_introuvable", "message": f"aucun adherent ne porte le numero {numero_adherent}"},
        )
    adherent, raison_sociale = ligne
    contrats = [
        contrat_resume_depuis_lignes(contrat, produit, branche, formule, apporteur)
        for contrat, produit, branche, formule, apporteur in lecture.contrats_de_adherent(session, adherent.id)
    ]
    return AdherentDetail(
        numero_adherent=adherent.numero_adherent,
        nom=adherent.nom,
        prenom=adherent.prenom,
        date_naissance=adherent.date_naissance,
        departement=adherent.departement,
        entreprise=raison_sociale,
        nombre_contrats=len(contrats),
        sexe=adherent.sexe,
        code_postal=adherent.code_postal,
        courriel=adherent.courriel,
        telephone=adherent.telephone,
        date_creation=adherent.date_creation,
        contrats=contrats,
    )
