"""Schemas d'entree et de sortie de l'API.

Les montants circulent en **centimes**, sous le nom `..._cents`, et jamais en euros decimaux. Un nombre a
virgule dans un format d'echange finit toujours par etre arrondi par un client, et le centime perdu
reapparait dans un bordereau. Les schemas exposent en plus un champ formate, destine a l'affichage, pour
qu'une interface n'ait pas a reimplementer la typographie francaise des montants.

Les libelles de champ sont en francais, comme le reste du projet : la documentation OpenAPI est lue par
les memes personnes que les ecrans.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from courtage.domaine.montants import formate_euros, formate_pourcent
from courtage.modeles.enums import (
    Fractionnement,
    MotifResiliation,
    StatutContrat,
    TypeApporteur,
    TypeAvenant,
)


class Erreur(BaseModel):
    """Corps de reponse en cas de refus. Le code est stable, le message est destine a un humain."""

    code: str = Field(description="identifiant stable du refus, par exemple acte_non_delegue")
    message: str = Field(description="explication destinee au gestionnaire")


class CorpsErreur(BaseModel):
    erreur: Erreur


class Pagination(BaseModel):
    total: int
    page: int
    par_page: int
    pages: int


class AdherentResume(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    numero_adherent: str
    nom: str
    prenom: str
    date_naissance: date
    departement: str
    entreprise: str | None = None
    nombre_contrats: int = 0


class AdherentDetail(AdherentResume):
    sexe: str
    code_postal: str
    courriel: str | None
    telephone: str | None
    date_creation: date
    contrats: list[ContratResume] = Field(default_factory=list)


class ListeAdherents(BaseModel):
    donnees: list[AdherentResume]
    pagination: Pagination


class ContratResume(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    numero_contrat: str
    statut: StatutContrat
    code_produit: str
    libelle_produit: str
    branche: str
    code_formule: str
    code_apporteur: str
    date_effet: date | None
    date_resiliation: date | None
    motif_resiliation: MotifResiliation | None
    cotisation_mensuelle_cents: int
    cotisation_mensuelle_affichee: str = ""

    def with_affichage(self) -> ContratResume:
        self.cotisation_mensuelle_affichee = formate_euros(self.cotisation_mensuelle_cents)
        return self


class ListeContrats(BaseModel):
    donnees: list[ContratResume]
    pagination: Pagination


class GarantieContrat(BaseModel):
    code: str
    libelle: str
    famille: str
    libelle_couverture: str
    taux_remboursement: str
    plafond_annuel_cents: int | None
    plafond_annuel_affiche: str | None


class AvenantSortie(BaseModel):
    numero_ordre: int
    type_avenant: TypeAvenant
    date_effet: date
    date_saisie: date
    statut: StatutContrat
    code_formule: str
    code_apporteur: str
    cotisation_mensuelle_cents: int
    cotisation_mensuelle_affichee: str
    fractionnement: Fractionnement
    motif_resiliation: MotifResiliation | None
    commentaire: str | None


class EtatContratSortie(BaseModel):
    """Reponse de l'endpoint « etat du contrat a la date D ».

    `avenant_numero_ordre` et `avenant_date_effet` disent **de quel acte** vient l'etat retourne. Sans
    cela, un gestionnaire qui conteste une valeur n'aurait aucun moyen de remonter a sa source.
    """

    numero_contrat: str
    a_la_date: date
    statut: StatutContrat
    est_en_portefeuille: bool
    #: La garantie court-elle ce jour-la. Faux pour un devis, un contrat suspendu ou resilie.
    garantie_en_cours: bool
    code_formule: str
    libelle_formule: str
    code_apporteur: str
    nom_apporteur: str
    cotisation_mensuelle_cents: int
    cotisation_mensuelle_affichee: str
    fractionnement: Fractionnement
    motif_resiliation: MotifResiliation | None
    avenant_numero_ordre: int
    avenant_type: str
    avenant_date_effet: date


class ContratDetail(ContratResume):
    adherent: AdherentResume
    entreprise: str | None
    compagnie: str
    date_souscription: date
    fractionnement: Fractionnement
    garanties: list[GarantieContrat] = Field(default_factory=list)
    avenants: list[AvenantSortie] = Field(default_factory=list)
    perimetre_delegue: PerimetreSortie | None = None
    cotisations_appelees_cents: int = 0
    cotisations_encaissees_cents: int = 0
    prestations_reglees_cents: int = 0


class PerimetreSortie(BaseModel):
    """Perimetre delegue sur le produit du contrat, a la date du jour.

    Expose par l'API pour que l'interface puisse masquer un bouton plutot que de laisser un gestionnaire
    remplir un formulaire qui sera refuse.
    """

    produit: str
    convention_reference: str | None
    portee_declaree: str | None
    actes_delegues: list[str]


class DemandeSouscriptionEntree(BaseModel):
    """Corps de la souscription d'un contrat."""

    numero_adherent: str = Field(description="numero de l'adherent, par exemple A000123")
    code_produit: str = Field(description="code du produit, par exemple SI-ESSENTIEL")
    code_formule: str = Field(description="code de la formule sur ce produit, par exemple F2")
    code_apporteur: str = Field(description="code de l'apporteur, par exemple AP01")
    date_souscription: date
    cotisation_mensuelle_cents: int = Field(ge=0, description="cotisation mensuelle en centimes d'euro")
    fractionnement: Fractionnement = Fractionnement.MENSUEL


