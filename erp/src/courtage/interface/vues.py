"""Vues de l'interface de gestionnaire, rendues en HTML.

Les vues ne contiennent aucune regle metier : elles lisent par `services.lecture`, ecrivent par
`services.contrats`, et traduisent les refus en messages a l'ecran. La meme regle qui protege l'API
protege donc les formulaires, sans etre ecrite deux fois.

Un detail d'ergonomie qui compte dans un outil de gestion : quand un acte n'est pas delegue au cabinet,
le bouton correspondant est desactive **et** l'explication est affichee. Laisser le bouton actif pour
refuser ensuite ferait perdre une saisie, et masquer le bouton sans rien dire laisserait croire a un
defaut du logiciel.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from courtage.api.transformation import avenant_sortie, contrat_resume_depuis_lignes, garantie_sortie
from courtage.config import parametres
from courtage.db import session_api
from courtage.domaine.avenants import EtatIntrouvable, etat_a_la_date
from courtage.domaine.cycle_vie import ContratResilie, MotifResiliationManquant, TransitionInterdite
from courtage.domaine.delegation import (
    LIBELLE_ACTE,
    ActeNonDelegue,
    DelegationAmbigue,
    perimetre_delegue,
)
from courtage.domaine.montants import euros_vers_centimes, formate_euros
from courtage.flux.enums import SeveriteAnomalie, StatutLot
from courtage.flux.modeles import AnomalieLot, LotFichier, RapprochementLigne
from courtage.ia import traitement
from courtage.interface import DOSSIER_GABARITS
from courtage.interface.libelles import libelle
from courtage.modeles import DossierPreRempli, ScoreResiliation, StatutDossier
from courtage.modeles.collectif import LigneAppelCotisation, PoliceCollective
from courtage.modeles.delegation import ActeDelegue, ConventionDelegation, ConventionProduit
from courtage.modeles.enums import (
    ActeGestion,
    Fractionnement,
    MotifResiliation,
    StatutContrat,
)
from courtage.modeles.financier import AppelCotisation, Encaissement, Prestation
from courtage.modeles.portefeuille import Adherent, Contrat
from courtage.modeles.referentiel import Apporteur, Branche, Compagnie, Formule, Garantie, Produit
from courtage.services import contrats as service_contrats
from courtage.services import lecture

routeur = APIRouter(tags=["interface"], include_in_schema=False)
gabarits = Jinja2Templates(directory=str(DOSSIER_GABARITS))
# Filtre de gabarit : `{{ contrat.statut | libelle }}` rend « en cours » et non « en_cours ».
gabarits.env.filters["libelle"] = libelle
gabarits.env.filters["euros"] = formate_euros
gabarits.env.filters["date_fr"] = lambda iso: date.fromisoformat(str(iso)).strftime("%d/%m/%Y")


def _rendu(
    requete: Request, nom: str, contexte: dict, code: int = status.HTTP_200_OK
) -> HTMLResponse:
    """Rend un gabarit. Point de passage unique, pour que la signature de Starlette ne soit ecrite
    qu'une fois : la requete y est le premier argument, et non une cle du contexte."""
    return gabarits.TemplateResponse(request=requete, name=nom, context=contexte, status_code=code)

SessionDep = Annotated[Session, Depends(session_api)]

#: Nombre de lignes affichees dans les tableaux secondaires d'une fiche.
LIGNES_APERCU = 12
#: Nombre de lignes affichees sur l'ecran des anomalies.
LIGNES_ANOMALIES = 40
#: Tous les actes de gestion, pour afficher aussi ceux qui ne sont pas delegues.
TOUS_LES_ACTES = tuple(ActeGestion)


def _page_introuvable(requete: Request, message: str) -> HTMLResponse:
    """Page 404 rendue par un gabarit, donc avec echappement automatique.

    Le message contient un identifiant saisi dans l'adresse. Le composer avec une f-string dans du
    HTML brut, comme c'etait le cas, permettait d'injecter du balisage par l'adresse elle-meme.
    Jinja2 echappe par defaut, ce qui ferme la porte sans avoir a y penser a chaque appel.
    """
    return _rendu(requete, "introuvable.html", _contexte("", message=message), status.HTTP_404_NOT_FOUND)


def _fin_historique() -> date:
    """Date a laquelle s'arretent les donnees de demonstration, lue dans la configuration."""
    return date.fromisoformat(parametres.date_reference)


def _contexte(page_active: str, **extra) -> dict:
    """Contexte commun a tous les gabarits. La requete est passee a part, a `_rendu`."""
    return {
        "titre_application": parametres.titre_application,
        "nom_cabinet": parametres.nom_cabinet,
        "mode_demonstration": parametres.mode_demonstration,
        "page_active": page_active,
        # Deux dates distinctes, et les confondre trompait le lecteur. `aujourdhui` est la date du
        # poste : elle sert a pre-remplir un formulaire de saisie, ou la bonne valeur par defaut est
        # bien le jour courant. `fin_historique` est la date a laquelle s'arretent les donnees de
        # demonstration, lue dans la configuration. L'accueil annoncait « Situation au <date du
        # jour> » devant des compteurs arretes au 30/09/2026 : un lecteur en concluait que le
        # portefeuille etait vide depuis des mois.
        "aujourdhui_iso": date.today().isoformat(),
        "fin_historique": _fin_historique().strftime("%d/%m/%Y"),
        **extra,
    }


