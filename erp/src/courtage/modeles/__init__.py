"""Entites de l'ERP.

Ce module importe toutes les entites pour que `Base.metadata` soit complet des que l'on importe
`courtage.modeles`. Alembic et les tests en dependent : une entite oubliee ici serait absente des
migrations generees.
"""

from courtage.modeles.base import Base
from courtage.modeles.collectif import LigneAppelCotisation, PoliceCollective
from courtage.modeles.delegation import ActeDelegue, ConventionDelegation, ConventionProduit
from courtage.modeles.dossier import DossierPreRempli, StatutDossier
from courtage.modeles.financier import (
    AppelCotisation,
    Bordereau,
    Commission,
    Encaissement,
    Prestation,
    Retrocession,
)
from courtage.modeles.portefeuille import Adherent, Avenant, Contrat, EntrepriseSouscriptrice
from courtage.modeles.referentiel import (
    Apporteur,
    Branche,
    Compagnie,
    Formule,
    FormuleGarantie,
    Garantie,
    Produit,
    TauxCommission,
    TauxRetrocession,
    TauxTaxe,
)
from courtage.modeles.scoring import ScoreResiliation

__all__ = [
    "ActeDelegue",
    "Adherent",
    "AppelCotisation",
    "Apporteur",
    "Avenant",
    "Base",
    "Bordereau",
    "Branche",
    "Commission",
    "Compagnie",
    "Contrat",
    "ConventionDelegation",
    "ConventionProduit",
    "DossierPreRempli",
    "Encaissement",
    "EntrepriseSouscriptrice",
    "Formule",
    "FormuleGarantie",
    "Garantie",
    "LigneAppelCotisation",
    "PoliceCollective",
    "Prestation",
    "Produit",
    "Retrocession",
    "ScoreResiliation",
    "StatutDossier",
    "TauxCommission",
    "TauxRetrocession",
    "TauxTaxe",
]
