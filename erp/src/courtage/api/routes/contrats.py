"""Routes des contrats et des avenants, dont l'endpoint « etat du contrat a la date D ».

Les refus metier ne sont pas traites ici : ils remontent sous forme d'exceptions du domaine, et
`api.app` les traduit en codes HTTP. Une route qui attraperait elle-meme ces exceptions finirait par
repondre un message different d'une route a l'autre pour le meme refus.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from courtage.api.schemas import (
    AdherentResume,
    AvenantSortie,
    ContratDetail,
    DemandeAvenant,
    DemandeMiseEnVigueur,
    DemandePrestationEntree,
    DemandeResiliation,
    DemandeSouscriptionEntree,
    EtatContratSortie,
    ListeContrats,
    Pagination,
    PrestationSortie,
)
from courtage.api.transformation import (
    avenant_sortie,
    contrat_resume_depuis_lignes,
    garantie_sortie,
    perimetre_sortie,
)
from courtage.db import session_api
from courtage.domaine.avenants import etat_a_la_date
from courtage.domaine.delegation import perimetre_delegue
from courtage.domaine.montants import formate_euros
from courtage.modeles.enums import StatutContrat
from courtage.services import contrats as service_contrats
from courtage.services import lecture

routeur = APIRouter(prefix="/api/v1/contrats", tags=["contrats"])

SessionDep = Annotated[Session, Depends(session_api)]


def _contrat_ou_404(session: Session, numero_contrat: str):
    try:
        return service_contrats.contrat_par_numero(session, numero_contrat)
    except service_contrats.ContratIntrouvable as erreur:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "contrat_introuvable", "message": str(erreur)},
        ) from erreur


@routeur.get(
    "",
    response_model=ListeContrats,
    summary="Liste du portefeuille",
    description="Liste paginee des contrats, filtrable par statut, par produit et par apporteur.",
)
def liste(
    session: SessionDep,
    statut: Annotated[StatutContrat | None, Query(description="devis, en_cours, suspendu ou resilie")] = None,
    code_produit: Annotated[str | None, Query(description="code du produit, par exemple SI-ESSENTIEL")] = None,
    code_apporteur: Annotated[str | None, Query(description="code de l'apporteur, par exemple AP01")] = None,
    recherche: Annotated[str | None, Query(description="fragment du numero de contrat")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    par_page: Annotated[int, Query(ge=1, le=200)] = 25,
) -> ListeContrats:
    resultat = lecture.liste_contrats(
        session,
        statut=statut,
        code_produit=code_produit,
        code_apporteur=code_apporteur,
        recherche=recherche,
        page=page,
        par_page=par_page,
    )
    return ListeContrats(
        donnees=[contrat_resume_depuis_lignes(*ligne) for ligne in resultat.lignes],
        pagination=Pagination(
            total=resultat.total, page=resultat.page, par_page=resultat.par_page, pages=resultat.pages
        ),
    )


@routeur.get(
    "/{numero_contrat}",
    response_model=ContratDetail,
    summary="Fiche d'un contrat",
    description=(
        "Fiche complete : garanties de la formule en cours, historique des avenants, cumuls de"
        " cotisations et de prestations, et perimetre delegue par la compagnie sur ce produit."
    ),
    responses={status.HTTP_404_NOT_FOUND: {"description": "aucun contrat ne porte ce numero"}},
)
def fiche(session: SessionDep, numero_contrat: str) -> ContratDetail:
    ligne = lecture.contrat_enrichi(session, numero_contrat)
    if ligne is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "contrat_introuvable", "message": f"aucun contrat ne porte le numero {numero_contrat}"},
        )
    contrat, produit, branche, formule, apporteur, compagnie, adherent, raison_sociale = ligne

    resume = contrat_resume_depuis_lignes(contrat, produit, branche, formule, apporteur)
    soldes = lecture.soldes_de_contrat(session, contrat.id)
    perimetre = perimetre_delegue(session, produit.id, date.today())

    return ContratDetail(
        **resume.model_dump(),
        adherent=AdherentResume(
            numero_adherent=adherent.numero_adherent,
            nom=adherent.nom,
            prenom=adherent.prenom,
            date_naissance=adherent.date_naissance,
            departement=adherent.departement,
            entreprise=raison_sociale,
        ),
        entreprise=raison_sociale,
        compagnie=compagnie.raison_sociale,
        date_souscription=contrat.date_souscription,
        fractionnement=contrat.fractionnement,
        garanties=[
            garantie_sortie(garantie, couverture)
            for garantie, couverture in lecture.garanties_de_formule(session, contrat.formule_id)
        ],
        avenants=[
            avenant_sortie(avenant, code_formule, code_apporteur)
            for avenant, code_formule, code_apporteur in lecture.avenants_de_contrat(session, contrat.id)
        ],
        perimetre_delegue=perimetre_sortie(produit.code, perimetre),
        cotisations_appelees_cents=soldes.cotisations_appelees_cents,
        cotisations_encaissees_cents=soldes.cotisations_encaissees_cents,
        prestations_reglees_cents=soldes.prestations_reglees_cents,
    )


@routeur.get(
    "/{numero_contrat}/etat",
    response_model=EtatContratSortie,
    summary="Etat du contrat a une date donnee",
    description=(
        "Reconstitue l'etat du contrat a la date demandee, en lisant l'avenant en vigueur ce jour-la :"
        " statut, formule, apporteur, cotisation et fractionnement. La reponse indique de quel avenant"
        " provient l'etat retourne, pour qu'une valeur contestee puisse etre remontee a son acte."
        "\n\nSans parametre, la date du jour est utilisee."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "contrat inconnu, ou date anterieure a la souscription du contrat"
        }
    },
)
def etat(
    session: SessionDep,
    numero_contrat: str,
    a_la_date: Annotated[
        date | None, Query(description="date de reconstitution, au format AAAA-MM-JJ. Defaut : aujourd'hui")
    ] = None,
) -> EtatContratSortie:
    contrat = _contrat_ou_404(session, numero_contrat)
    jour = a_la_date or date.today()
    etat_reconstitue = etat_a_la_date(session, contrat, jour)

    from courtage.modeles.referentiel import Apporteur, Formule

    formule = session.get(Formule, etat_reconstitue.formule_id)
    apporteur = session.get(Apporteur, etat_reconstitue.apporteur_id)

    return EtatContratSortie(
        numero_contrat=etat_reconstitue.numero_contrat,
        a_la_date=etat_reconstitue.a_la_date,
        statut=etat_reconstitue.statut,
        est_en_portefeuille=etat_reconstitue.est_en_portefeuille,
        garantie_en_cours=etat_reconstitue.garantie_en_cours,
        code_formule=formule.code,
        libelle_formule=formule.libelle,
        code_apporteur=apporteur.code,
        nom_apporteur=apporteur.nom,
        cotisation_mensuelle_cents=etat_reconstitue.cotisation_mensuelle_cents,
        cotisation_mensuelle_affichee=formate_euros(etat_reconstitue.cotisation_mensuelle_cents),
        fractionnement=etat_reconstitue.fractionnement,
        motif_resiliation=etat_reconstitue.motif_resiliation,
        avenant_numero_ordre=etat_reconstitue.avenant_numero_ordre,
        avenant_type=etat_reconstitue.avenant_type,
        avenant_date_effet=etat_reconstitue.avenant_date_effet,
    )


@routeur.get(
    "/{numero_contrat}/avenants",
    response_model=list[AvenantSortie],
    summary="Historique des avenants d'un contrat",
)
def avenants(session: SessionDep, numero_contrat: str) -> list[AvenantSortie]:
    contrat = _contrat_ou_404(session, numero_contrat)
    return [
        avenant_sortie(avenant, code_formule, code_apporteur)
        for avenant, code_formule, code_apporteur in lecture.avenants_de_contrat(session, contrat.id)
    ]


@routeur.post(
    "",
    response_model=ContratDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Souscrire un contrat",
    description=(
        "Cree un contrat **a l'etat de devis**, avec son avenant de souscription. Un devis n'a pas de"
        " date d'effet et ne se quittance pas : la mise en vigueur est un acte distinct."
        "\n\nL'acte d'affiliation doit etre delegue au cabinet par la compagnie du produit, sinon la"
        " souscription est refusee."
    ),
    responses={
        status.HTTP_409_CONFLICT: {"description": "acte non delegue au cabinet pour ce produit"},
        status.HTTP_422_UNPROCESSABLE_ENTITY: {"description": "reference inconnue ou donnee invalide"},
    },
)
def souscrire(session: SessionDep, demande: DemandeSouscriptionEntree, reponse: Response) -> ContratDetail:
    contrat = service_contrats.souscrit_contrat(
        session,
        service_contrats.DemandeSouscription(
            numero_adherent=demande.numero_adherent,
            code_produit=demande.code_produit,
            code_formule=demande.code_formule,
            code_apporteur=demande.code_apporteur,
            date_souscription=demande.date_souscription,
            cotisation_mensuelle_cents=demande.cotisation_mensuelle_cents,
            fractionnement=demande.fractionnement,
        ),
    )
    session.commit()
    reponse.headers["Location"] = f"/api/v1/contrats/{contrat.numero_contrat}"
    return fiche(session, contrat.numero_contrat)


@routeur.post(
    "/{numero_contrat}/mise-en-vigueur",
    response_model=EtatContratSortie,
    summary="Mettre un devis en vigueur",
    description="Fait passer un devis a l'etat en cours. C'est l'acte qui donne sa date d'effet au contrat.",
    responses={status.HTTP_409_CONFLICT: {"description": "transition interdite, ou acte non delegue"}},
)
def mise_en_vigueur(
    session: SessionDep, numero_contrat: str, demande: DemandeMiseEnVigueur
) -> EtatContratSortie:
    contrat = _contrat_ou_404(session, numero_contrat)
    service_contrats.met_en_vigueur(session, contrat, demande.date_effet, demande.commentaire)
    session.commit()
    return etat(session, numero_contrat, demande.date_effet)


@routeur.post(
    "/{numero_contrat}/resiliation",
    response_model=EtatContratSortie,
    summary="Resilier un contrat",
    description=(
        "Resilie un contrat en cours ou suspendu. **Le motif est obligatoire** : une resiliation sans"
        " motif est refusee, et la contrainte existe aussi en base."
        "\n\nUn devis ne se resilie pas : il n'a jamais pris effet, il reste donc a l'etat de devis."
    ),
    responses={
        status.HTTP_409_CONFLICT: {
            "description": "transition interdite, contrat deja resilie, ou radiation non deleguee"
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {"description": "motif de resiliation manquant"},
    },
)
def resiliation(session: SessionDep, numero_contrat: str, demande: DemandeResiliation) -> EtatContratSortie:
    contrat = _contrat_ou_404(session, numero_contrat)
    service_contrats.resilie(session, contrat, demande.date_effet, demande.motif, demande.commentaire)
    session.commit()
    return etat(session, numero_contrat, demande.date_effet)


@routeur.post(
    "/{numero_contrat}/avenants",
    response_model=EtatContratSortie,
    status_code=status.HTTP_201_CREATED,
    summary="Ajouter un avenant",
    description=(
        "Ajoute un avenant de modification : changement de formule, revalorisation tarifaire,"
        " changement d'apporteur, changement de fractionnement, suspension ou remise en vigueur."
        "\n\nLes champs non fournis reprennent la valeur courante du contrat. La date d'effet ne peut pas"
        " preceder celle du dernier avenant, sans quoi la reconstitution a date deviendrait ambigue."
    ),
    responses={
        status.HTTP_409_CONFLICT: {
            "description": "transition interdite, date d'effet anterieure au dernier avenant, ou acte non delegue"
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {"description": "reference inconnue ou donnee invalide"},
    },
)
def ajouter_avenant(session: SessionDep, numero_contrat: str, demande: DemandeAvenant) -> EtatContratSortie:
    from courtage.modeles.enums import StatutContrat as Statut

    contrat = _contrat_ou_404(session, numero_contrat)

    formule_id = None
    if demande.code_formule:
        from sqlalchemy import select

        from courtage.modeles.referentiel import Formule

        formule = session.scalars(
            select(Formule).where(Formule.produit_id == contrat.produit_id, Formule.code == demande.code_formule)
        ).first()
        if formule is None:
            raise service_contrats.ReferenceInvalide(
                f"la formule {demande.code_formule} n'existe pas sur le produit de ce contrat"
            )
        formule_id = formule.id

    apporteur_id = None
    if demande.code_apporteur:
        apporteur = lecture.apporteur_par_code(session, demande.code_apporteur)
        if apporteur is None:
            raise service_contrats.ReferenceInvalide(
                f"aucun apporteur ne porte le code {demande.code_apporteur}"
            )
        apporteur_id = apporteur.id

    statut_cible = {
        "suspension": Statut.SUSPENDU,
        "remise_en_vigueur": Statut.EN_COURS,
        "mise_en_vigueur": Statut.EN_COURS,
    }.get(demande.type_avenant.value)

    service_contrats.ajoute_avenant(
        session,
        contrat,
        type_avenant=demande.type_avenant,
        date_effet=demande.date_effet,
        statut_cible=statut_cible,
        formule_id=formule_id,
        apporteur_id=apporteur_id,
        cotisation_mensuelle_cents=demande.cotisation_mensuelle_cents,
        fractionnement=demande.fractionnement,
        commentaire=demande.commentaire,
    )
    session.commit()
    return etat(session, numero_contrat, demande.date_effet)


@routeur.post(
    "/{numero_contrat}/prestations",
    response_model=PrestationSortie,
    status_code=status.HTTP_201_CREATED,
    summary="Enregistrer une prestation",
    description=(
        "Enregistre une demande de remboursement sur un contrat, apres les quatre verifications"
        " qu'un gestionnaire ferait : la gestion des prestations est-elle **deleguee** au cabinet sur"
        " ce produit a la date des soins, le contrat etait-il en garantie ce jour-la, la formule en"
        " vigueur couvre-t-elle cette garantie, et le plafond annuel est-il encore disponible."
        "\n\nLe plafond annuel se consomme sur l'annee civile : la reponse indique ce qui avait deja"
        " ete rembourse. Une demande entierement plafonnee est enregistree comme **rejetee** avec son"
        " motif, et non comme un reglement a zero euro."
    ),
    responses={
        status.HTTP_409_CONFLICT: {
            "description": (
                "gestion des prestations non deleguee au cabinet sur ce produit, ou contrat sans"
                " garantie a la date des soins"
            )
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "contrat ou garantie inconnu, ou garantie non couverte par la formule"
        },
    },
)
def enregistrer_prestation(
    session: SessionDep, numero_contrat: str, demande: DemandePrestationEntree
) -> PrestationSortie:
    from sqlalchemy.exc import IntegrityError

    from courtage.domaine.montants import formate_euros, formate_pourcent
    from courtage.services import prestations as service_prestations

    contrat = _contrat_ou_404(session, numero_contrat)
    reference = service_prestations.reference_suivante(session, demande.date_soins)
    try:
        resultat = service_prestations.enregistre_prestation(
            session,
            service_prestations.DemandePrestation(
                numero_contrat=contrat.numero_contrat,
                code_garantie=demande.code_garantie,
                date_soins=demande.date_soins,
                montant_demande_cents=demande.montant_demande_cents,
            ),
            reference,
        )
        session.commit()
    except IntegrityError as erreur:
        # Seule la collision de reference est traduite ici. Deux saisies simultanees sur le meme mois
        # calculent la meme reference, la base en refuse une, et le client doit lire un refus plutot
        # qu'une erreur serveur. Toute autre violation d'integrite est un defaut du logiciel et doit
        # rester visible : elle est relancee telle quelle.
        session.rollback()
        if "reference_prestation" not in str(erreur.orig if erreur.orig is not None else erreur):
            raise
        raise service_prestations.ReferenceDejaAttribuee(
            f"la reference {reference} vient d'etre attribuee a une autre prestation,"
            " relancer la saisie"
        ) from erreur

    prestation = resultat.prestation
    return PrestationSortie(
        reference_prestation=prestation.reference_prestation,
        numero_contrat=contrat.numero_contrat,
        code_garantie=demande.code_garantie,
        date_soins=prestation.date_soins,
        statut=prestation.statut.value,
        montant_demande_cents=prestation.montant_demande_cents,
        montant_demande_affiche=formate_euros(prestation.montant_demande_cents),
        montant_rembourse_cents=prestation.montant_rembourse_cents,
        montant_rembourse_affiche=formate_euros(prestation.montant_rembourse_cents),
        taux_remboursement=formate_pourcent(resultat.taux_remboursement_bp),
        plafond_annuel_cents=resultat.plafond_annuel_cents,
        plafond_annuel_affiche=(
            formate_euros(resultat.plafond_annuel_cents)
            if resultat.plafond_annuel_cents is not None
            else None
        ),
        deja_rembourse_cents=resultat.deja_rembourse_cents,
        deja_rembourse_affiche=formate_euros(resultat.deja_rembourse_cents),
        motif_rejet=prestation.motif_rejet,
    )