@dataclass(frozen=True, slots=True)
class _LigneConvention:
    compagnie: str
    reference: str
    portee: str
    depuis: str
    produits: list[str]
    actes: list[str]
    actes_absents: list[str]
    #: Date de fin de validite, et si la convention est encore en vigueur aujourd'hui. Une convention
    #: echue doit se voir : sans cela, deux conventions de la meme compagnie s'affichent cote a cote
    #: sans que rien n'explique pourquoi l'une d'elles ne permet plus rien.
    jusqu_au: str | None
    echue: bool


def _conventions(session: Session) -> list[_LigneConvention]:
    """Conventions de delegation, avec leurs produits et leurs actes, delegues ou non."""
    lignes: list[_LigneConvention] = []
    conventions = session.execute(
        select(ConventionDelegation, Compagnie)
        .join(Compagnie, Compagnie.id == ConventionDelegation.compagnie_id)
        .order_by(Compagnie.code)
    ).all()
    for convention, compagnie in conventions:
        actes = set(
            session.scalars(select(ActeDelegue.acte).where(ActeDelegue.convention_id == convention.id))
        )
        produits = list(
            session.scalars(
                select(Produit.code)
                .join(ConventionProduit, ConventionProduit.produit_id == Produit.id)
                .where(ConventionProduit.convention_id == convention.id)
                .order_by(Produit.code)
            )
        )
        lignes.append(
            _LigneConvention(
                compagnie=compagnie.raison_sociale,
                reference=convention.reference,
                portee=convention.portee.value,
                depuis=convention.date_debut_validite.strftime("%d/%m/%Y"),
                produits=produits,
                actes=[LIBELLE_ACTE[acte] for acte in TOUS_LES_ACTES if acte in actes],
                actes_absents=[LIBELLE_ACTE[acte] for acte in TOUS_LES_ACTES if acte not in actes],
                jusqu_au=(
                    convention.date_fin_validite.strftime("%d/%m/%Y")
                    if convention.date_fin_validite
                    else None
                ),
                echue=(
                    convention.date_fin_validite is not None
                    and convention.date_fin_validite < date.today()
                ),
            )
        )
    return lignes


@routeur.get("/", response_class=HTMLResponse)
def accueil(requete: Request, session: SessionDep) -> HTMLResponse:
    """Ecran d'accueil : synthese du portefeuille, delegations, derniers mouvements."""
    synthese = lecture.synthese_accueil(session)

    par_branche = [
        {
            "libelle": libelle,
            "contrats": contrats,
            "cotisation": formate_euros(int(cotisation) * lecture.MOIS_PAR_AN),
        }
        for libelle, contrats, cotisation in session.execute(
            select(
                Branche.libelle,
                func.count(Contrat.id),
                func.coalesce(func.sum(Contrat.cotisation_mensuelle_cents), 0),
            )
            .join(Produit, Produit.branche_id == Branche.id)
            .join(Contrat, Contrat.produit_id == Produit.id)
            .where(Contrat.statut.in_(lecture.STATUTS_EN_PORTEFEUILLE))
            .group_by(Branche.libelle)
            .order_by(Branche.libelle)
        ).all()
    ]

    derniers = [
        {
            "numero": contrat.numero_contrat,
            "adherent": f"{adherent.nom} {adherent.prenom}",
            "produit": produit.code,
            "statut": contrat.statut.value,
            "statut_libelle": contrat.statut.value,
            "date_effet": contrat.date_effet.strftime("%d/%m/%Y") if contrat.date_effet else None,
            "cotisation": formate_euros(contrat.cotisation_mensuelle_cents),
        }
        for contrat, adherent, produit in session.execute(
            select(Contrat, Adherent, Produit)
            .join(Adherent, Adherent.id == Contrat.adherent_id)
            .join(Produit, Produit.id == Contrat.produit_id)
            .order_by(desc(Contrat.date_souscription), desc(Contrat.id))
            .limit(LIGNES_APERCU)
        ).all()
    ]

    return _rendu(
        requete,
        "accueil.html",
        _contexte(
            "accueil",
            synthese=synthese,
            cotisation_annualisee=formate_euros(synthese.cotisation_annualisee_cents),
            par_branche=par_branche,
            conventions=_conventions(session),
            derniers_contrats=derniers,
        ),
    )


@routeur.get("/adherents", response_class=HTMLResponse)
def liste_adherents(
    requete: Request,
    session: SessionDep,
    recherche: str | None = None,
    departement: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
) -> HTMLResponse:
    """Liste des adherents, avec recherche par nom, prenom ou numero."""
    resultat = lecture.liste_adherents(
        session, recherche=recherche, departement=departement, page=page, par_page=25
    )
    adherents = [
        {
            "numero": adherent.numero_adherent,
            "nom": adherent.nom,
            "prenom": adherent.prenom,
            "naissance": adherent.date_naissance.strftime("%d/%m/%Y"),
            "departement": adherent.departement,
            "entreprise": raison_sociale,
            "contrats": nombre,
        }
        for adherent, raison_sociale, nombre in resultat.lignes
    ]
    departements = list(session.scalars(select(Adherent.departement).distinct().order_by(Adherent.departement)))

    parametres_url = ""
    if recherche:
        parametres_url += f"&recherche={recherche}"
    if departement:
        parametres_url += f"&departement={departement}"

    return _rendu(
        requete,
        "adherents.html",
        _contexte(
            "adherents",
            adherents=adherents,
            recherche=recherche,
            departement=departement,
            departements=departements,
            page=resultat.page,
            pages=resultat.pages,
            total=resultat.total,
            parametres=parametres_url,
        ),
    )