class DemandeMiseEnVigueur(BaseModel):
    date_effet: date
    commentaire: str | None = None


class DemandeResiliation(BaseModel):
    date_effet: date
    motif: MotifResiliation = Field(description="motif obligatoire : aucune resiliation sans motif")
    commentaire: str | None = None


class DemandeAvenant(BaseModel):
    """Corps d'un avenant de modification. Seuls les champs fournis changent."""

    type_avenant: TypeAvenant
    date_effet: date
    code_formule: str | None = None
    code_apporteur: str | None = None
    cotisation_mensuelle_cents: int | None = Field(default=None, ge=0)
    fractionnement: Fractionnement | None = None
    commentaire: str | None = None


class DemandePrestationEntree(BaseModel):
    """Corps d'une demande de remboursement."""

    code_garantie: str = Field(description="code du poste de garantie, par exemple OPTIQUE")
    date_soins: date
    montant_demande_cents: int = Field(ge=0, description="montant des frais engages, en centimes")


class PrestationSortie(BaseModel):
    """Suite donnee a une demande de remboursement, avec de quoi l'expliquer a l'adherent."""

    reference_prestation: str
    numero_contrat: str
    code_garantie: str
    date_soins: date
    statut: str
    montant_demande_cents: int
    montant_demande_affiche: str
    montant_rembourse_cents: int
    montant_rembourse_affiche: str
    taux_remboursement: str
    plafond_annuel_cents: int | None
    plafond_annuel_affiche: str | None
    deja_rembourse_cents: int
    deja_rembourse_affiche: str
    motif_rejet: str | None


class ApporteurSortie(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    nom: str
    type_apporteur: TypeApporteur
    numero_orias: str | None
    departement: str
    date_entree: date
    date_sortie: date | None
    actif: bool
    contrats_en_portefeuille: int = 0
    cotisation_annualisee_cents: int = 0
    cotisation_annualisee_affichee: str = ""
    taux_retrocession_cadre: str | None = None


class ListeApporteurs(BaseModel):
    donnees: list[ApporteurSortie]


def formate_taux(taux_bp: int | None) -> str | None:
    return None if taux_bp is None else formate_pourcent(taux_bp)


# Les references differees entre schemas (adherent qui porte ses contrats, contrat qui porte son
# adherent) sont resolues apres la definition de tous les modeles.
AdherentDetail.model_rebuild()
ContratDetail.model_rebuild()
