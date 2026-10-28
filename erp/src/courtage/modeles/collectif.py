"""Police collective : le contrat cadre souscrit par une entreprise pour ses salaries.

Ce que ce module corrige, et pourquoi cela comptait.

En sante collective, ce n'est pas le salarie qui souscrit : c'est son entreprise. Elle signe une
**police**, et chaque salarie couvert y est **affilie**. Deux consequences que le schema doit porter,
sous peine de decrire un metier qui n'existe pas :

1. **La cotisation est appelee aupres de l'entreprise, pas aupres du salarie.** Un appel de cotisation
   collectif est un appel groupe, adresse au souscripteur, dont le detail ligne a ligne donne la part de
   chaque affilie. C'est ce document que le service comptable de l'entreprise regle, en un virement.
2. **La police a sa propre vie.** Elle prend effet, elle est revalorisee, elle est resiliee. Quand elle
   est resiliee, toutes les affiliations tombent avec elle, ce qui n'a pas d'equivalent en individuel.

Le modele precedent donnait un contrat par salarie et un appel de cotisation par salarie, sans aucune
police. La documentation affirmait pourtant que la cotisation etait appelee aupres de l'entreprise : le
schema et le texte ne decrivaient pas la meme chose.

Ce qui est conserve : **l'affiliation reste un contrat**. C'est le bulletin d'affiliation du salarie, il
porte sa formule, ses garanties, ses prestations et son historique d'avenants, exactement comme un
contrat individuel. Il porte en plus un lien vers sa police. Ce choix evite de dupliquer toute la
mecanique d'historisation pour le collectif, et il correspond au vocabulaire : un gestionnaire parle bien
du « contrat » d'un salarie.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from courtage.modeles.base import Base, cle_primaire
from courtage.modeles.enums import Fractionnement, MotifResiliation, StatutContrat, colonne_enum
from courtage.modeles.portefeuille import EntrepriseSouscriptrice
from courtage.modeles.referentiel import Apporteur, Formule, Produit


class PoliceCollective(Base):
    """Contrat cadre souscrit par une entreprise pour ses salaries.

    La police porte le souscripteur, le produit, l'apporteur et les dates. Elle ne porte pas de
    cotisation : le tarif depend de la formule de chaque affilie et de son age, et il se lit sur les
    contrats rattaches.
    """

    __tablename__ = "police_collective"

    id: Mapped[int] = cle_primaire()
    numero_police: Mapped[str] = mapped_column(String(24), unique=True)
    entreprise_id: Mapped[int] = mapped_column(ForeignKey("entreprise_souscriptrice.id"), index=True)
    produit_id: Mapped[int] = mapped_column(ForeignKey("produit.id"), index=True)
    #: Formule du socle obligatoire de la police. Un affilie peut souscrire une formule superieure.
    formule_socle_id: Mapped[int] = mapped_column(ForeignKey("formule.id"), index=True)
    apporteur_id: Mapped[int] = mapped_column(ForeignKey("apporteur.id"), index=True)

    statut: Mapped[StatutContrat] = mapped_column(colonne_enum(StatutContrat, "statut_contrat"), index=True)
    date_souscription: Mapped[date] = mapped_column(Date)
    date_effet: Mapped[date] = mapped_column(Date, index=True)
    date_resiliation: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    motif_resiliation: Mapped[MotifResiliation | None] = mapped_column(
        colonne_enum(MotifResiliation, "motif_resiliation"), nullable=True
    )
    #: Periodicite des appels groupes adresses a l'entreprise. Le trimestriel est l'usage courant en
    #: collectif, parce qu'un service comptable ne traite pas douze appels par an de bon coeur.
    fractionnement: Mapped[Fractionnement] = mapped_column(colonne_enum(Fractionnement, "fractionnement"))

    entreprise: Mapped[EntrepriseSouscriptrice] = relationship()
    produit: Mapped[Produit] = relationship()
    formule_socle: Mapped[Formule] = relationship()
    apporteur: Mapped[Apporteur] = relationship()

    __table_args__ = (
        CheckConstraint(
            "(statut = 'resilie' AND date_resiliation IS NOT NULL AND motif_resiliation IS NOT NULL)"
            " OR (statut <> 'resilie' AND date_resiliation IS NULL AND motif_resiliation IS NULL)",
            name="resiliation_datee_et_motivee",
        ),
        CheckConstraint(
            "date_resiliation IS NULL OR date_resiliation >= date_effet", name="resiliation_apres_effet"
        ),
        CheckConstraint("date_effet >= date_souscription", name="effet_apres_souscription"),
        Index("ix_police_collective_entreprise_effet", "entreprise_id", "date_effet"),
    )


class LigneAppelCotisation(Base):
    """Detail d'un appel de cotisation groupe : la part d'un affilie dans l'appel de sa police.

    Pourquoi ces lignes existent plutot qu'un simple montant global. Le service comptable de
    l'entreprise regle un montant unique, mais le cabinet doit savoir, affilie par affilie, ce qui a ete
    appele : pour la commission, pour la repartition en cas de reglement partiel, et pour repondre a un
    salarie qui conteste sa retenue sur salaire. Sans le detail, un appel groupe est une somme que
    personne ne sait justifier.
    """

    __tablename__ = "ligne_appel_cotisation"

    id: Mapped[int] = cle_primaire()
    appel_id: Mapped[int] = mapped_column(
        ForeignKey("appel_cotisation.id", ondelete="CASCADE"), index=True
    )
    contrat_id: Mapped[int] = mapped_column(ForeignKey("contrat.id"), index=True)
    montant_ht_cents: Mapped[int] = mapped_column()
    montant_taxe_cents: Mapped[int] = mapped_column()
    montant_total_cents: Mapped[int] = mapped_column()

    __table_args__ = (
        CheckConstraint("montant_ht_cents >= 0", name="montant_ht_positif"),
        CheckConstraint("montant_taxe_cents >= 0", name="montant_taxe_positif"),
        CheckConstraint(
            "montant_total_cents = montant_ht_cents + montant_taxe_cents", name="total_egale_ht_plus_taxe"
        ),
        # Un affilie n'apparait qu'une fois dans un appel donne.
        Index("ix_ligne_appel_cotisation_appel_contrat", "appel_id", "contrat_id", unique=True),
    )
