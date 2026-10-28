"""Referentiel : branches, compagnies, produits, garanties, formules, apporteurs et taux dates.

Tout ce qui est parametre du metier vit ici, en base, avec des dates de validite. Aucun taux, aucun
plafond et aucune regle de commissionnement n'est ecrit dans le code : un gestionnaire doit pouvoir les
changer sans livraison. C'est aussi la condition pour que le meme logiciel serve deux cabinets qui ne
commissionnent pas pareil.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from courtage.modeles.base import Base, centimes, cle_primaire, points_de_base
from courtage.modeles.enums import AssietteCommission, TypeApporteur, colonne_enum


class Branche(Base):
    """Branche d'assurance. Table et non enumeration, pour rester extensible.

    Le projet livre la sante individuelle, la sante collective et la prevoyance. Ouvrir une branche
    automobile ou dommages est une insertion dans cette table plus les produits correspondants, pas une
    modification du code.
    """

    __tablename__ = "branche"

    id: Mapped[int] = cle_primaire()
    code: Mapped[str] = mapped_column(String(32), unique=True)
    libelle: Mapped[str] = mapped_column(String(120))
    # Regroupement d'analyse : « personnes » pour la sante et la prevoyance, « biens » si des branches
    # dommages etaient ouvertes un jour. Sert les axes du module de pilotage.
    famille: Mapped[str] = mapped_column(String(32))

    produits: Mapped[list[Produit]] = relationship(back_populates="branche")


class Compagnie(Base):
    """Porteur du risque. Le cabinet place ses contrats, la compagnie les assure."""

    __tablename__ = "compagnie"

    id: Mapped[int] = cle_primaire()
    code: Mapped[str] = mapped_column(String(32), unique=True)
    raison_sociale: Mapped[str] = mapped_column(String(200))
    siren: Mapped[str | None] = mapped_column(String(9), nullable=True)
    actif: Mapped[bool] = mapped_column(default=True)

    produits: Mapped[list[Produit]] = relationship(back_populates="compagnie")

    __table_args__ = (CheckConstraint("siren IS NULL OR length(siren) = 9", name="siren_9_chiffres"),)


class Apporteur(Base):
    """Membre du reseau de distribution : mandataire, sous-courtier, comparateur ou salarie."""

    __tablename__ = "apporteur"

    id: Mapped[int] = cle_primaire()
    code: Mapped[str] = mapped_column(String(32), unique=True)
    nom: Mapped[str] = mapped_column(String(200))
    type_apporteur: Mapped[TypeApporteur] = mapped_column(colonne_enum(TypeApporteur, "type_apporteur"))
    # Numero d'immatriculation au registre unique des intermediaires. Donnee de simulation.
    numero_orias: Mapped[str | None] = mapped_column(String(16), nullable=True)
    departement: Mapped[str] = mapped_column(String(3))
    date_entree: Mapped[date] = mapped_column(Date)
    date_sortie: Mapped[date | None] = mapped_column(Date, nullable=True)
    actif: Mapped[bool] = mapped_column(default=True)

    __table_args__ = (
        CheckConstraint("date_sortie IS NULL OR date_sortie >= date_entree", name="sortie_apres_entree"),
        Index("ix_apporteur_type_actif", "type_apporteur", "actif"),
    )


class Produit(Base):
    """Produit d'assurance place par le cabinet, rattache a une branche et a une compagnie."""

    __tablename__ = "produit"

    id: Mapped[int] = cle_primaire()
    code: Mapped[str] = mapped_column(String(32), unique=True)
    libelle: Mapped[str] = mapped_column(String(200))
    branche_id: Mapped[int] = mapped_column(ForeignKey("branche.id"), index=True)
    compagnie_id: Mapped[int] = mapped_column(ForeignKey("compagnie.id"), index=True)
    date_ouverture: Mapped[date] = mapped_column(Date)
    date_fermeture: Mapped[date | None] = mapped_column(Date, nullable=True)

    branche: Mapped[Branche] = relationship(back_populates="produits")
    compagnie: Mapped[Compagnie] = relationship(back_populates="produits")
    formules: Mapped[list[Formule]] = relationship(back_populates="produit")

    __table_args__ = (
        CheckConstraint(
            "date_fermeture IS NULL OR date_fermeture >= date_ouverture", name="fermeture_apres_ouverture"
        ),
    )


