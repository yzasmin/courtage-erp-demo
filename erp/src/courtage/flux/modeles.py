"""Tables de la chaine des flux financiers : journal des lots, anomalies, couche brute, rapprochement.

Ces six tables sont ajoutees par le module 2. Elles sont volontairement separees des entites du
module 1 : la couche brute contient **ce que l'emetteur a envoye**, pas ce que le cabinet tient pour
vrai. Melanger les deux est l'erreur qui rend une reprise de donnees impossible a auditer, parce qu'on ne
sait plus si une valeur vient du fichier recu ou d'une correction de gestionnaire.

Trois proprietes sont portees par le schema, et non par le code :

1. **Un fichier, un lot.** L'unicite porte sur le couple empreinte et nom de fichier, et ce choix
   demande une phrase. Redeposer exactement le meme fichier, meme nom et memes octets, ne cree pas un
   second lot : c'est la base qui refuse, donc rejouer le graphe ne change rien, meme si deux executions
   se chevauchent. Mais le **meme contenu sous un autre nom** est un cas different, et frequent : une
   compagnie qui renvoie son bordereau en le renommant. Celui-la cree bien un lot, qui sera rejete par
   le controle « fichier deja charge ». Le rejet reste ainsi visible au journal au lieu d'etre avale en
   silence par une contrainte d'unicite.
2. **Une ligne brute appartient a un lot et a une seule.** La cle etrangere est en `CASCADE` : supprimer
   un lot efface ses lignes, ce qui est le seul comportement correct pour une couche brute.
3. **Un encaissement a au plus un rapprochement.** L'unicite sur `encaissement_id` empeche deux verdicts
   contradictoires sur le meme mouvement bancaire.
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

from courtage.flux.enums import (
    CodeAnomalie,
    FormatFichier,
    MethodeRapprochement,
    SeveriteAnomalie,
    StatutLot,
    TypeFlux,
)
from courtage.modeles.base import Base, centimes, cle_primaire
from courtage.modeles.enums import colonne_enum
from courtage.modeles.financier import AppelCotisation, Encaissement
from courtage.modeles.referentiel import Compagnie


class LotFichier(Base):
    """Un fichier recu, et tout ce que la piste d'audit doit en garder.

    Le lot est l'unite de traitement du graphe Airflow : un fichier, un lot, un verdict. Le journal
    d'execution par lot exige par la specification est une lecture de cette table.

    `sha256` est calcule sur le fichier tel qu'il a ete depose, avant toute lecture ou normalisation.
    C'est ce qui permet d'affirmer deux ans plus tard que le fichier archive est bien celui qui a ete
    traite.
    """

    __tablename__ = "lot_fichier"

    id: Mapped[int] = cle_primaire()
    reference_lot: Mapped[str] = mapped_column(String(60), unique=True)
    nom_fichier: Mapped[str] = mapped_column(String(200))
    type_flux: Mapped[TypeFlux] = mapped_column(colonne_enum(TypeFlux, "type_flux"), index=True)
    format_fichier: Mapped[FormatFichier] = mapped_column(colonne_enum(FormatFichier, "format_fichier"))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    taille_octets: Mapped[int] = mapped_column()
    compagnie_id: Mapped[int | None] = mapped_column(ForeignKey("compagnie.id"), nullable=True, index=True)
    # Premier jour du mois couvert par le fichier.
    periode: Mapped[date] = mapped_column(Date, index=True)
    date_depot: Mapped[date] = mapped_column(Date)
    statut: Mapped[StatutLot] = mapped_column(colonne_enum(StatutLot, "statut_lot"), index=True)
    chemin_archive: Mapped[str | None] = mapped_column(String(400), nullable=True)
    nombre_lignes_fichier: Mapped[int] = mapped_column(default=0)
    nombre_lignes_chargees: Mapped[int] = mapped_column(default=0)
    # Ligne de total declaree par l'emetteur, et somme reellement calculee sur les lignes.
    # Les deux sont conservees : l'ecart est la preuve du rejet, et il doit rester lisible apres coup.
    montant_total_declare_cents: Mapped[int | None] = centimes(nullable=True)
    montant_total_calcule_cents: Mapped[int | None] = centimes(nullable=True)
    motif_rejet: Mapped[str | None] = mapped_column(Text, nullable=True)
    detecte_le: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    traite_le: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    compagnie: Mapped[Compagnie | None] = relationship()
    anomalies: Mapped[list[AnomalieLot]] = relationship(back_populates="lot", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("taille_octets > 0", name="taille_positive"),
        CheckConstraint("nombre_lignes_fichier >= 0", name="lignes_fichier_positives"),
        CheckConstraint("nombre_lignes_chargees >= 0", name="lignes_chargees_positives"),
        # Un lot rejete porte toujours son motif, et un lot charge n'en porte jamais.
        CheckConstraint(
            "(statut = 'rejete' AND motif_rejet IS NOT NULL) OR (statut <> 'rejete' AND motif_rejet IS NULL)",
            name="rejet_motive",
        ),
        # Aucune ecriture partielle : un lot rejete n'a charge aucune ligne. La contrainte est en base
        # et non seulement dans le code, parce que c'est l'exigence la plus importante du module.
        CheckConstraint(
            "statut <> 'rejete' OR nombre_lignes_chargees = 0", name="rejet_sans_ecriture_partielle"
        ),
        # Rejouer la detection sur un dossier d'arrivee inchange ne peut pas creer un second lot.
        UniqueConstraint("sha256", "nom_fichier", name="uq_lot_empreinte_nom"),
        Index("ix_lot_fichier_periode_flux", "periode", "type_flux"),
    )


class AnomalieLot(Base):
    """Une anomalie constatee sur un lot, par un controle nomme.

    `ligne_numero` est nul pour les anomalies de niveau fichier, qui sont celles qu'aucune ligne ne porte
    a elle seule : l'empreinte deja connue et la ligne de total incoherente.

    Les anomalies sont conservees meme quand le lot est rejete. C'est precisement dans ce cas qu'elles
    servent : le rapport d'anomalies est ce que le gestionnaire renvoie a l'emetteur.
    """

    __tablename__ = "anomalie_lot"

    id: Mapped[int] = cle_primaire()
    lot_id: Mapped[int] = mapped_column(ForeignKey("lot_fichier.id", ondelete="CASCADE"), index=True)
    ligne_numero: Mapped[int | None] = mapped_column(nullable=True)
    reference_ligne: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    code: Mapped[CodeAnomalie] = mapped_column(colonne_enum(CodeAnomalie, "code_anomalie"), index=True)
    severite: Mapped[SeveriteAnomalie] = mapped_column(
        colonne_enum(SeveriteAnomalie, "severite_anomalie"), index=True
    )
    message: Mapped[str] = mapped_column(Text)
    valeur_constatee: Mapped[str | None] = mapped_column(String(200), nullable=True)
    valeur_attendue: Mapped[str | None] = mapped_column(String(200), nullable=True)

    lot: Mapped[LotFichier] = relationship(back_populates="anomalies")

    __table_args__ = (
        CheckConstraint("ligne_numero IS NULL OR ligne_numero > 0", name="ligne_numero_positif"),
        # Un meme controle ne signale pas deux fois la meme ligne du meme lot.
        UniqueConstraint("lot_id", "ligne_numero", "code", "reference_ligne", name="uq_anomalie_unique"),
    )


class BrutCotisation(Base):
    """Ligne de bordereau de cotisations, telle que la compagnie l'a envoyee.

    Les montants sont deja en centimes : la conversion depuis les euros du fichier a lieu a la lecture,
    et une valeur qui ne se convertit pas produit une anomalie de schema au lieu d'entrer en base.
    Garder des euros ici obligerait a stocker un flottant, ce que le projet refuse partout.

    Le porteur suit celui de l'appel du coeur ERP : un contrat et son adherent en individuel, une police
    collective et son nombre d'affilies en collectif. Les quatre colonnes sont donc nullables, et c'est
    le controle de schema qui impose d'en avoir un porteur et un seul.
    """

    __tablename__ = "brut_cotisation"

    id: Mapped[int] = cle_primaire()
    lot_id: Mapped[int] = mapped_column(ForeignKey("lot_fichier.id", ondelete="CASCADE"), index=True)
    ligne_numero: Mapped[int] = mapped_column()
    reference_appel: Mapped[str] = mapped_column(String(40), index=True)
    numero_contrat: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    numero_adherent: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    numero_police: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    nombre_affilies: Mapped[int | None] = mapped_column(nullable=True)
    code_produit: Mapped[str] = mapped_column(String(32))
    periode_debut: Mapped[date] = mapped_column(Date)
    periode_fin: Mapped[date] = mapped_column(Date)
    date_echeance: Mapped[date] = mapped_column(Date, index=True)
    montant_ht_cents: Mapped[int] = centimes()
    montant_taxe_cents: Mapped[int] = centimes()
    montant_total_cents: Mapped[int] = centimes()

    lot: Mapped[LotFichier] = relationship()

    __table_args__ = (
        UniqueConstraint("lot_id", "ligne_numero", name="uq_brut_cotisation_ligne"),
        CheckConstraint("periode_fin >= periode_debut", name="periode_coherente"),
        CheckConstraint("nombre_affilies IS NULL OR nombre_affilies >= 1", name="au_moins_un_affilie"),
        # Un porteur et un seul, comme sur l'appel de cotisation du coeur ERP.
        CheckConstraint(
            "(CASE WHEN numero_contrat IS NULL THEN 0 ELSE 1 END)"
            " + (CASE WHEN numero_police IS NULL THEN 0 ELSE 1 END) = 1",
            name="porteur_unique",
        ),
    )


class BrutEncaissement(Base):
    """Ligne de releve bancaire, telle que la banque l'a envoyee.

    `reference_appel` est nullable, et c'est le coeur du rapprochement : un virement saisi a la main par
    un adherent ne porte aucune reference exploitable, et c'est exactement le cas que la correspondance
    par montant et date doit traiter.
    """

    __tablename__ = "brut_encaissement"

    id: Mapped[int] = cle_primaire()
    lot_id: Mapped[int] = mapped_column(ForeignKey("lot_fichier.id", ondelete="CASCADE"), index=True)
    ligne_numero: Mapped[int] = mapped_column()
    reference_bancaire: Mapped[str] = mapped_column(String(60), index=True)
    reference_appel: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    numero_contrat: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    #: Renseigne quand le virement vient d'une entreprise souscriptrice pour sa police collective.
    numero_police: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    date_encaissement: Mapped[date] = mapped_column(Date, index=True)
    montant_cents: Mapped[int] = centimes()
    moyen: Mapped[str] = mapped_column(String(20))
    libelle: Mapped[str | None] = mapped_column(String(200), nullable=True)

    lot: Mapped[LotFichier] = relationship()

    __table_args__ = (
        UniqueConstraint("lot_id", "ligne_numero", name="uq_brut_encaissement_ligne"),
        CheckConstraint("montant_cents <> 0", name="montant_non_nul"),
    )


class BrutPrestation(Base):
    """Ligne de bordereau de prestations, telle que la compagnie ou le concentrateur l'a envoyee."""

    __tablename__ = "brut_prestation"

    id: Mapped[int] = cle_primaire()
    lot_id: Mapped[int] = mapped_column(ForeignKey("lot_fichier.id", ondelete="CASCADE"), index=True)
    ligne_numero: Mapped[int] = mapped_column()
    reference_prestation: Mapped[str] = mapped_column(String(40), index=True)
    numero_contrat: Mapped[str] = mapped_column(String(24), index=True)
    numero_adherent: Mapped[str] = mapped_column(String(20), index=True)
    code_garantie: Mapped[str] = mapped_column(String(32), index=True)
    date_soins: Mapped[date] = mapped_column(Date, index=True)
    montant_demande_cents: Mapped[int] = centimes()
    montant_rembourse_cents: Mapped[int] = centimes()

    lot: Mapped[LotFichier] = relationship()

    __table_args__ = (
        UniqueConstraint("lot_id", "ligne_numero", name="uq_brut_prestation_ligne"),
        CheckConstraint("montant_demande_cents >= 0", name="montant_demande_positif"),
        CheckConstraint("montant_rembourse_cents >= 0", name="montant_rembourse_positif"),
    )