@routeur.get("/adherents/{numero_adherent}", response_class=HTMLResponse)
def fiche_adherent(requete: Request, session: SessionDep, numero_adherent: str) -> HTMLResponse:
    """Fiche adherent : identite, contrats, et score de resiliation de chaque contrat (module 5)."""
    ligne = lecture.adherent_par_numero(session, numero_adherent)
    if ligne is None:
        return _page_introuvable(requete, f"Aucun adherent ne porte le numero {numero_adherent}.")
    adherent, entreprise = ligne
    contrats = [
        contrat_resume_depuis_lignes(*lignes_contrat)
        for lignes_contrat in lecture.contrats_de_adherent(session, adherent.id)
    ]
    aujourdhui = date.today()
    age = (
        aujourdhui.year
        - adherent.date_naissance.year
        - ((aujourdhui.month, aujourdhui.day) < (adherent.date_naissance.month, adherent.date_naissance.day))
    )
    return _rendu(
        requete,
        "adherent.html",
        _contexte(
            "adherents",
            adherent=adherent,
            entreprise=entreprise,
            contrats=contrats,
            age=age,
            scores=_derniers_scores(session, adherent.id),
        ),
    )


def _derniers_scores(session: Session, adherent_id: int) -> list[tuple[str, ScoreResiliation]]:
    """Dernier score de resiliation de chaque contrat de l'adherent, le plus risque en tete.

    Un contrat resilie depuis le dernier scoring garde son dernier score : la fiche le montre tel qu'il
    etait, avec sa date, plutot que de le faire disparaitre.
    """
    derniere_date = (
        select(ScoreResiliation.contrat_id, func.max(ScoreResiliation.date_observation).label("date_max"))
        .group_by(ScoreResiliation.contrat_id)
        .subquery()
    )
    lignes = session.execute(
        select(Contrat.numero_contrat, ScoreResiliation)
        .join(ScoreResiliation, ScoreResiliation.contrat_id == Contrat.id)
        .join(
            derniere_date,
            (derniere_date.c.contrat_id == ScoreResiliation.contrat_id)
            & (derniere_date.c.date_max == ScoreResiliation.date_observation),
        )
        .where(Contrat.adherent_id == adherent_id)
        .order_by(desc(ScoreResiliation.probabilite))
    ).all()
    return [(numero, score) for numero, score in lignes]


@routeur.get("/contrats", response_class=HTMLResponse)
def liste_contrats(
    requete: Request,
    session: SessionDep,
    statut: str | None = None,
    code_produit: str | None = None,
    code_apporteur: str | None = None,
    recherche: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
) -> HTMLResponse:
    """Liste du portefeuille, filtrable par statut, produit et apporteur."""
    statut_filtre = StatutContrat(statut) if statut else None
    resultat = lecture.liste_contrats(
        session,
        statut=statut_filtre,
        code_produit=code_produit,
        code_apporteur=code_apporteur,
        recherche=recherche,
        page=page,
        par_page=25,
    )
    parametres_url = ""
    for nom, valeur in (
        ("statut", statut),
        ("code_produit", code_produit),
        ("code_apporteur", code_apporteur),
        ("recherche", recherche),
    ):
        if valeur:
            parametres_url += f"&{nom}={valeur}"

    return _rendu(
        requete,
        "contrats.html",
        _contexte(
            "contrats",
            contrats=[contrat_resume_depuis_lignes(*ligne) for ligne in resultat.lignes],
            statuts=[s.value for s in StatutContrat],
            statut=statut,
            produits=list(session.scalars(select(Produit).order_by(Produit.code))),
            code_produit=code_produit,
            apporteurs=list(session.scalars(select(Apporteur).order_by(Apporteur.code))),
            code_apporteur=code_apporteur,
            recherche=recherche,
            page=resultat.page,
            pages=resultat.pages,
            total=resultat.total,
            parametres=parametres_url,
        ),
    )


