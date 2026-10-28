"""Application : API documentee et interface de gestionnaire, servies par le meme processus.

La traduction des refus metier en codes HTTP est centralisee ici, par des gestionnaires d'exception.
Deux consequences utiles : les routes restent courtes et ne se repetent pas, et le meme refus produit
toujours le meme code et le meme format de reponse, quelle que soit la route qui l'a declenche.

Le choix des codes :

| Exception du domaine | Code | Pourquoi |
| --- | --- | --- |
| `ContratIntrouvable`, `EtatIntrouvable` | 404 | la ressource demandee n'existe pas, ou pas a cette date |
| `ActeNonDelegue` | 409 | la demande est bien formee, mais l'etat du systeme l'interdit |
| `TransitionInterdite`, `ContratResilie` | 409 | meme raison : conflit avec l'etat actuel du contrat |
| `DateAvenantIncoherente` | 409 | conflit avec l'historique deja enregistre |
| `DelegationAmbigue`, `TauxIntrouvable` | 409 | le parametrage ne permet pas de conclure, il faut le corriger |
| `MotifResiliationManquant`, `ReferenceInvalide` | 422 | la demande est syntaxiquement valide mais metier invalide |
| `ContratSansGarantie` | 409 | le contrat n'etait pas en garantie a la date des soins |
| `GarantieNonCouverte` | 422 | la formule en vigueur ne couvre pas la garantie invoquee |

Le 409 plutot que le 400 pour la delegation est deliberé : la requete n'a rien d'incorrect, c'est la
convention en vigueur qui s'y oppose. Un client qui recoit 400 corrige sa requete, un client qui recoit
409 va voir l'etat du systeme, ce qui est la bonne reaction.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError as ErreurValidationRequete
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as ExceptionHTTPStarlette

from courtage.api.routes import adherents, contrats, reseau
from courtage.config import parametres
from courtage.domaine.avenants import EtatIntrouvable
from courtage.domaine.cycle_vie import ContratResilie, MotifResiliationManquant, TransitionInterdite
from courtage.domaine.delegation import ActeNonDelegue, DelegationAmbigue
from courtage.domaine.montants import TauxIntrouvable
from courtage.interface import vues
from courtage.services.contrats import ContratIntrouvable, DateAvenantIncoherente, ReferenceInvalide
from courtage.services.prestations import ContratSansGarantie, GarantieNonCouverte, ReferenceDejaAttribuee

DESCRIPTION = """
API du mini-ERP de courtage sante et prevoyance.

Le cabinet modelise est un **courtier grossiste delegataire** : les compagnies portent le risque, le
cabinet place les contrats, et accomplit au nom des compagnies les actes de gestion qu'elles lui ont
delegues par convention. Toutes les donnees sont synthetiques.

### Ce qu'il faut savoir avant d'appeler

- **Les montants sont en centimes d'euro**, dans les champs suffixes `_cents`. Chaque montant est double
  d'un champ formate, destine a l'affichage.
- **Les taux sont en points de base** dans la base, et formates en pourcentage dans les reponses.
  10 000 points de base valent 100 pour cent.
- **Un contrat nait a l'etat de devis.** Il ne porte pas de date d'effet et ne se quittance pas tant
  qu'il n'est pas mis en vigueur.
- **Une resiliation exige un motif.** La regle est portee par l'API et par une contrainte en base.
- **Un acte non delegue est refuse**, avec un code 409 et le rappel du perimetre reellement delegue.

### Piste d'audit