class RapprochementLigne(Base):
    """Verdict du rapprochement pour un mouvement bancaire.

    Pourquoi une table distincte plutot que deux colonnes de plus sur `encaissement` : le verdict porte
    des informations qui n'appartiennent pas au mouvement bancaire mais au **traitement** de ce
    mouvement, et qui doivent rester lisibles apres un nouveau passage. La methode employee, l'ecart
    constate, la fenetre de tolerance utilisee et le lot d'origine sont de celles-la. Un auditeur qui
    conteste un rapprochement lit cette ligne, pas le code.

    `encaissement_id` est nullable : une ligne de releve qui ne correspond a aucun mouvement connu du
    cabinet est elle-meme une anomalie, et elle doit apparaitre dans le rapport.
    """

    __tablename__ = "rapprochement_ligne"

    id: Mapped[int] = cle_primaire()
    lot_id: Mapped[int | None] = mapped_column(
        ForeignKey("lot_fichier.id", ondelete="SET NULL"), nullable=True, index=True
    )
    encaissement_id: Mapped[int | None] = mapped_column(
        ForeignKey("encaissement.id"), nullable=True, unique=True
    )
    reference_bancaire: Mapped[str] = mapped_column(String(60), index=True)
    appel_id: Mapped[int | None] = mapped_column(ForeignKey("appel_cotisation.id"), nullable=True, index=True)
    methode: Mapped[MethodeRapprochement] = mapped_column(
        colonne_enum(MethodeRapprochement, "methode_rapprochement")
    )
    # Reprend l'enumeration du module 1 : rapproche, partiel, ecart, non_trouve, non_rapproche.
    statut: Mapped[str] = mapped_column(String(20), index=True)
    montant_encaisse_cents: Mapped[int] = centimes()
    montant_appele_cents: Mapped[int | None] = centimes(nullable=True)
    ecart_cents: Mapped[int | None] = centimes(nullable=True)
    fenetre_jours: Mapped[int | None] = mapped_column(nullable=True)
    calcule_le: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    encaissement: Mapped[Encaissement | None] = relationship()
    appel: Mapped[AppelCotisation | None] = relationship()

    __table_args__ = (
        # Un rapprochement abouti porte necessairement l'appel retenu, et une methode qui n'est pas
        # « aucune ». L'inverse laisserait croire a un rapprochement sans contrepartie.
        CheckConstraint(
            "statut NOT IN ('rapproche', 'partiel') OR (appel_id IS NOT NULL AND methode <> 'aucune')",
            name="abouti_implique_appel",
        ),
        Index("ix_rapprochement_ligne_statut_methode", "statut", "methode"),
    )