def _appels_du_contrat(session: Session, contrat, police) -> list[dict]:
    """Appels de cotisation a afficher sur la fiche d'un contrat.

    En individuel, ce sont les appels du contrat. En collectif, le contrat n'est pas quittance pour
    lui-meme : c'est sa police qui l'est, et la part de l'affilie se lit dans la ligne de detail.
    Ne chercher que `contrat_id`, comme avant la modelisation de la police, afficherait un tableau
    vide et laisserait croire que le salarie n'a jamais ete appele.
    """
    if police is not None:
        requete = (
            select(AppelCotisation, LigneAppelCotisation.montant_total_cents)
            .join(
                LigneAppelCotisation,
                (LigneAppelCotisation.appel_id == AppelCotisation.id)
                & (LigneAppelCotisation.contrat_id == contrat.id),
            )
            .where(AppelCotisation.police_id == police.id)
            .order_by(desc(AppelCotisation.date_echeance))
            .limit(LIGNES_APERCU)
        )
        return [
            {
                "reference": appel.reference_appel,
                "periode": f"{appel.periode_debut:%m/%Y} au {appel.periode_fin:%m/%Y}",
                "echeance": appel.date_echeance.strftime("%d/%m/%Y"),
                "statut": appel.statut.value,
                "total": formate_euros(part),
                "groupe": f"appel groupé, {appel.nombre_affilies} affiliés",
            }
            for appel, part in session.execute(requete).all()
        ]

    return [
        {
            "reference": appel.reference_appel,
            "periode": f"{appel.periode_debut:%m/%Y} au {appel.periode_fin:%m/%Y}",
            "echeance": appel.date_echeance.strftime("%d/%m/%Y"),
            "statut": appel.statut.value,
            "total": formate_euros(appel.montant_total_cents),
            "groupe": None,
        }
        for appel in session.scalars(
            select(AppelCotisation)
            .where(AppelCotisation.contrat_id == contrat.id)
            .order_by(desc(AppelCotisation.date_echeance))
            .limit(LIGNES_APERCU)
        )
    ]