class Garantie(Base):
    """Poste de garantie : soins courants, hospitalisation, optique, deces, incapacite...

    **Catalogue global, et non liste par branche.** C'est la formule qui dit quelles garanties
    s'appliquent et a quel niveau, par `formule_garantie`. Rattacher la garantie a une branche aurait
    oblige a dupliquer « hospitalisation » autant de fois qu'il y a de branches qui la couvrent, donc a
    maintenir autant de referentiels que de branches. Ouvrir une branche automobile ajoute ici ses
    propres postes, sans toucher aux postes existants.
    """

    __tablename__ = "garantie"

    id: Mapped[int] = cle_primaire()
    code: Mapped[str] = mapped_column(String(32), unique=True)
    libelle: Mapped[str] = mapped_column(String(200))
    # Regroupement d'affichage sur la fiche contrat : « sante » ou « prevoyance ». Un gestionnaire lit
    # les postes de soins ensemble, et les garanties de prevoyance ensemble.
    famille: Mapped[str] = mapped_column(String(32), index=True)
    # Ordre d'affichage. Les tableaux de garanties se lisent ainsi toujours dans le meme ordre d'un
    # produit a l'autre.
    rang: Mapped[int] = mapped_column(default=0)


class Formule(Base):
    """Niveau de couverture propose sur un produit. Le contrat porte une formule a un instant donne."""

    __tablename__ = "formule"

    id: Mapped[int] = cle_primaire()
    produit_id: Mapped[int] = mapped_column(ForeignKey("produit.id"), index=True)
    code: Mapped[str] = mapped_column(String(32))
    libelle: Mapped[str] = mapped_column(String(200))
    niveau: Mapped[int] = mapped_column()
    tarif_base_mensuel_cents: Mapped[int] = centimes()

    produit: Mapped[Produit] = relationship(back_populates="formules")
    couvertures: Mapped[list[FormuleGarantie]] = relationship(back_populates="formule")

    __table_args__ = (
        UniqueConstraint("produit_id", "code", name="uq_formule_produit_code"),
        CheckConstraint("niveau BETWEEN 1 AND 4", name="niveau_entre_1_et_4"),
        CheckConstraint("tarif_base_mensuel_cents >= 0", name="tarif_base_positif"),
    )


class FormuleGarantie(Base):
    """Niveau de couverture d'une garantie dans une formule : taux de remboursement et plafond annuel.

    Le plafond sert deja au module 1 pour l'affichage de la fiche contrat, et servira au module 2 pour le
    controle non bloquant « prestation proche du plafond de garantie ».
    """

    __tablename__ = "formule_garantie"

    id: Mapped[int] = cle_primaire()
    formule_id: Mapped[int] = mapped_column(ForeignKey("formule.id"), index=True)
    garantie_id: Mapped[int] = mapped_column(ForeignKey("garantie.id"), index=True)
    libelle_couverture: Mapped[str] = mapped_column(String(200))
    taux_remboursement_bp: Mapped[int] = points_de_base()
    plafond_annuel_cents: Mapped[int | None] = centimes(nullable=True)

    formule: Mapped[Formule] = relationship(back_populates="couvertures")
    garantie: Mapped[Garantie] = relationship()

    __table_args__ = (
        UniqueConstraint("formule_id", "garantie_id", name="uq_formule_garantie"),
        CheckConstraint("taux_remboursement_bp >= 0", name="taux_remboursement_positif"),
        CheckConstraint("plafond_annuel_cents IS NULL OR plafond_annuel_cents >= 0", name="plafond_positif"),
    )


