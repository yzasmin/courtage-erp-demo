"""Flux financiers : appels de cotisation, encaissements, prestations, commissions, retrocessions,
bordereaux.

Le sens de circulation de l'argent, qui determine tout le reste du module :

- Le cabinet **appelle** la cotisation aupres de l'adherent, ou de l'entreprise souscriptrice en
  collectif. C'est le quittancement. Il **encaisse** ensuite, et le rapprochement entre l'appele et
  l'encaisse est le travail du module 2.
- Le cabinet **sert** les prestations quand la compagnie lui en a delegue la gestion.
- La compagnie **verse** au cabinet une commission sur les cotisations.
- Le cabinet **reverse** a l'apporteur une part de cette commission : la retrocession.

Les deux derniers mouvements sont ce qui distingue un courtier grossiste d'un courtier de proximite, et
ils sont la raison d'etre du reseau d'apporteurs.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from courtage.modeles.base import Base, centimes, cle_primaire, points_de_base
from courtage.modeles.enums import (
    MoyenPaiement,
    StatutAppel,
    StatutBordereau,
    StatutPrestation,
    StatutRapprochement,
    TypeBordereau,
    colonne_enum,
)
from courtage.modeles.portefeuille import Adherent, Contrat
from courtage.modeles.referentiel import Apporteur, Compagnie, Garantie, Produit


class Bordereau(Base):
    """Document recapitulatif echange avec une compagnie ou un apporteur.

    Un bordereau s'adresse soit a une compagnie, soit a un apporteur, jamais aux deux : un bordereau de
    commissions vient d'une compagnie, un bordereau de retrocessions part vers un apporteur. La
    contrainte `destinataire_unique` l'impose en base.
    """

    __tablename__ = "bordereau"

    id: Mapped[int] = cle_primaire()
    reference: Mapped[str] = mapped_column(String(40), unique=True)
    type_bordereau: Mapped[TypeBordereau] = mapped_column(colonne_enum(TypeBordereau, "type_bordereau"), index=True)
    # Premier jour du mois couvert. Un bordereau est mensuel dans ce projet.
    periode: Mapped[date] = mapped_column(Date, index=True)
    date_emission: Mapped[date] = mapped_column(Date)
    compagnie_id: Mapped[int | None] = mapped_column(ForeignKey("compagnie.id"), nullable=True, index=True)
    apporteur_id: Mapped[int | None] = mapped_column(ForeignKey("apporteur.id"), nullable=True, index=True)
    nombre_lignes: Mapped[int] = mapped_column(default=0)
    montant_total_cents: Mapped[int] = centimes(default=0)
    statut: Mapped[StatutBordereau] = mapped_column(colonne_enum(StatutBordereau, "statut_bordereau"))

    compagnie: Mapped[Compagnie | None] = relationship()
    apporteur: Mapped[Apporteur | None] = relationship()

    __table_args__ = (
        # Exactement un destinataire. Ecrit sans operateur booleen pour rester valide sur les deux moteurs.
        CheckConstraint(
            "(CASE WHEN compagnie_id IS NULL THEN 0 ELSE 1 END)"
            " + (CASE WHEN apporteur_id IS NULL THEN 0 ELSE 1 END) = 1",
            name="destinataire_unique",
        ),
        CheckConstraint("nombre_lignes >= 0", name="nombre_lignes_positif"),
    )


class AppelCotisation(Base):
    """Cotisation appelee aupres du payeur pour une periode de garantie donnee.

    **Deux formes, un seul objet.** En individuel, l'appel s'adresse a l'adherent et porte sur son
    contrat : `contrat_id` est renseigne. En collectif, l'appel s'adresse a l'entreprise souscriptrice
    et porte sur sa police : `police_id` est renseigne, et le detail affilie par affilie vit dans
    `ligne_appel_cotisation`. Exactement l'un des deux est renseigne, et une contrainte l'impose.

    Modeliser les deux formes dans la meme table est volontaire : un appel de cotisation est un appel de
    cotisation, il se rapproche d'un encaissement de la meme facon, il entre dans un bordereau de la
    meme facon, et le module de pilotage somme une seule colonne.

    Le montant est decompose en part hors taxe et part de taxe, et le total est stocke puis verifie par
    contrainte. Stocker le total plutot que de le recalculer permet aux requetes de pilotage de sommer une
    colonne, et la contrainte garantit qu'il ne peut pas mentir.

    `taux_taxe_bp` est fige a l'emission : c'est le taux en vigueur a la date d'echeance. Une relecture de
    bordereau deux ans plus tard retrouve le taux applique, meme si le parametre a change depuis.
    """

    __tablename__ = "appel_cotisation"

    id: Mapped[int] = cle_primaire()
    reference_appel: Mapped[str] = mapped_column(String(40), unique=True)
    #: Renseigne pour un appel individuel. Nul pour un appel groupe, qui porte `police_id`.
    contrat_id: Mapped[int | None] = mapped_column(ForeignKey("contrat.id"), nullable=True, index=True)
    #: Renseigne pour un appel groupe adresse a l'entreprise souscriptrice.
    police_id: Mapped[int | None] = mapped_column(
        ForeignKey("police_collective.id"), nullable=True, index=True
    )
    #: Nombre d'affilies couverts par un appel groupe. Vaut 1 pour un appel individuel.
    nombre_affilies: Mapped[int] = mapped_column(default=1)
    periode_debut: Mapped[date] = mapped_column(Date)
    periode_fin: Mapped[date] = mapped_column(Date)
    date_echeance: Mapped[date] = mapped_column(Date, index=True)
    montant_ht_cents: Mapped[int] = centimes()
    montant_taxe_cents: Mapped[int] = centimes()
    montant_total_cents: Mapped[int] = centimes()
    taux_taxe_bp: Mapped[int] = points_de_base()
    statut: Mapped[StatutAppel] = mapped_column(colonne_enum(StatutAppel, "statut_appel"), index=True)
    bordereau_id: Mapped[int | None] = mapped_column(ForeignKey("bordereau.id"), nullable=True, index=True)

    contrat: Mapped[Contrat | None] = relationship()
    bordereau: Mapped[Bordereau | None] = relationship()
    encaissements: Mapped[list[Encaissement]] = relationship(back_populates="appel")

    __table_args__ = (
        CheckConstraint("montant_ht_cents >= 0", name="montant_ht_positif"),
        CheckConstraint("montant_taxe_cents >= 0", name="montant_taxe_positif"),
        CheckConstraint(
            "montant_total_cents = montant_ht_cents + montant_taxe_cents", name="total_egale_ht_plus_taxe"
        ),
        CheckConstraint("periode_fin >= periode_debut", name="periode_coherente"),
        CheckConstraint("taux_taxe_bp >= 0 AND taux_taxe_bp <= 10000", name="taux_taxe_entre_0_et_100"),
        # Un appel s'adresse a un contrat ou a une police, jamais aux deux, jamais a aucun.
        CheckConstraint(
            "(CASE WHEN contrat_id IS NULL THEN 0 ELSE 1 END)"
            " + (CASE WHEN police_id IS NULL THEN 0 ELSE 1 END) = 1",
            name="destinataire_unique",
        ),
        CheckConstraint("nombre_affilies >= 1", name="au_moins_un_affilie"),
        # Ni un contrat ni une police ne peuvent etre quittances deux fois pour la meme periode.
        UniqueConstraint("contrat_id", "periode_debut", name="uq_appel_contrat_periode"),
        UniqueConstraint("police_id", "periode_debut", name="uq_appel_police_periode"),
        Index("ix_appel_cotisation_contrat_echeance", "contrat_id", "date_echeance"),
        Index("ix_appel_cotisation_police_echeance", "police_id", "date_echeance"),
    )


class Encaissement(Base):
    """Mouvement bancaire rattache, ou non, a un appel de cotisation.

    `appel_id` est nullable, et c'est le point important : un encaissement arrive d'un releve bancaire
    avant d'etre rapproche. Un mouvement non rapproche est une anomalie a traiter, pas une donnee
    invalide. L'ecran des anomalies du module 2 lit cette table.

    Le montant peut etre negatif : un rejet de prelevement est un encaissement negatif, et le forcer a
    etre positif obligerait a inventer une table de rejets qui n'existe pas dans un releve bancaire.
    """

    __tablename__ = "encaissement"

    id: Mapped[int] = cle_primaire()
    reference_bancaire: Mapped[str] = mapped_column(String(60), unique=True)
    appel_id: Mapped[int | None] = mapped_column(ForeignKey("appel_cotisation.id"), nullable=True, index=True)
    contrat_id: Mapped[int | None] = mapped_column(ForeignKey("contrat.id"), nullable=True, index=True)
    date_encaissement: Mapped[date] = mapped_column(Date, index=True)
    montant_cents: Mapped[int] = centimes()
    moyen: Mapped[MoyenPaiement] = mapped_column(colonne_enum(MoyenPaiement, "moyen_paiement"))
    statut_rapprochement: Mapped[StatutRapprochement] = mapped_column(
        colonne_enum(StatutRapprochement, "statut_rapprochement"), index=True
    )
    libelle_releve: Mapped[str | None] = mapped_column(String(200), nullable=True)

    appel: Mapped[AppelCotisation | None] = relationship(back_populates="encaissements")
    contrat: Mapped[Contrat | None] = relationship()

    __table_args__ = (
        CheckConstraint("montant_cents <> 0", name="montant_non_nul"),
        # Un encaissement rapproche porte necessairement l'appel auquel il est rattache.
        CheckConstraint(
            "statut_rapprochement <> 'rapproche' OR appel_id IS NOT NULL", name="rapproche_implique_appel"
        ),
    )


class Prestation(Base):
    """Remboursement servi a un adherent au titre d'une garantie.

    Le cabinet ne sert les prestations que si la compagnie lui en a delegue la gestion. La verification se
    fait a l'enregistrement, dans `domaine.delegation`, et non ici : une contrainte de base ne sait pas
    lire une convention datee.
    """

    __tablename__ = "prestation"

    id: Mapped[int] = cle_primaire()
    reference_prestation: Mapped[str] = mapped_column(String(40), unique=True)
    contrat_id: Mapped[int] = mapped_column(ForeignKey("contrat.id"), index=True)
    adherent_id: Mapped[int] = mapped_column(ForeignKey("adherent.id"), index=True)
    garantie_id: Mapped[int] = mapped_column(ForeignKey("garantie.id"), index=True)
    date_soins: Mapped[date] = mapped_column(Date, index=True)
    date_reglement: Mapped[date | None] = mapped_column(Date, nullable=True)
    montant_demande_cents: Mapped[int] = centimes()
    montant_rembourse_cents: Mapped[int] = centimes()
    statut: Mapped[StatutPrestation] = mapped_column(colonne_enum(StatutPrestation, "statut_prestation"), index=True)
    motif_rejet: Mapped[str | None] = mapped_column(String(200), nullable=True)
    bordereau_id: Mapped[int | None] = mapped_column(ForeignKey("bordereau.id"), nullable=True, index=True)

    contrat: Mapped[Contrat] = relationship()
    adherent: Mapped[Adherent] = relationship()
    garantie: Mapped[Garantie] = relationship()
    bordereau: Mapped[Bordereau | None] = relationship()

    __table_args__ = (
        CheckConstraint("montant_demande_cents >= 0", name="montant_demande_positif"),
        CheckConstraint("montant_rembourse_cents >= 0", name="montant_rembourse_positif"),
        # On ne rembourse jamais plus que ce qui est demande.
        CheckConstraint(
            "montant_rembourse_cents <= montant_demande_cents", name="rembourse_inferieur_demande"
        ),
        CheckConstraint(
            "(statut = 'reglee' AND date_reglement IS NOT NULL) OR (statut <> 'reglee' AND date_reglement IS NULL)",
            name="reglement_date_si_reglee",
        ),
        # Une prestation rejetee ne rembourse rien et porte un motif.
        CheckConstraint(
            "statut <> 'rejetee' OR (montant_rembourse_cents = 0 AND motif_rejet IS NOT NULL)",
            name="rejet_motive_et_non_rembourse",
        ),
        CheckConstraint(
            "date_reglement IS NULL OR date_reglement >= date_soins", name="reglement_apres_soins"
        ),
        Index("ix_prestation_contrat_soins", "contrat_id", "date_soins"),
        Index("ix_prestation_adherent_soins", "adherent_id", "date_soins"),
    )


class Commission(Base):
    """Commission due par la compagnie au cabinet, pour un appel de cotisation.

    Une commission par appel de cotisation, pas plus : l'unicite sur `appel_id` empeche un double
    commissionnement, qui est l'erreur la plus couteuse de ce calcul. Le taux et l'assiette sont figes au
    calcul, pour qu'un bordereau reste explicable apres un changement de bareme.

    **Deux formes, comme l'appel qu'elle remunere.** Une commission sur un appel individuel porte le
    contrat, une commission sur un appel groupe porte la police collective. Le porteur suit donc
    exactement celui de `appel_cotisation`, et la meme contrainte impose d'en avoir un et un seul. Le
    forcer a etre un contrat obligerait a designer un affilie au hasard sur une cotisation appelee
    aupres de l'entreprise, ce qui rendrait le bordereau indefendable devant la compagnie.
    """

    __tablename__ = "commission"

    id: Mapped[int] = cle_primaire()
    appel_id: Mapped[int] = mapped_column(ForeignKey("appel_cotisation.id"), unique=True)
    #: Renseigne pour une commission sur appel individuel. Nul pour un appel groupe.
    contrat_id: Mapped[int | None] = mapped_column(ForeignKey("contrat.id"), nullable=True, index=True)
    #: Renseigne pour une commission sur appel groupe adresse a l'entreprise souscriptrice.
    police_id: Mapped[int | None] = mapped_column(
        ForeignKey("police_collective.id"), nullable=True, index=True
    )
    compagnie_id: Mapped[int] = mapped_column(ForeignKey("compagnie.id"), index=True)
    produit_id: Mapped[int] = mapped_column(ForeignKey("produit.id"), index=True)
    periode: Mapped[date] = mapped_column(Date, index=True)
    assiette_cents: Mapped[int] = centimes()
    taux_bp: Mapped[int] = points_de_base()
    montant_cents: Mapped[int] = centimes()
    bordereau_id: Mapped[int | None] = mapped_column(ForeignKey("bordereau.id"), nullable=True, index=True)

    appel: Mapped[AppelCotisation] = relationship()
    contrat: Mapped[Contrat | None] = relationship()
    compagnie: Mapped[Compagnie] = relationship()
    produit: Mapped[Produit] = relationship()
    bordereau: Mapped[Bordereau | None] = relationship()

    __table_args__ = (
        CheckConstraint("assiette_cents >= 0", name="assiette_positive"),
        CheckConstraint("montant_cents >= 0", name="montant_positif"),
        CheckConstraint("taux_bp >= 0 AND taux_bp <= 10000", name="taux_entre_0_et_100"),
        CheckConstraint("montant_cents <= assiette_cents", name="commission_inferieure_assiette"),
        # Un porteur, et un seul, comme pour l'appel de cotisation qu'elle remunere.
        CheckConstraint(
            "(CASE WHEN contrat_id IS NULL THEN 0 ELSE 1 END)"
            " + (CASE WHEN police_id IS NULL THEN 0 ELSE 1 END) = 1",
            name="destinataire_unique",
        ),
    )


class Retrocession(Base):
    """Part de commission reversee a l'apporteur qui a amene l'affaire.

    Une retrocession par commission. L'apporteur retenu est celui en vigueur sur le contrat a la date
    d'echeance de l'appel, ce qui rend un changement d'apporteur en cours d'annee sans effet retroactif
    sur les mois deja regles.
    """

    __tablename__ = "retrocession"

    id: Mapped[int] = cle_primaire()
    commission_id: Mapped[int] = mapped_column(ForeignKey("commission.id"), unique=True)
    apporteur_id: Mapped[int] = mapped_column(ForeignKey("apporteur.id"), index=True)
    periode: Mapped[date] = mapped_column(Date, index=True)
    assiette_cents: Mapped[int] = centimes()
    taux_bp: Mapped[int] = points_de_base()
    montant_cents: Mapped[int] = centimes()
    bordereau_id: Mapped[int | None] = mapped_column(ForeignKey("bordereau.id"), nullable=True, index=True)
    commentaire: Mapped[str | None] = mapped_column(Text, nullable=True)

    commission: Mapped[Commission] = relationship()
    apporteur: Mapped[Apporteur] = relationship()
    bordereau: Mapped[Bordereau | None] = relationship()

    __table_args__ = (
        CheckConstraint("assiette_cents >= 0", name="assiette_positive"),
        CheckConstraint("montant_cents >= 0", name="montant_positif"),
        CheckConstraint("taux_bp >= 0 AND taux_bp <= 10000", name="taux_entre_0_et_100"),
        # Le cabinet ne peut pas reverser plus que la commission qu'il a recue.
        CheckConstraint("montant_cents <= assiette_cents", name="retrocession_inferieure_commission"),
        Index("ix_retrocession_apporteur_periode", "apporteur_id", "periode"),
    )
