"""Transformation des lignes de base vers les schemas de sortie.

Un seul endroit pour cette traduction, partage par les routes et par les vues de l'interface. Le but est
qu'un contrat soit presente identiquement partout, montant formate compris.
"""

from __future__ import annotations

from courtage.api.schemas import AvenantSortie, ContratResume, GarantieContrat, PerimetreSortie
from courtage.domaine.delegation import LIBELLE_ACTE, PerimetreDelegue
from courtage.domaine.montants import formate_euros, formate_pourcent


def contrat_resume_depuis_lignes(contrat, produit, branche, formule, apporteur) -> ContratResume:
    """Construit le resume d'un contrat a partir des entites jointes."""
    return ContratResume(
        numero_contrat=contrat.numero_contrat,
        statut=contrat.statut,
        code_produit=produit.code,
        libelle_produit=produit.libelle,
        branche=branche.libelle,
        code_formule=formule.code,
        code_apporteur=apporteur.code,
        date_effet=contrat.date_effet,
        date_resiliation=contrat.date_resiliation,
        motif_resiliation=contrat.motif_resiliation,
        cotisation_mensuelle_cents=contrat.cotisation_mensuelle_cents,
    ).with_affichage()


def garantie_sortie(garantie, couverture) -> GarantieContrat:
    return GarantieContrat(
        code=garantie.code,
        libelle=garantie.libelle,
        famille=garantie.famille,
        libelle_couverture=couverture.libelle_couverture,
        taux_remboursement=formate_pourcent(couverture.taux_remboursement_bp),
        plafond_annuel_cents=couverture.plafond_annuel_cents,
        plafond_annuel_affiche=(
            formate_euros(couverture.plafond_annuel_cents)
            if couverture.plafond_annuel_cents is not None
            else None
        ),
    )


def avenant_sortie(avenant, code_formule: str, code_apporteur: str) -> AvenantSortie:
    return AvenantSortie(
        numero_ordre=avenant.numero_ordre,
        type_avenant=avenant.type_avenant,
        date_effet=avenant.date_effet,
        date_saisie=avenant.date_saisie,
        statut=avenant.statut,
        code_formule=code_formule,
        code_apporteur=code_apporteur,
        cotisation_mensuelle_cents=avenant.cotisation_mensuelle_cents,
        cotisation_mensuelle_affichee=formate_euros(avenant.cotisation_mensuelle_cents),
        fractionnement=avenant.fractionnement,
        motif_resiliation=avenant.motif_resiliation,
        commentaire=avenant.commentaire,
    )


def perimetre_sortie(code_produit: str, perimetre: PerimetreDelegue) -> PerimetreSortie:
    """Perimetre delegue, avec les actes tries pour que la sortie soit stable d'un appel a l'autre."""
    return PerimetreSortie(
        produit=code_produit,
        convention_reference=perimetre.convention_reference,
        portee_declaree=perimetre.portee_declaree,
        actes_delegues=sorted(LIBELLE_ACTE[acte] for acte in perimetre.actes),
    )