def _rend_fiche_contrat(
    requete: Request,
    session: Session,
    numero_contrat: str,
    a_la_date: str | None,
    message: str | None = None,
    message_type: str = "succes",
    code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """Rend la fiche contrat. Partage par la lecture et par les retours de formulaire."""
    ligne = lecture.contrat_enrichi(session, numero_contrat)
    if ligne is None:
        return _page_introuvable(requete, f"Aucun contrat ne porte le numero {numero_contrat}.")
    contrat, produit, branche, formule, apporteur, compagnie, adherent, entreprise = ligne

    jour = date.fromisoformat(a_la_date) if a_la_date else date.today()
    etat = None
    erreur_etat = None
    etat_formule = None
    etat_apporteur = None
    etat_cotisation = None
    try:
        etat = etat_a_la_date(session, contrat, jour)
        etat_formule = session.get(Formule, etat.formule_id)
        etat_apporteur = session.get(Apporteur, etat.apporteur_id)
        etat_cotisation = formate_euros(etat.cotisation_mensuelle_cents)
    except EtatIntrouvable as erreur:
        erreur_etat = str(erreur)

    perimetre = perimetre_delegue(session, produit.id, date.today())
    soldes = lecture.soldes_de_contrat(session, contrat.id)

    police = session.get(PoliceCollective, contrat.police_id) if contrat.police_id else None
    appels = _appels_du_contrat(session, contrat, police)
    prestations = [
        {
            "reference": prestation.reference_prestation,
            "garantie": libelle_garantie,
            "date_soins": prestation.date_soins.strftime("%d/%m/%Y"),
            "statut": prestation.statut.value,
            "rembourse": formate_euros(prestation.montant_rembourse_cents),
        }
        for prestation, libelle_garantie in session.execute(
            select(Prestation, Garantie.libelle)
            .join(Garantie, Garantie.id == Prestation.garantie_id)
            .where(Prestation.contrat_id == contrat.id)
            .order_by(desc(Prestation.date_soins))
            .limit(LIGNES_APERCU)
        ).all()
    ]

    return _rendu(
        requete,
        "contrat.html",
        _contexte(
            "contrats",
            contrat=contrat,
            produit=produit,
            branche=branche,
            formule=formule,
            apporteur=apporteur,
            compagnie=compagnie,
            adherent=adherent,
            entreprise=entreprise,
            police=police,
            cotisation=formate_euros(contrat.cotisation_mensuelle_cents),
            soldes=soldes,
            soldes_appelees=formate_euros(soldes.cotisations_appelees_cents),
            soldes_encaissees=formate_euros(soldes.cotisations_encaissees_cents),
            soldes_prestations=formate_euros(soldes.prestations_reglees_cents),
            a_la_date=jour.isoformat(),
            etat=etat,
            etat_formule=etat_formule,
            etat_apporteur=etat_apporteur,
            etat_cotisation=etat_cotisation,
            erreur_etat=erreur_etat,
            perimetre=perimetre,
            actes_delegues=[LIBELLE_ACTE[a] for a in TOUS_LES_ACTES if a in perimetre.actes],
            actes_non_delegues=[LIBELLE_ACTE[a] for a in TOUS_LES_ACTES if a not in perimetre.actes],
            affiliation_deleguee=perimetre.autorise(ActeGestion.AFFILIATION),
            radiation_deleguee=perimetre.autorise(ActeGestion.RADIATION),
            prestations_deleguees=perimetre.autorise(ActeGestion.GESTION_PRESTATIONS),
            garanties=[
                garantie_sortie(garantie, couverture)
                for garantie, couverture in lecture.garanties_de_formule(session, contrat.formule_id)
            ],
            avenants=[
                avenant_sortie(avenant, code_formule, code_apporteur)
                for avenant, code_formule, code_apporteur in lecture.avenants_de_contrat(session, contrat.id)
            ],
            appels=appels,
            prestations=prestations,
            motifs=[m.value for m in MotifResiliation],
            message=message,
            message_type=message_type,
        ),
        code,
    )


@routeur.get("/contrats/nouveau", response_class=HTMLResponse)
def formulaire_contrat(
    requete: Request, session: SessionDep, numero_adherent: str | None = None
) -> HTMLResponse:
    """Formulaire de saisie d'un contrat."""
    return _rendu(
        requete,
        "contrat_nouveau.html",
        _contexte(
            "saisie",
            produits=list(session.scalars(select(Produit).order_by(Produit.code))),
            apporteurs=list(session.scalars(select(Apporteur).order_by(Apporteur.code))),
            codes_formule=list(session.scalars(select(Formule.code).distinct().order_by(Formule.code))),
            fractionnements=[f.value for f in Fractionnement],
            saisie={"numero_adherent": numero_adherent},
        ),
    )


@routeur.post("/contrats/nouveau", response_class=HTMLResponse)
def enregistre_contrat(
    requete: Request,
    session: SessionDep,
    numero_adherent: Annotated[str, Form()],
    code_produit: Annotated[str, Form()],
    code_formule: Annotated[str, Form()],
    code_apporteur: Annotated[str, Form()],
    date_souscription: Annotated[str, Form()],
    cotisation_euros: Annotated[str, Form()],
    fractionnement: Annotated[str, Form()],
) -> HTMLResponse:
    """Cree un devis depuis le formulaire, ou reaffiche le formulaire avec le motif du refus.

    La saisie est conservee en cas de refus : un gestionnaire ne doit pas retaper sept champs parce que
    le numero d'adherent comportait une faute.
    """
    saisie = {
        "numero_adherent": numero_adherent,
        "code_produit": code_produit,
        "code_formule": code_formule,
        "code_apporteur": code_apporteur,
        "date_souscription": date_souscription,
        "cotisation_euros": cotisation_euros,
        "fractionnement": fractionnement,
    }

    def refus(message: str) -> HTMLResponse:
        return _rendu(
            requete,
            "contrat_nouveau.html",
            _contexte(
                "saisie",
                produits=list(session.scalars(select(Produit).order_by(Produit.code))),
                apporteurs=list(session.scalars(select(Apporteur).order_by(Apporteur.code))),
                codes_formule=list(session.scalars(select(Formule.code).distinct().order_by(Formule.code))),
                fractionnements=[f.value for f in Fractionnement],
                saisie=saisie,
                message=message,
                message_type="refus",
            ),
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    try:
        cotisation_cents = euros_vers_centimes(cotisation_euros)
    except (ArithmeticError, ValueError):
        return refus(f"Montant de cotisation illisible : {cotisation_euros}")

    try:
        demande = service_contrats.DemandeSouscription(
            numero_adherent=numero_adherent.strip(),
            code_produit=code_produit,
            code_formule=code_formule,
            code_apporteur=code_apporteur,
            date_souscription=date.fromisoformat(date_souscription),
            cotisation_mensuelle_cents=cotisation_cents,
            fractionnement=Fractionnement(fractionnement),
        )
        contrat = service_contrats.souscrit_contrat(session, demande)
        session.commit()
    except service_contrats.ReferenceInvalide as erreur:
        session.rollback()
        return refus(str(erreur))
    except (ActeNonDelegue, DelegationAmbigue) as erreur:
        session.rollback()
        return refus(str(erreur))
    except ValueError as erreur:
        session.rollback()
        return refus(f"Saisie invalide : {erreur}")

    return RedirectResponse(
        url=f"/contrats/{contrat.numero_contrat}?cree=1", status_code=status.HTTP_303_SEE_OTHER
    )


@routeur.get("/contrats/{numero_contrat}", response_class=HTMLResponse)
def fiche_contrat(
    requete: Request,
    session: SessionDep,
    numero_contrat: str,
    a_la_date: str | None = None,
    cree: int | None = None,
) -> HTMLResponse:
    """Fiche contrat : garanties, avenants, flux, delegation et reconstitution a date."""
    message = "Devis cree. Il prendra effet apres mise en vigueur." if cree else None
    return _rend_fiche_contrat(requete, session, numero_contrat, a_la_date, message, "succes")


@routeur.post("/contrats/{numero_contrat}/mise-en-vigueur", response_class=HTMLResponse)
def met_en_vigueur(
    requete: Request,
    session: SessionDep,
    numero_contrat: str,
    date_effet: Annotated[str, Form()],
) -> HTMLResponse:
    """Met un devis en vigueur depuis la fiche contrat."""
    try:
        jour = date.fromisoformat(date_effet)
        contrat = service_contrats.contrat_par_numero(session, numero_contrat)
        service_contrats.met_en_vigueur(session, contrat, jour)
        session.commit()
    except (
        TransitionInterdite,
        ContratResilie,
        ActeNonDelegue,
        DelegationAmbigue,
        service_contrats.DateAvenantIncoherente,
        service_contrats.ContratIntrouvable,
        ValueError,
    ) as erreur:
        session.rollback()
        # 409 et non 200 : un refus metier qui repond « succes » est invisible pour la supervision,
        # pour les tests de bout en bout et pour tout automate. Seul le HTML disait la verite.
        return _rend_fiche_contrat(
            requete, session, numero_contrat, None, str(erreur), "refus", status.HTTP_409_CONFLICT
        )
    return _rend_fiche_contrat(
        requete, session, numero_contrat, None, f"Contrat mis en vigueur au {date_effet}.", "succes"
    )


@routeur.post("/contrats/{numero_contrat}/resiliation", response_class=HTMLResponse)
def resilie_contrat(
    requete: Request,
    session: SessionDep,
    numero_contrat: str,
    date_effet: Annotated[str, Form()],
    motif: Annotated[str, Form()],
) -> HTMLResponse:
    """Resilie un contrat depuis la fiche. Le motif est impose par le formulaire et par le domaine."""
    try:
        jour = date.fromisoformat(date_effet)
        motif_retenu = MotifResiliation(motif)
        contrat = service_contrats.contrat_par_numero(session, numero_contrat)
        service_contrats.resilie(session, contrat, jour, motif_retenu)
        session.commit()
    except (
        TransitionInterdite,
        ContratResilie,
        MotifResiliationManquant,
        ActeNonDelegue,
        DelegationAmbigue,
        service_contrats.DateAvenantIncoherente,
        service_contrats.ContratIntrouvable,
        ValueError,
    ) as erreur:
        session.rollback()
        return _rend_fiche_contrat(
            requete, session, numero_contrat, None, str(erreur), "refus", status.HTTP_409_CONFLICT
        )
    return _rend_fiche_contrat(
        requete,
        session,
        numero_contrat,
        None,
        f"Contrat resilie au {date_effet}, motif {motif}.",
        "succes",
    )


#: Statuts de rapprochement qui constituent une anomalie a traiter. `rapproche` n'en est pas une, et
#: `non_rapproche` est l'etat d'un mouvement que la chaine n'a pas encore examine.
STATUTS_EN_ECART = ("partiel", "ecart")


def _porteur_de_l_appel():
    """Expression du numero de porteur d'un appel, individuel ou groupe.

    Pourquoi cette fonction existe. Les requetes de cet ecran joignaient `contrat` sur
    `appel_cotisation.contrat_id`, en jointure interne. Un appel groupe adresse a une entreprise
    souscriptrice porte `police_id` et laisse `contrat_id` nul : la jointure l'excluait purement et
    simplement, et les ecarts des polices collectives, c'est-a-dire ceux qui portent les plus gros
    montants, n'apparaissaient nulle part. Les deux porteurs sont donc joints en externe, et le numero
    affiche est celui du porteur reellement renseigne.
    """
    return func.coalesce(Contrat.numero_contrat, PoliceCollective.numero_police)


def _joint_les_porteurs(requete):
    """Ajoute les deux jointures externes de porteur a une requete partant de `appel_cotisation`."""
    return requete.outerjoin(Contrat, Contrat.id == AppelCotisation.contrat_id).outerjoin(
        PoliceCollective, PoliceCollective.id == AppelCotisation.police_id
    )


@routeur.get("/anomalies", response_class=HTMLResponse)
def anomalies(requete: Request, session: SessionDep) -> HTMLResponse:
    """Ecran des anomalies de rapprochement, alimente par la chaine des flux financiers.

    Cet ecran lisait les appels au statut `impaye` du coeur ERP. C'etait un contresens : un appel
    impaye est un etat du quittancement, pas le resultat d'un rapprochement, et la specification
    demande que **les ecarts du rapprochement** alimentent cet ecran. L'exposure dbt
    `ecran_anomalies_erp` annoncait d'ailleurs les lots rejetes et les ecarts, que l'ecran n'affichait
    pas. Il lit desormais les trois tables que la chaine ecrit : `lot_fichier` pour les lots rejetes,
    `anomalie_lot` pour le detail du rapport renvoye a l'emetteur, et `rapprochement_ligne` pour le
    verdict de chaque mouvement bancaire.

    Trois sources de donnees, trois questions de gestionnaire, dans l'ordre ou il se les pose : quels
    fichiers ai-je refuses et pourquoi, quels appels ne sont pas soldes, et quels virements ne se
    rattachent a rien.
    """
    lots_rejetes = [
        {
            "reference": reference,
            "fichier": nom_fichier,
            "flux": libelle(type_flux),
            "compagnie": compagnie or "sans émetteur identifié",
            "periode": periode.strftime("%m/%Y"),
            "motif": motif or "motif non consigné",
            "lignes": lignes,
            "anomalies": anomalies_bloquantes,
        }
        for (
            reference, nom_fichier, type_flux, compagnie, periode, motif, lignes, anomalies_bloquantes
        ) in session.execute(
            select(
                LotFichier.reference_lot,
                LotFichier.nom_fichier,
                LotFichier.type_flux,
                Compagnie.raison_sociale,
                LotFichier.periode,
                LotFichier.motif_rejet,
                LotFichier.nombre_lignes_fichier,
                func.count(AnomalieLot.id),
            )
            .outerjoin(Compagnie, Compagnie.id == LotFichier.compagnie_id)
            .outerjoin(
                AnomalieLot,
                (AnomalieLot.lot_id == LotFichier.id)
                & (AnomalieLot.severite == SeveriteAnomalie.BLOQUANT),
            )
            .where(LotFichier.statut == StatutLot.REJETE)
            .group_by(
                LotFichier.id,
                LotFichier.reference_lot,
                LotFichier.nom_fichier,
                LotFichier.type_flux,
                Compagnie.raison_sociale,
                LotFichier.periode,
                LotFichier.motif_rejet,
                LotFichier.nombre_lignes_fichier,
            )
            .order_by(desc(LotFichier.periode), LotFichier.reference_lot)
            .limit(LIGNES_ANOMALIES)
        ).all()
    ]

    ecarts = [
        {
            "reference_bancaire": reference_bancaire,
            "reference_appel": reference_appel or "appel non identifié",
            "porteur": porteur,
            # Seul un contrat individuel a une fiche dans l'interface. Une police collective se
            # consulte par l'entreprise souscriptrice, et un lien vers `/contrats/<numero de police>`
            # tomberait sur une page introuvable.
            "porteur_consultable": est_contrat is not None,
            "echeance": echeance.strftime("%d/%m/%Y") if echeance else "",
            "statut": statut,
            "methode": libelle(methode),
            "appele": formate_euros(int(appele or 0)),
            "encaisse": formate_euros(int(encaisse or 0)),
            "ecart": formate_euros(int(ecart or 0)),
        }
        for (
            reference_bancaire, reference_appel, porteur, echeance, statut, methode, appele, encaisse,
            ecart, est_contrat,
        ) in session.execute(
            _joint_les_porteurs(
                select(
                    RapprochementLigne.reference_bancaire,
                    AppelCotisation.reference_appel,
                    _porteur_de_l_appel(),
                    AppelCotisation.date_echeance,
                    RapprochementLigne.statut,
                    RapprochementLigne.methode,
                    RapprochementLigne.montant_appele_cents,
                    RapprochementLigne.montant_encaisse_cents,
                    RapprochementLigne.ecart_cents,
                    AppelCotisation.contrat_id,
                ).outerjoin(AppelCotisation, AppelCotisation.id == RapprochementLigne.appel_id)
            )
            .where(RapprochementLigne.statut.in_(STATUTS_EN_ECART))
            # Valeur absolue, et c'est la correction d'un defaut de lecture : l'ecart est signe, un
            # reglement partiel le porte negatif. Trier sur l'ecart signe remontait les trop-percus en
            # tete et repoussait les plus gros manques hors des quarante lignes affichees, alors que la
            # tuile du montant en ecart, elle, somme des valeurs absolues. Les lignes ne disaient plus
            # la meme chose que le total au-dessus d'elles.
            .order_by(desc(func.abs(RapprochementLigne.ecart_cents)))
            .limit(LIGNES_ANOMALIES)
        ).all()
    ]

    non_trouves = [
        {
            "reference_bancaire": reference_bancaire,
            "date": date_encaissement.strftime("%d/%m/%Y") if date_encaissement else "",
            "montant": formate_euros(int(montant or 0)),
            "libelle_bancaire": libelle_bancaire or "sans libellé",
        }
        for reference_bancaire, date_encaissement, montant, libelle_bancaire in session.execute(
            select(
                RapprochementLigne.reference_bancaire,
                Encaissement.date_encaissement,
                RapprochementLigne.montant_encaisse_cents,
                Encaissement.libelle_releve,
            )
            .outerjoin(Encaissement, Encaissement.id == RapprochementLigne.encaissement_id)
            .where(RapprochementLigne.statut == "non_trouve")
            .order_by(desc(RapprochementLigne.montant_encaisse_cents))
            .limit(LIGNES_ANOMALIES)
        ).all()
    ]

    # Produits dont l'encaissement n'est pas delegue : leurs appels ne sont pas des impayes.
    produits_sans_encaissement = set(
        session.scalars(
            select(Produit.id)
            .join(ConventionProduit, ConventionProduit.produit_id == Produit.id)
            .join(ConventionDelegation, ConventionDelegation.id == ConventionProduit.convention_id)
            .where(
                ~select(ActeDelegue.id)
                .where(
                    ActeDelegue.convention_id == ConventionDelegation.id,
                    ActeDelegue.acte == ActeGestion.ENCAISSEMENT,
                )
                .exists()
            )
        )
    )
    sans_encaissement = [
        {
            "produit": f"{code}, {libelle_produit}",
            "compagnie": compagnie,
            "appels": appels,
            "montant": formate_euros(int(montant or 0)),
        }
        for code, libelle_produit, compagnie, appels, montant in session.execute(
            select(
                Produit.code,
                Produit.libelle,
                Compagnie.raison_sociale,
                func.count(AppelCotisation.id),
                func.coalesce(func.sum(AppelCotisation.montant_total_cents), 0),
            )
            # La requete part de l'appel et remonte vers son produit, et non l'inverse. Partir du
            # produit pour descendre vers ses contrats **et** ses polices croiserait les deux, et
            # chaque appel individuel serait compte autant de fois qu'il y a de polices sur le
            # produit. Un appel a un seul porteur : une ligne, un produit, un comptage.
            .select_from(AppelCotisation)
            .outerjoin(Contrat, Contrat.id == AppelCotisation.contrat_id)
            .outerjoin(PoliceCollective, PoliceCollective.id == AppelCotisation.police_id)
            .join(
                Produit,
                Produit.id == func.coalesce(Contrat.produit_id, PoliceCollective.produit_id),
            )
            .join(Compagnie, Compagnie.id == Produit.compagnie_id)
            .where(Produit.id.in_(produits_sans_encaissement))
            .group_by(Produit.code, Produit.libelle, Compagnie.raison_sociale)
            .order_by(Produit.code)
        ).all()
    ]

    total_lots_rejetes = (
        session.scalar(
            select(func.count(LotFichier.id)).where(LotFichier.statut == StatutLot.REJETE)
        )
        or 0
    )
    total_ecarts = (
        session.scalar(
            select(func.count(RapprochementLigne.id)).where(
                RapprochementLigne.statut.in_(STATUTS_EN_ECART)
            )
        )
        or 0
    )
    total_non_trouves = (
        session.scalar(
            select(func.count(RapprochementLigne.id)).where(
                RapprochementLigne.statut == "non_trouve"
            )
        )
        or 0
    )
    montant_ecart = (
        session.scalar(
            select(func.coalesce(func.sum(func.abs(RapprochementLigne.ecart_cents)), 0)).where(
                RapprochementLigne.statut.in_(STATUTS_EN_ECART)
            )
        )
        or 0
    )
    chaine_jamais_executee = (session.scalar(select(func.count(LotFichier.id))) or 0) == 0

    return _rendu(
        requete,
        "anomalies.html",
        _contexte(
            "anomalies",
            lots_rejetes=lots_rejetes,
            ecarts=ecarts,
            non_trouves=non_trouves,
            sans_encaissement=sans_encaissement,
            total_lots_rejetes=total_lots_rejetes,
            total_ecarts=total_ecarts,
            total_non_trouves=total_non_trouves,
            montant_ecart=formate_euros(int(montant_ecart)),
            chaine_jamais_executee=chaine_jamais_executee,
        ),
    )


@dataclass(frozen=True, slots=True)
class _LigneDossier:
    dossier: DossierPreRempli
    champs: dict
    controles: list
    numero_adherent: str | None
    numero_contrat: str | None
    bloquant: bool


def _rend_dossiers(
    requete: Request, session: Session, message: str | None = None, message_type: str = "succes"
) -> HTMLResponse:
    lignes = []
    # Une file d'attente se traite du plus ancien au plus recent ; les dossiers decides passent apres.
    dossiers = session.scalars(
        select(DossierPreRempli).order_by(DossierPreRempli.statut != StatutDossier.A_VALIDER, DossierPreRempli.id)
    ).all()
    for dossier in dossiers:
        adherent = session.get(Adherent, dossier.adherent_id) if dossier.adherent_id else None
        contrat = session.get(Contrat, dossier.contrat_id) if dossier.contrat_id else None
        controles = traitement.controles_du_dossier(dossier)
        lignes.append(
            _LigneDossier(
                dossier=dossier,
                champs=json.loads(dossier.extraction),
                controles=controles,
                numero_adherent=adherent.numero_adherent if adherent else None,
                numero_contrat=contrat.numero_contrat if contrat else None,
                bloquant=any(c.niveau == "bloquant" for c in controles),
            )
        )
    a_valider = sum(1 for ligne in lignes if ligne.dossier.statut is StatutDossier.A_VALIDER)
    return _rendu(
        requete,
        "validation_ia.html",
        _contexte("validation", lignes=lignes, a_valider=a_valider, message=message, message_type=message_type),
    )


@routeur.get("/dossiers-a-valider", response_class=HTMLResponse)
def dossiers_a_valider(requete: Request, session: SessionDep) -> HTMLResponse:
    """Ecran de validation humaine des dossiers pre-remplis par l'agent (module 4a)."""
    return _rend_dossiers(requete, session)


@routeur.post("/dossiers-a-valider/{dossier_id}/decision", response_class=HTMLResponse)
def decide_dossier(
    requete: Request,
    session: SessionDep,
    dossier_id: int,
    decision: Annotated[str, Form()],
    motif: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Decision du gestionnaire. Valider un remboursement cree la prestation ; rien ne s'ecrit avant."""
    dossier = session.get(DossierPreRempli, dossier_id)
    if dossier is None:
        return _page_introuvable(requete, f"Aucun dossier ne porte le numero {dossier_id}.")
    try:
        if decision == "valider":
            traitement.valide_dossier(session, dossier)
            message = f"Dossier {dossier.nom_fichier} validé."
            if dossier.prestation_id:
                prestation = session.get(Prestation, dossier.prestation_id)
                message += f" Prestation {prestation.reference_prestation} enregistrée."
        else:
            traitement.rejette_dossier(session, dossier, motif)
            message = f"Dossier {dossier.nom_fichier} rejeté."
        session.commit()
    except traitement.DossierNonValidable as refus:
        session.rollback()
        return _rend_dossiers(requete, session, f"Décision refusée : {refus}", "refus")
    return _rend_dossiers(requete, session, message)


@routeur.get("/reseau", response_class=HTMLResponse)
def reseau(requete: Request, session: SessionDep) -> HTMLResponse:
    """Reseau d'apporteurs et conventions de delegation."""
    from courtage.api.routes.reseau import _apporteur_sortie

    return _rendu(
        requete,
        "reseau.html",
        _contexte(
            "reseau",
            apporteurs=[_apporteur_sortie(*ligne) for ligne in lecture.liste_apporteurs(session)],
            conventions=_conventions(session),
        ),
    )