Aucun changement d'etat d'un contrat n'est enregistre sans l'avenant qui le porte, dans la meme
transaction. L'endpoint `GET /api/v1/contrats/{numero}/etat` reconstitue l'etat a n'importe quelle date
et indique de quel avenant provient la valeur retournee.
"""

ETIQUETTES = [
    {"name": "adherents", "description": "Personnes couvertes et leur rattachement a une entreprise."},
    {"name": "contrats", "description": "Portefeuille, cycle de vie, avenants et reconstitution a date."},
    {
        "name": "reseau et delegation",
        "description": "Apporteurs, production, et perimetre delegue par chaque compagnie.",
    },
    {"name": "interface", "description": "Ecrans de gestion, rendus en HTML."},
    {"name": "exploitation", "description": "Etat de service, pour la supervision et le compose."},
]


def _refus(code_http: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=code_http, content={"erreur": {"code": code, "message": message}})


def cree_application() -> FastAPI:
    application = FastAPI(
        title=parametres.titre_application,
        description=DESCRIPTION,
        version="0.1.0",
        openapi_tags=ETIQUETTES,
        docs_url="/api/doc",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    application.include_router(adherents.routeur)
    application.include_router(contrats.routeur)
    application.include_router(reseau.routeur)
    application.include_router(vues.routeur)

    from courtage.interface import DOSSIER_STATIQUES

    application.mount("/statiques", StaticFiles(directory=DOSSIER_STATIQUES), name="statiques")

    @application.get("/sante", tags=["exploitation"], summary="Etat de service")
    def sante() -> dict[str, str]:
        """Repond toujours 200 quand le processus sert. Utilise par le `healthcheck` du compose."""
        return {"etat": "ok", "application": parametres.titre_application, "version": "0.1.0"}

    # --- Format unique de toutes les reponses d'erreur -------------------------------------------
    # Sans ces deux gestionnaires, trois formats cohabiteraient : `{"erreur": ...}` pour les refus
    # metier, `{"detail": ...}` pour les `HTTPException`, et le format de validation de FastAPI pour
    # les corps mal formes. Un client devrait alors savoir lequel attendre selon la route appelee.
    # Un test de l'API verifie que toute reponse d'erreur porte bien la cle `erreur`.

    @application.exception_handler(ExceptionHTTPStarlette)
    def _erreur_http(_: Request, erreur: ExceptionHTTPStarlette) -> JSONResponse:
        detail = erreur.detail
        if isinstance(detail, dict) and "code" in detail:
            return _refus(erreur.status_code, str(detail["code"]), str(detail.get("message", "")))
        return _refus(erreur.status_code, f"http_{erreur.status_code}", str(detail))

    @application.exception_handler(ErreurValidationRequete)
    def _requete_invalide(_: Request, erreur: ErreurValidationRequete) -> JSONResponse:
        champs = []
        for anomalie in erreur.errors():
            chemin = ".".join(str(partie) for partie in anomalie.get("loc", ()) if partie != "body")
            champs.append(f"{chemin or 'corps'} : {anomalie.get('msg', '')}")
        return _refus(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "requete_invalide",
            "; ".join(champs) or "corps de requete invalide",
        )

    # --- Traduction des refus metier en reponses HTTP --------------------------------------------

    @application.exception_handler(ContratIntrouvable)
    def _contrat_introuvable(_: Request, erreur: ContratIntrouvable) -> JSONResponse:
        return _refus(status.HTTP_404_NOT_FOUND, "contrat_introuvable", str(erreur))

    @application.exception_handler(EtatIntrouvable)
    def _etat_introuvable(_: Request, erreur: EtatIntrouvable) -> JSONResponse:
        return _refus(status.HTTP_404_NOT_FOUND, "etat_introuvable", str(erreur))

    @application.exception_handler(ActeNonDelegue)
    def _acte_non_delegue(_: Request, erreur: ActeNonDelegue) -> JSONResponse:
        return _refus(status.HTTP_409_CONFLICT, "acte_non_delegue", str(erreur))

    @application.exception_handler(DelegationAmbigue)
    def _delegation_ambigue(_: Request, erreur: DelegationAmbigue) -> JSONResponse:
        return _refus(status.HTTP_409_CONFLICT, "delegation_ambigue", str(erreur))

    @application.exception_handler(TransitionInterdite)
    def _transition_interdite(_: Request, erreur: TransitionInterdite) -> JSONResponse:
        return _refus(status.HTTP_409_CONFLICT, "transition_interdite", str(erreur))

    @application.exception_handler(ContratResilie)
    def _contrat_resilie(_: Request, erreur: ContratResilie) -> JSONResponse:
        return _refus(status.HTTP_409_CONFLICT, "contrat_resilie", str(erreur))

    @application.exception_handler(DateAvenantIncoherente)
    def _date_incoherente(_: Request, erreur: DateAvenantIncoherente) -> JSONResponse:
        return _refus(status.HTTP_409_CONFLICT, "date_avenant_incoherente", str(erreur))

    @application.exception_handler(TauxIntrouvable)
    def _taux_introuvable(_: Request, erreur: TauxIntrouvable) -> JSONResponse:
        return _refus(status.HTTP_409_CONFLICT, "taux_introuvable", str(erreur))

    @application.exception_handler(MotifResiliationManquant)
    def _motif_manquant(_: Request, erreur: MotifResiliationManquant) -> JSONResponse:
        return _refus(status.HTTP_422_UNPROCESSABLE_ENTITY, "motif_resiliation_manquant", str(erreur))

    @application.exception_handler(ContratSansGarantie)
    def _contrat_sans_garantie(_: Request, erreur: ContratSansGarantie) -> JSONResponse:
        return _refus(status.HTTP_409_CONFLICT, 'contrat_sans_garantie', str(erreur))

    @application.exception_handler(GarantieNonCouverte)
    def _garantie_non_couverte(_: Request, erreur: GarantieNonCouverte) -> JSONResponse:
        return _refus(status.HTTP_422_UNPROCESSABLE_ENTITY, 'garantie_non_couverte', str(erreur))

    @application.exception_handler(ReferenceDejaAttribuee)
    def _reference_deja_attribuee(_: Request, erreur: ReferenceDejaAttribuee) -> JSONResponse:
        return _refus(status.HTTP_409_CONFLICT, "reference_deja_attribuee", str(erreur))

    @application.exception_handler(ReferenceInvalide)
    def _reference_invalide(_: Request, erreur: ReferenceInvalide) -> JSONResponse:
        return _refus(status.HTTP_422_UNPROCESSABLE_ENTITY, "reference_invalide", str(erreur))

    return application


application = cree_application()
