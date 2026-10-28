"""Dossier pre-rempli par l'agent de lecture des documents recus (module 4a).

Le dossier est la frontiere entre le modele de langage et le portefeuille. L'agent ecrit ici, et nulle
part ailleurs : un dossier nait toujours au statut `a_valider`, et seule une decision humaine prise dans
l'ecran de validation le fait passer a `valide`, ce qui declenche alors l'ecriture definitive (une
prestation, par le service qui applique deja la delegation, la garantie a date et le plafond).

L'extraction et les controles sont conserves tels quels, en JSON : ce que le gestionnaire a vu au moment
de decider doit rester relisible, y compris si le modele ou les regles changent ensuite.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from courtage.modeles.base import Base, cle_primaire
from courtage.modeles.enums import colonne_enum


class StatutDossier(StrEnum):
    A_VALIDER = "a_valider"
    VALIDE = "valide"
    REJETE = "rejete"


class DossierPreRempli(Base):
    __tablename__ = "dossier_pre_rempli"
    __table_args__ = (
        CheckConstraint(
            "(statut = 'rejete' AND motif_rejet IS NOT NULL) OR (statut <> 'rejete' AND motif_rejet IS NULL)",
            name="rejet_motive",
        ),
        # Une prestation n'existe qu'apres une validation humaine.
        CheckConstraint("prestation_id IS NULL OR statut = 'valide'", name="prestation_apres_validation"),
    )

    id: Mapped[int] = cle_primaire()
    nom_fichier: Mapped[str] = mapped_column(String(200), unique=True)
    type_document: Mapped[str | None] = mapped_column(String(40))
    statut: Mapped[StatutDossier] = mapped_column(colonne_enum(StatutDossier, "statut_dossier"), index=True)
    adherent_id: Mapped[int | None] = mapped_column(ForeignKey("adherent.id"), index=True)
    contrat_id: Mapped[int | None] = mapped_column(ForeignKey("contrat.id"), index=True)
    prestation_id: Mapped[int | None] = mapped_column(ForeignKey("prestation.id"))
    #: Champs rendus par le modele, apres validation de leur forme.
    extraction: Mapped[str] = mapped_column(Text)
    #: Liste des controles metier : code, niveau (ok, alerte, bloquant) et message.
    controles: Mapped[str] = mapped_column(Text)
    similarite_nom: Mapped[float | None] = mapped_column(Float)
    montant_total_cents: Mapped[int | None] = mapped_column(BigInteger)
    modele: Mapped[str] = mapped_column(String(60))
    jetons_entree: Mapped[int] = mapped_column(Integer)
    jetons_sortie: Mapped[int] = mapped_column(Integer)
    cout_microdollars: Mapped[int] = mapped_column(Integer)
    latence_ms: Mapped[int] = mapped_column(Integer)
    cree_le: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())
    decide_le: Mapped[datetime | None] = mapped_column(DateTime)
    motif_rejet: Mapped[str | None] = mapped_column(String(300))
