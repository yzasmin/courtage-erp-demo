"""Delegation de gestion : conventions entre une compagnie et le cabinet, et actes delegues.

Pourquoi cette table existe, et pourquoi elle est au coeur du projet plutot qu'en annexe.

Un courtier **delegataire** accomplit, au nom de la compagnie, des actes qui appartiennent normalement a
l'assureur : emettre les appels de cotisation, encaisser, affilier et radier des adherents, regler les
prestations, parfois instruire les sinistres. Ce perimetre n'est pas un reglage d'application : il est
fixe par une convention de delegation, signee produit par produit, avec une date de debut et parfois une
date de fin. Une compagnie peut deleguer le quittancement et l'encaissement sans deleguer les
prestations, et le logiciel doit alors refuser d'enregistrer une prestation sur ce produit.

Trois tables, parce que trois choses differentes sont en jeu :

- `convention_delegation` : l'accord lui-meme, avec la compagnie, ses dates de validite et sa portee
  declaree.
- `convention_produit` : les produits couverts. Une convention peut en couvrir plusieurs, et un produit
  ne doit pas etre couvert par deux conventions en vigueur a la meme date, sans quoi le perimetre devient
  ambigu. Cette unicite dans le temps est verifiee par le code, pas par une contrainte : une base
  relationnelle ne sait pas exprimer « pas deux periodes qui se chevauchent » de facon portable.
- `acte_delegue` : les actes effectivement delegues, un par ligne.

La portee declaree (« totale » ou « partielle ») est un libelle de convention, utile a lire sur un ecran.
**Elle n'est jamais opposee a un acte.** Ce qui autorise un acte, c'est la presence de la ligne
correspondante dans `acte_delegue`. Une convention saisie « totale » mais sans ligne pour les prestations
ne permet donc pas de regler une prestation : le libelle ne peut pas elargir le perimetre reel.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from courtage.modeles.base import Base, cle_primaire
from courtage.modeles.enums import ActeGestion, PorteeDelegation, colonne_enum
from courtage.modeles.referentiel import Compagnie, Produit


class ConventionDelegation(Base):
    """Convention de delegation de gestion conclue avec une compagnie."""

    __tablename__ = "convention_delegation"

    id: Mapped[int] = cle_primaire()
    reference: Mapped[str] = mapped_column(String(40), unique=True)
    compagnie_id: Mapped[int] = mapped_column(ForeignKey("compagnie.id"), index=True)
    portee: Mapped[PorteeDelegation] = mapped_column(colonne_enum(PorteeDelegation, "portee_delegation"))
    date_debut_validite: Mapped[date] = mapped_column(Date)
    date_fin_validite: Mapped[date | None] = mapped_column(Date, nullable=True)
    commentaire: Mapped[str | None] = mapped_column(Text, nullable=True)

    compagnie: Mapped[Compagnie] = relationship()
    produits: Mapped[list[ConventionProduit]] = relationship(
        back_populates="convention", cascade="all, delete-orphan"
    )
    actes: Mapped[list[ActeDelegue]] = relationship(back_populates="convention", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint(
            "date_fin_validite IS NULL OR date_fin_validite >= date_debut_validite",
            name="validite_convention_coherente",
        ),
        Index("ix_convention_delegation_compagnie_debut", "compagnie_id", "date_debut_validite"),
    )


class ConventionProduit(Base):
    """Produit couvert par une convention de delegation."""

    __tablename__ = "convention_produit"

    id: Mapped[int] = cle_primaire()
    convention_id: Mapped[int] = mapped_column(
        ForeignKey("convention_delegation.id", ondelete="CASCADE"), index=True
    )
    produit_id: Mapped[int] = mapped_column(ForeignKey("produit.id"), index=True)

    convention: Mapped[ConventionDelegation] = relationship(back_populates="produits")
    produit: Mapped[Produit] = relationship()

    __table_args__ = (UniqueConstraint("convention_id", "produit_id", name="uq_convention_produit"),)


class ActeDelegue(Base):
    """Acte de gestion delegue par la convention.

    La presence de la ligne vaut autorisation. Son absence vaut refus. Il n'y a pas de colonne
    « autorise » booleenne, parce qu'une ligne a `false` et une ligne absente auraient alors le meme
    sens, et deux facons de dire la meme chose finissent toujours par divergir.
    """

    __tablename__ = "acte_delegue"

    id: Mapped[int] = cle_primaire()
    convention_id: Mapped[int] = mapped_column(
        ForeignKey("convention_delegation.id", ondelete="CASCADE"), index=True
    )
    acte: Mapped[ActeGestion] = mapped_column(colonne_enum(ActeGestion, "acte_gestion"))

    convention: Mapped[ConventionDelegation] = relationship(back_populates="actes")

    __table_args__ = (UniqueConstraint("convention_id", "acte", name="uq_acte_delegue"),)