class TauxTaxe(Base):
    """Taux de taxe sur les cotisations, par branche et par periode de validite.

    Valeurs de simulation, voir `docs/HYPOTHESES.md`. Ce qui est demontre ici n'est pas la justesse du
    taux mais le mecanisme : le calcul va chercher le taux en vigueur a la date d'echeance, donc un
    changement au 1er janvier se repercute sans toucher au code.
    """

    __tablename__ = "taux_taxe"

    id: Mapped[int] = cle_primaire()
    branche_id: Mapped[int] = mapped_column(ForeignKey("branche.id"), index=True)
    libelle: Mapped[str] = mapped_column(String(200))
    taux_bp: Mapped[int] = points_de_base()
    date_debut_validite: Mapped[date] = mapped_column(Date)
    date_fin_validite: Mapped[date | None] = mapped_column(Date, nullable=True)

    branche: Mapped[Branche] = relationship()

    __table_args__ = (
        CheckConstraint("taux_bp >= 0 AND taux_bp <= 10000", name="taux_taxe_entre_0_et_100"),
        CheckConstraint(
            "date_fin_validite IS NULL OR date_fin_validite >= date_debut_validite", name="validite_taxe_coherente"
        ),
        Index("ix_taux_taxe_branche_debut", "branche_id", "date_debut_validite"),
    )


class TauxCommission(Base):
    """Taux de commission verse par la compagnie au cabinet, par produit et par periode de validite."""

    __tablename__ = "taux_commission"

    id: Mapped[int] = cle_primaire()
    produit_id: Mapped[int] = mapped_column(ForeignKey("produit.id"), index=True)
    taux_bp: Mapped[int] = points_de_base()
    assiette: Mapped[AssietteCommission] = mapped_column(colonne_enum(AssietteCommission, "assiette_commission"))
    date_debut_validite: Mapped[date] = mapped_column(Date)
    date_fin_validite: Mapped[date | None] = mapped_column(Date, nullable=True)

    produit: Mapped[Produit] = relationship()

    __table_args__ = (
        CheckConstraint("taux_bp >= 0 AND taux_bp <= 10000", name="taux_commission_entre_0_et_100"),
        CheckConstraint(
            "date_fin_validite IS NULL OR date_fin_validite >= date_debut_validite",
            name="validite_commission_coherente",
        ),
        Index("ix_taux_commission_produit_debut", "produit_id", "date_debut_validite"),
    )


class TauxRetrocession(Base):
    """Part de la commission reversee a un apporteur.

    `produit_id` nul signifie « tous les produits » : c'est le cas courant d'un accord cadre avec un
    mandataire. Une ligne portant un produit precis l'emporte sur l'accord cadre, regle appliquee dans
    `domaine.montants.taux_retrocession_applicable`.
    """

    __tablename__ = "taux_retrocession"

    id: Mapped[int] = cle_primaire()
    apporteur_id: Mapped[int] = mapped_column(ForeignKey("apporteur.id"), index=True)
    produit_id: Mapped[int | None] = mapped_column(ForeignKey("produit.id"), nullable=True, index=True)
    taux_bp: Mapped[int] = points_de_base()
    date_debut_validite: Mapped[date] = mapped_column(Date)
    date_fin_validite: Mapped[date | None] = mapped_column(Date, nullable=True)

    apporteur: Mapped[Apporteur] = relationship()
    produit: Mapped[Produit | None] = relationship()

    __table_args__ = (
        CheckConstraint("taux_bp >= 0 AND taux_bp <= 10000", name="taux_retrocession_entre_0_et_100"),
        CheckConstraint(
            "date_fin_validite IS NULL OR date_fin_validite >= date_debut_validite",
            name="validite_retrocession_coherente",
        ),
        Index("ix_taux_retrocession_apporteur_debut", "apporteur_id", "date_debut_validite"),
    )
