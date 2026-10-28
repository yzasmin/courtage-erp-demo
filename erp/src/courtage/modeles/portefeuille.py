"""Portefeuille : entreprises souscriptrices, adherents, contrats et avenants.

Le choix de modelisation le plus important du projet est ici : **l'avenant porte l'historique, et il
porte un etat complet, non un ecart.** Chaque avenant enregistre ce que le contrat devient a partir de sa
date d'effet. Reconstituer le contrat au 14 mars 2025 revient donc a lire l'avenant dont la date d'effet
est la plus recente parmi celles anterieures ou egales a cette date. Aucun repli de deltas, aucune
seconde verite a maintenir.

Les colonnes de la table `contrat` qui dupliquent cet etat (formule, apporteur, cotisation, statut) sont
un cache de l'avenant courant. Un test compare systematiquement ce cache a la reconstitution a la date du
jour, pour qu'il ne puisse pas deriver en silence.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from courtage.modeles.base import Base, centimes, cle_primaire
from courtage.modeles.enums import (
    Fractionnement,
    MotifResiliation,
    StatutContrat,
    TypeAvenant,
    colonne_enum,
)
from courtage.modeles.referentiel import Apporteur, Formule, Produit


class EntrepriseSouscriptrice(Base):
    """Entreprise qui souscrit un contrat collectif pour ses salaries.

    En sante collective, l'entreprise est le souscripteur et le salarie est l'adherent. La distinction
    compte : la cotisation est appelee aupres de l'entreprise, les prestations sont servies au salarie.
    """

    __tablename__ = "entreprise_souscriptrice"

    id: Mapped[int] = cle_primaire()
    siren: Mapped[str] = mapped_column(String(9), unique=True)
    raison_sociale: Mapped[str] = mapped_column(String(200), index=True)
    code_naf: Mapped[str] = mapped_column(String(6))
    effectif: Mapped[int] = mapped_column()
    departement: Mapped[str] = mapped_column(String(3), index=True)
    date_adhesion: Mapped[date] = mapped_column(Date)

    adherents: Mapped[list[Adherent]] = relationship(back_populates="entreprise")

    __table_args__ = (
        CheckConstraint("length(siren) = 9", name="siren_9_chiffres"),
        CheckConstraint("effectif > 0", name="effectif_positif"),
    )


class Adherent(Base):
    """Personne physique couverte. Rattachee a une entreprise en collectif, autonome en individuel."""

    __tablename__ = "adherent"

    id: Mapped[int] = cle_primaire()
    numero_adherent: Mapped[str] = mapped_column(String(20), unique=True)
    nom: Mapped[str] = mapped_column(String(120), index=True)
    prenom: Mapped[str] = mapped_column(String(120))
    date_naissance: Mapped[date] = mapped_column(Date)
    # Donnee de tarification en prevoyance. Valeurs simulees : "F" ou "M".
    sexe: Mapped[str] = mapped_column(String(1))
    code_postal: Mapped[str] = mapped_column(String(5))
    departement: Mapped[str] = mapped_column(String(3), index=True)
    courriel: Mapped[str | None] = mapped_column(String(200), nullable=True)
    telephone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    entreprise_id: Mapped[int | None] = mapped_column(
        ForeignKey("entreprise_souscriptrice.id"), nullable=True, index=True
    )
    date_creation: Mapped[date] = mapped_column(Date)
    cree_le: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    entreprise: Mapped[EntrepriseSouscriptrice | None] = relationship(back_populates="adherents")
    contrats: Mapped[list[Contrat]] = relationship(back_populates="adherent")

    __table_args__ = (
        CheckConstraint("sexe IN ('F', 'M')", name="sexe_connu"),
        CheckConstraint("length(code_postal) = 5", name="code_postal_5_chiffres"),
        # Recherche par nom et prenom : c'est ainsi qu'un gestionnaire cherche un adherent au telephone.
        Index("ix_adherent_nom_prenom", "nom", "prenom"),
    )


class Contrat(Base):
    """Contrat place par le cabinet pour un adherent.

    Les colonnes d'etat courant (statut, formule, apporteur, cotisation, fractionnement) sont le reflet de
    l'avenant en vigueur aujourd'hui. Elles existent pour que la liste du portefeuille se lise en une
    requete, sans reconstituer 5 000 historiques.
    """

    __tablename__ = "contrat"

    id: Mapped[int] = cle_primaire()
    numero_contrat: Mapped[str] = mapped_column(String(24), unique=True)
    adherent_id: Mapped[int] = mapped_column(ForeignKey("adherent.id"), index=True)
    entreprise_id: Mapped[int | None] = mapped_column(
        ForeignKey("entreprise_souscriptrice.id"), nullable=True, index=True
    )
    produit_id: Mapped[int] = mapped_column(ForeignKey("produit.id"), index=True)
    #: Police collective dont ce contrat est une affiliation. Nul en individuel et en prevoyance.
    #: Une affiliation ne peut pas survivre a sa police : la resiliation de la police entraine celle
    #: de toutes ses affiliations, regle appliquee par le generateur et par le service.
    police_id: Mapped[int | None] = mapped_column(
        ForeignKey("police_collective.id"), nullable=True, index=True
    )

    statut: Mapped[StatutContrat] = mapped_column(colonne_enum(StatutContrat, "statut_contrat"), index=True)
    date_souscription: Mapped[date] = mapped_column(Date)
    date_effet: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    date_resiliation: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    motif_resiliation: Mapped[MotifResiliation | None] = mapped_column(
        colonne_enum(MotifResiliation, "motif_resiliation"), nullable=True
    )

    formule_id: Mapped[int] = mapped_column(ForeignKey("formule.id"), index=True)
    apporteur_id: Mapped[int] = mapped_column(ForeignKey("apporteur.id"), index=True)
    cotisation_mensuelle_cents: Mapped[int] = centimes()
    fractionnement: Mapped[Fractionnement] = mapped_column(colonne_enum(Fractionnement, "fractionnement"))

    cree_le: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    maj_le: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    adherent: Mapped[Adherent] = relationship(back_populates="contrats")
    entreprise: Mapped[EntrepriseSouscriptrice | None] = relationship()
    produit: Mapped[Produit] = relationship()
    formule: Mapped[Formule] = relationship()
    apporteur: Mapped[Apporteur] = relationship()
    avenants: Mapped[list[Avenant]] = relationship(
        back_populates="contrat", order_by="Avenant.numero_ordre", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("cotisation_mensuelle_cents >= 0", name="cotisation_positive"),
        # Un devis n'a pas pris effet, et tout contrat qui a quitte l'etat de devis en a une.
        CheckConstraint(
            "(statut = 'devis' AND date_effet IS NULL) OR (statut <> 'devis' AND date_effet IS NOT NULL)",
            name="date_effet_selon_statut",
        ),
        # Resilier exige une date et un motif. C'est l'exigence de la specification, portee par la base
        # et non seulement par l'API, pour qu'aucun chemin d'ecriture ne puisse y echapper.
        CheckConstraint(
            "(statut = 'resilie' AND date_resiliation IS NOT NULL AND motif_resiliation IS NOT NULL)"
            " OR (statut <> 'resilie' AND date_resiliation IS NULL AND motif_resiliation IS NULL)",
            name="resiliation_datee_et_motivee",
        ),
        CheckConstraint(
            "date_effet IS NULL OR date_effet >= date_souscription", name="effet_apres_souscription"
        ),
        CheckConstraint(
            "date_resiliation IS NULL OR date_effet IS NULL OR date_resiliation >= date_effet",
            name="resiliation_apres_effet",
        ),
        # Liste du portefeuille filtree par statut puis triee par date d'effet : requete de l'ecran
        # d'accueil du gestionnaire.
        Index("ix_contrat_statut_effet", "statut", "date_effet"),
        Index("ix_contrat_apporteur_statut", "apporteur_id", "statut"),
    )


class Avenant(Base):
    """Version datee d'un contrat. Porte l'etat complet du contrat a partir de sa date d'effet.

    `numero_ordre` commence a zero pour la souscription et s'incremente a chaque evenement. Le couple
    (contrat, numero d'ordre) est unique : deux avenants ne peuvent pas occuper le meme rang, ce qui rend
    l'historique non ambigu meme si deux avenants partagent une date d'effet.
    """

    __tablename__ = "avenant"

    id: Mapped[int] = cle_primaire()
    contrat_id: Mapped[int] = mapped_column(ForeignKey("contrat.id", ondelete="CASCADE"), index=True)
    numero_ordre: Mapped[int] = mapped_column()
    type_avenant: Mapped[TypeAvenant] = mapped_column(colonne_enum(TypeAvenant, "type_avenant"))
    date_effet: Mapped[date] = mapped_column(Date)
    # Date de saisie par le gestionnaire, distincte de la date d'effet : un avenant peut etre saisi
    # apres coup, et la piste d'audit doit garder les deux.
    date_saisie: Mapped[date] = mapped_column(Date)
    #: Qui a saisi l'acte. Une piste d'audit qui ne dit pas « qui » ne repond qu'a la moitie de la
    #: question posee par un controle. Le projet n'ayant pas d'authentification, la valeur vaut
    #: « generateur » pour les donnees simulees et « saisie_manuelle » pour les actes passes par
    #: l'API ou l'interface. La colonne est en place, et brancher une authentification reviendra a
    #: y ecrire l'identifiant reel plutot qu'a modifier le schema.
    auteur: Mapped[str] = mapped_column(String(60), default="saisie_manuelle")

    statut: Mapped[StatutContrat] = mapped_column(colonne_enum(StatutContrat, "statut_contrat"))
    formule_id: Mapped[int] = mapped_column(ForeignKey("formule.id"), index=True)
    apporteur_id: Mapped[int] = mapped_column(ForeignKey("apporteur.id"), index=True)
    cotisation_mensuelle_cents: Mapped[int] = centimes()
    fractionnement: Mapped[Fractionnement] = mapped_column(colonne_enum(Fractionnement, "fractionnement"))
    motif_resiliation: Mapped[MotifResiliation | None] = mapped_column(
        colonne_enum(MotifResiliation, "motif_resiliation"), nullable=True
    )
    commentaire: Mapped[str | None] = mapped_column(Text, nullable=True)

    contrat: Mapped[Contrat] = relationship(back_populates="avenants")
    formule: Mapped[Formule] = relationship()
    apporteur: Mapped[Apporteur] = relationship()

    __table_args__ = (
        UniqueConstraint("contrat_id", "numero_ordre", name="uq_avenant_contrat_ordre"),
        CheckConstraint("numero_ordre >= 0", name="ordre_positif"),
        CheckConstraint("cotisation_mensuelle_cents >= 0", name="cotisation_positive"),
        CheckConstraint(
            "(statut = 'resilie' AND motif_resiliation IS NOT NULL)"
            " OR (statut <> 'resilie' AND motif_resiliation IS NULL)",
            name="motif_si_resilie",
        ),
        # Index de la reconstitution a date : « le dernier avenant de ce contrat avant telle date ».
        Index("ix_avenant_contrat_effet", "contrat_id", "date_effet"),
    )
