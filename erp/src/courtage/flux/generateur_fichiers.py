"""Generateur des fichiers recus, et de leur manifeste de verite terrain.

Ce que ce module simule : le courrier du mois. Chaque compagnie envoie son bordereau de cotisations, la
banque son releve des encaissements, et les compagnies qui delegent la gestion des prestations envoient
leur bordereau de prestations. Les fichiers sont construits a partir du portefeuille reellement genere
au module 1, donc ils sont coherents avec lui : un bordereau qui parlerait de contrats inexistants
n'exercerait pas le rapprochement, il exercerait seulement le controle de contrat inconnu.

Ensuite viennent les anomalies, et c'est la raison d'etre du module. Elles sont de deux familles, qui ne
se melangent pas :

- les **anomalies de ligne non bloquantes**, semees a hauteur de la part visee sur l'ensemble des
  lignes : un montant atypique, une prestation proche du plafond. Le lot est charge, l'anomalie est
  journalisee, un gestionnaire la regarde.
- les **anomalies bloquantes**, posees lot par lot et non tirees au hasard. Une seule par lot, parce
  qu'une anomalie bloquante rejette le lot entier : en semer au hasard reviendrait a rejeter la moitie
  du courrier, et le rapport de detection ne serait plus lisible. Chaque controle bloquant de la
  specification est exerce au moins une fois.

Pour chaque lot rejete, le generateur produit aussi le **fichier corrige** que l'emetteur renvoie, sous
un autre nom. La chaine montre ainsi la boucle complete du metier : rejet integral, renvoi, chargement.
Et un des lots sains est redeposse octet pour octet sous un autre nom, pour exercer le controle
d'empreinte.

Tout est deterministe a graine fixe : meme graine, memes fichiers, meme manifeste. La preuve est
l'empreinte SHA-256 de chaque fichier, inscrite au manifeste.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from random import Random

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from courtage.domaine.delegation import perimetre_delegue
from courtage.domaine.montants import applique_taux
from courtage.flux.enums import CodeAnomalie, FormatFichier, SeveriteAnomalie, TypeFlux
from courtage.flux.fichiers import (
    CLE_METIER,
    colonnes_totalisees,
    ecrit_csv,
    ecrit_xlsx,
    empreinte_fichier,
)
from courtage.flux.parametres import (
    ECART_TOTAL_INJECTE_CENTS,
    FACTEURS_MONTANT_ATYPIQUE,
    LOT_REDEPOSE_SOUS_AUTRE_NOM,
    LOTS_A_ANOMALIE_BLOQUANTE,
    NB_ECARTS_RELEVE,
    NB_MOUVEMENTS_NON_IDENTIFIES,
    NOM_DOSSIER_ARRIVEE,
    NOM_MANIFESTE,
    PART_LIGNES_ANORMALES,
    PART_PLAFOND_VISEE,
    PART_REGLEMENT_INSUFFISANT,
    PART_RELEVE_SANS_REFERENCE,
    PERIODES_SIMULEES,
    SUFFIXE_CORRECTION,
    SUFFIXE_REDEPOT,
    SURPLUS_ECART_CENTS,
    racine_flux,
)
from courtage.flux.referentiel import ContextePorteur, ReferentielControles, charge_referentiel
from courtage.modeles.collectif import PoliceCollective
from courtage.modeles.enums import ActeGestion
from courtage.modeles.financier import AppelCotisation, Encaissement, Prestation
from courtage.modeles.portefeuille import Adherent, Contrat
from courtage.modeles.referentiel import Compagnie, Garantie, Produit

#: Numero de contrat qui n'existe dans aucun portefeuille. Le prefixe reste celui du projet pour que
#: l'anomalie ressemble a une erreur de saisie et non a une chaine de test.
NUMERO_CONTRAT_INEXISTANT = "CT-2026-099999"
#: Numero d'adherent inexistant, construit sur le meme principe.
NUMERO_ADHERENT_INEXISTANT = "A9990001"
#: Date impossible, celle qu'un export mal configure produit reellement.
DATE_IMPOSSIBLE = "31/02/2026"
#: Montant non numerique, celui qu'un tableur ecrit quand une formule echoue.
MONTANT_NON_NUMERIQUE = "#N/A"
#: Nombre de jours avant la date d'effet du porteur ou l'anomalie de periode incoherente est placee.
JOURS_AVANT_DATE_EFFET = 45
#: Duree de la periode injectee, en jours, pour qu'elle reste coherente avec elle-meme.
JOURS_PERIODE_INJECTEE = 29
#: Nombre de virements hors perimetre de delegation ajoutes a chaque releve bancaire.
NB_VIREMENTS_HORS_DELEGATION = 6
#: Delai entre l'echeance et le virement recu a tort, en jours.
DELAI_VIREMENT_HORS_DELEGATION_JOURS = 7


@dataclass(slots=True)
class LotSimule:
    """Un fichier a ecrire : son identite, ses lignes, et les anomalies qui y ont ete injectees."""

    rang: int
    reference: str
    nom_fichier: str
    type_flux: TypeFlux
    format_fichier: FormatFichier
    periode: date
    code_compagnie: str | None
    lignes: list[dict[str, object]]
    totaux_forces: dict[str, int] | None = None
    anomalies: list[dict[str, object]] = field(default_factory=list)
    #: Vrai quand ce lot est une correction renvoyee apres rejet, ou une copie redeposse.
    derive_de: str | None = None

    def note_anomalie(
        self,
        code: CodeAnomalie,
        severite: SeveriteAnomalie,
        ligne_numero: int | None,
        reference_ligne: str | None,
        detail: str,
    ) -> None:
        self.anomalies.append(
            {
                "nom_fichier": self.nom_fichier,
                "reference_lot": self.reference,
                "ligne_numero": ligne_numero,
                "reference_ligne": reference_ligne,
                "code": code.value,
                "severite": severite.value,
                "detail": detail,
            }
        )


@dataclass(slots=True)
class ResultatGenerationFichiers:
    """Compte rendu de la generation des fichiers, enregistre dans `results/`."""

    graine: int
    dossier_arrivee: str
    manifeste: str
    lots: list[dict[str, object]]
    anomalies: list[dict[str, object]]
    lignes_totales: int
    lignes_anormales: int

    def en_dictionnaire(self) -> dict[str, object]:
        part = self.lignes_anormales / self.lignes_totales if self.lignes_totales else 0.0
        return {
            "graine": self.graine,
            "dossier_arrivee": self.dossier_arrivee,
            "manifeste": self.manifeste,
            "nombre_lots": len(self.lots),
            "lignes_totales": self.lignes_totales,
            "lignes_anormales": self.lignes_anormales,
            "part_lignes_anormales": round(part, 4),
            "anomalies_par_code": _compte_par(self.anomalies, "code"),
            "anomalies_par_severite": _compte_par(self.anomalies, "severite"),
            "lots": self.lots,
        }


def _compte_par(elements: list[dict[str, object]], cle: str) -> dict[str, int]:
    compte: dict[str, int] = defaultdict(int)
    for element in elements:
        compte[str(element[cle])] += 1
    return dict(sorted(compte.items()))


# --- Lecture du portefeuille ---------------------------------------------------------------------


def _fin_de_mois(premier_jour: date) -> date:
    if premier_jour.month == 12:
        return date(premier_jour.year, 12, 31)
    return date(premier_jour.year, premier_jour.month + 1, 1) - timedelta(days=1)


def _colonnes_appel() -> tuple:
    """Colonnes communes aux deux formes d'appel, individuelle et groupee."""
    return (
        AppelCotisation.reference_appel,
        AppelCotisation.nombre_affilies,
        AppelCotisation.periode_debut,
        AppelCotisation.periode_fin,
        AppelCotisation.date_echeance,
        AppelCotisation.montant_ht_cents,
        AppelCotisation.montant_taxe_cents,
        AppelCotisation.montant_total_cents,
        AppelCotisation.taux_taxe_bp,
    )


def _ligne_cotisation(
    porteur_contrat: str | None,
    numero_adherent: str | None,
    numero_police: str | None,
    code_produit: str,
    reste: tuple,
) -> dict[str, object]:
    """Compose une ligne de bordereau de cotisations, individuelle ou groupee."""
    (
        reference,
        nombre_affilies,
        periode_debut,
        periode_fin,
        date_echeance,
        montant_ht,
        montant_taxe,
        montant_total,
        taux_taxe_bp,
    ) = reste
    return {
        "reference_appel": reference,
        "numero_contrat": porteur_contrat,
        "numero_adherent": numero_adherent,
        "numero_police": numero_police,
        # Le nombre d'affilies n'a de sens que sur un appel groupe. En individuel, la colonne reste
        # vide : y ecrire 1 laisserait croire que le bordereau compte des affilies alors qu'il compte
        # des contrats.
        "nombre_affilies": None if numero_police is None else int(nombre_affilies or 1),
        "code_produit": code_produit,
        "periode_debut": periode_debut,
        "periode_fin": periode_fin,
        "date_echeance": date_echeance,
        "montant_ht": int(montant_ht),
        "montant_taxe": int(montant_taxe),
        "montant_total": int(montant_total),
        # Colonne technique, retiree avant ecriture : elle sert a recalculer la taxe quand
        # l'injection modifie le montant hors taxe.
        "_taux_taxe_bp": int(taux_taxe_bp),
    }


def _lignes_cotisations(session: Session, periode: date, compagnie_id: int) -> list[dict[str, object]]:
    """Lignes du bordereau de cotisations d'une compagnie pour un mois.

    Deux requetes et non une : le produit se lit sur le contrat pour un appel individuel et sur la
    police pour un appel groupe. Les reunir par une jointure conditionnelle rendrait la requete
    illisible pour un relecteur, et c'est precisement la requete qu'un expert du metier voudra relire.
    """
    debut, fin = periode, _fin_de_mois(periode)
    bornes = (AppelCotisation.date_echeance >= debut, AppelCotisation.date_echeance <= fin)

    requete_individuels = (
        select(Contrat.numero_contrat, Adherent.numero_adherent, Produit.code, *_colonnes_appel())
        .join(Contrat, Contrat.id == AppelCotisation.contrat_id)
        .join(Adherent, Adherent.id == Contrat.adherent_id)
        .join(Produit, Produit.id == Contrat.produit_id)
        .where(Produit.compagnie_id == compagnie_id, *bornes)
    )
    requete_groupes = (
        select(PoliceCollective.numero_police, Produit.code, *_colonnes_appel())
        .join(PoliceCollective, PoliceCollective.id == AppelCotisation.police_id)
        .join(Produit, Produit.id == PoliceCollective.produit_id)
        .where(Produit.compagnie_id == compagnie_id, *bornes)
    )

    lignes: list[dict[str, object]] = []
    for numero_contrat, numero_adherent, code_produit, *reste in session.execute(requete_individuels):
        lignes.append(
            _ligne_cotisation(numero_contrat, numero_adherent, None, code_produit, tuple(reste))
        )
    for numero_police, code_produit, *reste in session.execute(requete_groupes):
        lignes.append(_ligne_cotisation(None, None, numero_police, code_produit, tuple(reste)))
    lignes.sort(key=lambda ligne: str(ligne["reference_appel"]))
    return lignes


def _lignes_encaissements(session: Session, periode: date, alea: Random) -> list[dict[str, object]]:
    """Lignes du releve bancaire d'un mois : les mouvements que le cabinet a reellement recus.

    Le porteur du mouvement suit celui de l'appel : le contrat pour un reglement individuel, la police
    pour le virement unique du service comptable d'une entreprise souscriptrice.
    """
    debut, fin = periode, _fin_de_mois(periode)
    requete = (
        select(
            Encaissement.reference_bancaire,
            Encaissement.date_encaissement,
            Encaissement.libelle_releve,
            AppelCotisation.reference_appel,
            Contrat.numero_contrat,
            PoliceCollective.numero_police,
            Encaissement.moyen,
            Encaissement.montant_cents,
        )
        .join(AppelCotisation, AppelCotisation.id == Encaissement.appel_id, isouter=True)
        .join(Contrat, Contrat.id == Encaissement.contrat_id, isouter=True)
        .join(PoliceCollective, PoliceCollective.id == AppelCotisation.police_id, isouter=True)
        .where(Encaissement.date_encaissement >= debut, Encaissement.date_encaissement <= fin)
        .order_by(Encaissement.reference_bancaire)
    )
    lignes: list[dict[str, object]] = []
    for (
        reference,
        date_operation,
        libelle,
        reference_appel,
        numero_contrat,
        numero_police,
        moyen,
        montant,
    ) in session.execute(requete):
        lignes.append(
            {
                "reference_bancaire": reference,
                "date_operation": date_operation,
                "libelle": libelle or "",
                "reference_appel": reference_appel,
                "numero_contrat": numero_contrat,
                "numero_police": numero_police,
                "moyen": moyen.value if hasattr(moyen, "value") else str(moyen),
                "montant": int(montant),
            }
        )
    _efface_des_references(lignes, alea)
    lignes.extend(_lignes_encaissements_non_deleguees(session, periode))
    lignes.extend(_lignes_ecarts_de_reglement(session, periode))
    lignes.extend(_mouvements_non_identifies(periode, alea))
    lignes.sort(key=lambda ligne: str(ligne["reference_bancaire"]))
    return lignes


def _efface_des_references(lignes: list[dict[str, object]], alea: Random) -> None:
    """Retire la reference d'appel d'une part des mouvements.

    Le payeur qui fait un virement a la main ne reprend pas la reference de sa quittance. Son mouvement
    arrive donc au releve avec un libelle, un montant et une date, et rien d'autre. C'est le cas que la
    correspondance par montant et date existe pour traiter, et sans ces lignes cette seconde methode ne
    serait jamais exercee : le rapprochement afficherait cent pour cent de correspondances par
    reference, ce qui ne demontrerait que la moitie de la regle demandee.
    """
    eligibles = [ligne for ligne in lignes if ligne.get("reference_appel")]
    nombre = round(len(eligibles) * PART_RELEVE_SANS_REFERENCE)
    for ligne in alea.sample(eligibles, min(nombre, len(eligibles))):
        ligne["reference_appel"] = None


def _lignes_ecarts_de_reglement(session: Session, periode: date) -> list[dict[str, object]]:
    """Reglements d'un montant different de l'appel : trop percu, ou versement nettement insuffisant.

    Ces mouvements existent et ne sont pas des erreurs de fichier : un payeur arrondit, un autre regle
    une partie seulement. Ils doivent ressortir avec le statut « ecart » et alimenter l'ecran des
    anomalies, et non etre ranges parmi les lots fautifs.
    """
    debut, fin = periode, _fin_de_mois(periode)
    requete = (
        select(
            AppelCotisation.reference_appel,
            Contrat.numero_contrat,
            AppelCotisation.date_echeance,
            AppelCotisation.montant_total_cents,
        )
        .join(Contrat, Contrat.id == AppelCotisation.contrat_id)
        .where(AppelCotisation.date_echeance >= debut, AppelCotisation.date_echeance <= fin)
        .order_by(AppelCotisation.reference_appel.desc())
        .limit(NB_ECARTS_RELEVE)
    )
    lignes: list[dict[str, object]] = []
    for rang, (reference_appel, numero_contrat, echeance, montant) in enumerate(
        session.execute(requete), start=1
    ):
        trop_percu = rang % 2 == 1
        if trop_percu:
            montant_verse = int(montant) + SURPLUS_ECART_CENTS
            libelle = f"REGLEMENT ARRONDI {numero_contrat}"
        else:
            montant_verse = max(1, round(int(montant) * PART_REGLEMENT_INSUFFISANT))
            libelle = f"ACOMPTE PARTIEL {numero_contrat}"
        lignes.append(
            {
                "reference_bancaire": f"ENC-ECART-{periode:%Y%m}-{rang:03d}",
                "date_operation": echeance + timedelta(days=DELAI_VIREMENT_HORS_DELEGATION_JOURS),
                "libelle": libelle,
                "reference_appel": reference_appel,
                "numero_contrat": numero_contrat,
                "numero_police": None,
                "moyen": "virement",
                "montant": montant_verse,
            }
        )
    return lignes


def _mouvements_non_identifies(periode: date, alea: Random) -> list[dict[str, object]]:
    """Mouvements inattribuables : ni reference, ni porteur, aucun montant correspondant.

    Tout releve bancaire en contient. Ils doivent ressortir « non trouve » et rester visibles : un
    mouvement credite que personne ne sait rattacher est un probleme de tresorerie, pas une ligne a
    oublier.
    """
    lignes: list[dict[str, object]] = []
    for rang in range(1, NB_MOUVEMENTS_NON_IDENTIFIES + 1):
        lignes.append(
            {
                "reference_bancaire": f"ENC-NONID-{periode:%Y%m}-{rang:03d}",
                "date_operation": periode + timedelta(days=alea.randrange(1, 28)),
                "libelle": "VIREMENT RECU SANS LIBELLE EXPLOITABLE",
                "reference_appel": None,
                "numero_contrat": None,
                "numero_police": None,
                "moyen": "virement",
                "montant": alea.randrange(1_000, 99_900, 100),
            }
        )
    return lignes


def _produits_sans_delegation_encaissement(session: Session, a_la_date: date) -> list[int]:
    """Produits dont la compagnie n'a pas delegue l'encaissement au cabinet, a une date donnee."""
    sans: list[int] = []
    for produit_id in session.scalars(select(Produit.id).order_by(Produit.id)):
        perimetre = perimetre_delegue(session, produit_id, a_la_date)
        if not perimetre.autorise(ActeGestion.ENCAISSEMENT):
            sans.append(produit_id)
    return sans


def _lignes_encaissements_non_deleguees(session: Session, periode: date) -> list[dict[str, object]]:
    """Virements recus a tort, sur des produits dont le cabinet n'encaisse pas.

    Le cas est courant et il est instructif : la compagnie preleve elle-meme, mais un adherent paie le
    cabinet, par habitude ou parce qu'il a repris un ancien mandat. Le mouvement figure donc au releve.
    La chaine doit le voir et **refuser de le traiter**, parce que l'encaissement n'est pas un acte
    delegue sur ce produit. C'est la demonstration la plus directe de la delegation de gestion dans les
    flux financiers, et aucune anomalie de fichier n'en rend compte : ce n'est pas le fichier qui est
    fautif, c'est l'acte qui est hors perimetre.
    """
    debut, fin = periode, _fin_de_mois(periode)
    produits = _produits_sans_delegation_encaissement(session, periode)
    if not produits:
        return []
    requete = (
        select(
            AppelCotisation.reference_appel,
            Contrat.numero_contrat,
            AppelCotisation.date_echeance,
            AppelCotisation.montant_total_cents,
        )
        .join(Contrat, Contrat.id == AppelCotisation.contrat_id)
        .where(
            Contrat.produit_id.in_(produits),
            AppelCotisation.date_echeance >= debut,
            AppelCotisation.date_echeance <= fin,
        )
        .order_by(AppelCotisation.reference_appel)
        .limit(NB_VIREMENTS_HORS_DELEGATION)
    )
    lignes: list[dict[str, object]] = []
    for rang, (reference_appel, numero_contrat, echeance, montant) in enumerate(
        session.execute(requete), start=1
    ):
        lignes.append(
            {
                "reference_bancaire": f"ENC-HORSDEL-{periode:%Y%m}-{rang:03d}",
                "date_operation": echeance + timedelta(days=DELAI_VIREMENT_HORS_DELEGATION_JOURS),
                "libelle": f"VIREMENT ADHERENT {numero_contrat}",
                "reference_appel": reference_appel,
                "numero_contrat": numero_contrat,
                "numero_police": None,
                "moyen": "virement",
                "montant": int(montant),
            }
        )
    return lignes


def _lignes_prestations(session: Session, periode: date, compagnie_id: int) -> list[dict[str, object]]:
    debut, fin = periode, _fin_de_mois(periode)
    requete = (
        select(
            Prestation.reference_prestation,
            Contrat.numero_contrat,
            Adherent.numero_adherent,
            Garantie.code,
            Prestation.date_soins,
            Prestation.montant_demande_cents,
            Prestation.montant_rembourse_cents,
        )
        .join(Contrat, Contrat.id == Prestation.contrat_id)
        .join(Adherent, Adherent.id == Prestation.adherent_id)
        .join(Garantie, Garantie.id == Prestation.garantie_id)
        .join(Produit, Produit.id == Contrat.produit_id)
        .where(
            Produit.compagnie_id == compagnie_id,
            Prestation.date_soins >= debut,
            Prestation.date_soins <= fin,
        )
        .order_by(Prestation.reference_prestation)
    )
    return [
        {
            "reference_prestation": reference,
            "numero_contrat": numero_contrat,
            "numero_adherent": numero_adherent,
            "code_garantie": code_garantie,
            "date_soins": date_soins,
            "montant_demande": int(demande),
            "montant_rembourse": int(rembourse),
        }
        for (
            reference,
            numero_contrat,
            numero_adherent,
            code_garantie,
            date_soins,
            demande,
            rembourse,
        ) in session.execute(requete)
    ]


# --- Construction des lots -----------------------------------------------------------------------


def _nom_fichier(type_flux: TypeFlux, code_emetteur: str, periode: date, format_fichier: FormatFichier) -> str:
    return f"{type_flux.value.upper()}_{code_emetteur}_{periode:%Y%m}.{format_fichier.value}"


def _construit_lots(session: Session, alea: Random) -> list[LotSimule]:
    """Construit les dix-huit lots reguliers, dans l'ordre que les rangs des anomalies supposent."""
    compagnies = list(session.execute(select(Compagnie.id, Compagnie.code).order_by(Compagnie.code)))
    lots: list[LotSimule] = []
    rang = 0
    for periode in PERIODES_SIMULEES:
        for position, (compagnie_id, code) in enumerate(compagnies):
            lignes = _lignes_cotisations(session, periode, compagnie_id)
            if not lignes:
                continue
            rang += 1
            # La premiere compagnie envoie son bordereau en XLSX, les autres en CSV. La specification
            # exige les deux formats, et un cabinet les recoit effectivement melanges.
            format_fichier = FormatFichier.XLSX if position == 0 else FormatFichier.CSV
            nom = _nom_fichier(TypeFlux.COTISATIONS, code, periode, format_fichier)
            lots.append(
                LotSimule(
                    rang=rang,
                    reference=Path(nom).stem,
                    nom_fichier=nom,
                    type_flux=TypeFlux.COTISATIONS,
                    format_fichier=format_fichier,
                    periode=periode,
                    code_compagnie=code,
                    lignes=lignes,
                )
            )

        lignes_banque = _lignes_encaissements(session, periode, alea)
        if lignes_banque:
            rang += 1
            nom = _nom_fichier(TypeFlux.ENCAISSEMENTS, "BANQUE", periode, FormatFichier.CSV)
            lots.append(
                LotSimule(
                    rang=rang,
                    reference=Path(nom).stem,
                    nom_fichier=nom,
                    type_flux=TypeFlux.ENCAISSEMENTS,
                    format_fichier=FormatFichier.CSV,
                    periode=periode,
                    code_compagnie=None,
                    lignes=lignes_banque,
                )
            )

        for compagnie_id, code in compagnies:
            lignes = _lignes_prestations(session, periode, compagnie_id)
            if not lignes:
                continue
            rang += 1
            nom = _nom_fichier(TypeFlux.PRESTATIONS, code, periode, FormatFichier.CSV)
            lots.append(
                LotSimule(
                    rang=rang,
                    reference=Path(nom).stem,
                    nom_fichier=nom,
                    type_flux=TypeFlux.PRESTATIONS,
                    format_fichier=FormatFichier.CSV,
                    periode=periode,
                    code_compagnie=code,
                    lignes=lignes,
                )
            )
    return lots


# --- Injection des anomalies ---------------------------------------------------------------------


def _injecte_montant_atypique(lot: LotSimule, ligne: dict[str, object], alea: Random, rang_ligne: int) -> bool:
    """Multiplie le montant hors taxe par un facteur franchement anormal, et recalcule la taxe.

    Recalculer la taxe est indispensable : laisser l'ancienne romprait la coherence interne de la ligne,
    et le lot serait rejete pour une autre raison que celle que l'on voulait injecter.
    """
    facteur = alea.choice(FACTEURS_MONTANT_ATYPIQUE)
    ancien_ht = int(ligne["montant_ht"])
    nouveau_ht = max(1, round(ancien_ht * facteur))
    taux = int(ligne["_taux_taxe_bp"])
    ligne["montant_ht"] = nouveau_ht
    ligne["montant_taxe"] = applique_taux(nouveau_ht, taux)
    ligne["montant_total"] = nouveau_ht + int(ligne["montant_taxe"])
    lot.note_anomalie(
        CodeAnomalie.MONTANT_ATYPIQUE,
        SeveriteAnomalie.AVERTISSEMENT,
        rang_ligne,
        str(ligne[CLE_METIER[lot.type_flux]]),
        f"montant hors taxe porte de {ancien_ht} a {nouveau_ht} centimes, facteur {facteur}",
    )
    return True


def _injecte_proche_plafond(
    lot: LotSimule, ligne: dict[str, object], referentiel: ReferentielControles, rang_ligne: int
) -> bool:
    """Porte la consommation annuelle de l'adherent a une part elevee du plafond de la garantie.

    Le remboursement injecte est calcule **a partir de la consommation deja enregistree** avant la date
    des soins, et non fixe a l'aveugle. C'est ce qui rend le manifeste vrai : une anomalie annoncee doit
    reellement satisfaire la definition du controle, sinon le rappel mesure est faux. Si la consommation
    anterieure depasse deja la cible, la ligne ne peut pas franchir le seuil et aucune anomalie n'est
    annoncee.
    """
    numero_contrat = str(ligne["numero_contrat"])
    code_garantie = str(ligne["code_garantie"])
    reference = str(ligne["reference_prestation"])
    plafond = referentiel.plafond(numero_contrat, code_garantie)
    if plafond is None or plafond <= 0:
        return False
    jour = ligne["date_soins"]
    deja = referentiel.cumul_avant(numero_contrat, code_garantie, jour, reference)  # type: ignore[arg-type]
    cible = round(plafond * PART_PLAFOND_VISEE)
    rembourse = cible - deja
    if rembourse <= 0:
        return False
    ligne["montant_rembourse"] = rembourse
    ligne["montant_demande"] = max(int(ligne["montant_demande"]), rembourse)
    lot.note_anomalie(
        CodeAnomalie.PRESTATION_PROCHE_PLAFOND,
        SeveriteAnomalie.AVERTISSEMENT,
        rang_ligne,
        reference,
        f"consommation annuelle portee de {deja} a {cible} centimes, pour un plafond de {plafond}",
    )
    return True


def _injecte_anomalies_non_bloquantes(
    lots: list[LotSimule], alea: Random, referentiel: ReferentielControles
) -> None:
    """Seme les anomalies non bloquantes pour atteindre la part visee sur l'ensemble des lignes.

    Les lignes eligibles sont celles des bordereaux de cotisations et de prestations. Un releve bancaire
    n'en recoit pas : les deux controles non bloquants de la specification portent sur un montant de
    cotisation et sur une prestation, et inventer une anomalie bancaire pour faire du volume donnerait un
    taux de detection flatteur sur un controle qui n'existe pas.
    """
    lignes_totales = sum(len(lot.lignes) for lot in lots)
    cible = round(lignes_totales * PART_LIGNES_ANORMALES)
    eligibles = [lot for lot in lots if lot.type_flux is not TypeFlux.ENCAISSEMENTS]
    lignes_eligibles = sum(len(lot.lignes) for lot in eligibles)
    if not lignes_eligibles or cible <= 0:
        return

    injectees = 0
    for lot in eligibles:
        # Repartition proportionnelle au poids du lot dans les lignes eligibles, pour que chaque
        # bordereau porte sa part d'anomalies au lieu de les concentrer sur le premier.
        quota = round(cible * len(lot.lignes) / lignes_eligibles)
        if quota <= 0:
            continue
        rangs = alea.sample(range(len(lot.lignes)), min(quota, len(lot.lignes)))
        for position in sorted(rangs):
            ligne = lot.lignes[position]
            rang_ligne = position + 1
            if lot.type_flux is TypeFlux.COTISATIONS:
                pose = _injecte_montant_atypique(lot, ligne, alea, rang_ligne)
            else:
                pose = _injecte_proche_plafond(lot, ligne, referentiel, rang_ligne)
            if pose:
                injectees += 1
    # Le compte reel est recalcule au manifeste : il peut differer de la cible d'une unite par lot a
    # cause des arrondis, et d'une ligne de prestation sans plafond saisi.


def _candidats_hors_delegation(
    session: Session, referentiel: ReferentielControles, periode: date
) -> list[tuple[str, str, str, str]]:
    """Contrats dont le produit n'a plus le quittancement delegue sur la periode donnee.

    Rend des quadruplets numero de contrat, numero d'adherent, code du produit et code de la compagnie.
    Les candidats sont restreints aux contrats couverts sur toute la periode, pour que l'anomalie
    injectee soit **uniquement** un refus de delegation : si le contrat etait resilie avant la periode,
    le controle de periode incoherente se declencherait aussi et le manifeste annoncerait une anomalie
    pour une autre.
    """
    debut, fin = periode, _fin_de_mois(periode)
    produits_hors = [
        produit_id
        for produit_id in sorted(set(referentiel.produit_par_code.values()))
        if ActeGestion.QUITTANCEMENT not in referentiel.actes_delegues(produit_id, debut)
    ]
    if not produits_hors:
        return []
    requete = (
        select(Contrat.numero_contrat, Adherent.numero_adherent, Produit.code, Compagnie.code)
        .join(Adherent, Adherent.id == Contrat.adherent_id)
        .join(Produit, Produit.id == Contrat.produit_id)
        .join(Compagnie, Compagnie.id == Produit.compagnie_id)
        .where(
            Contrat.produit_id.in_(produits_hors),
            Contrat.date_effet.is_not(None),
            Contrat.date_effet <= debut,
            or_(Contrat.date_resiliation.is_(None), Contrat.date_resiliation > fin),
        )
        .order_by(Contrat.numero_contrat)
        .limit(50)
    )
    return [tuple(ligne) for ligne in session.execute(requete)]  # type: ignore[misc]


def _porteur_de_la_ligne(ligne: dict[str, object]) -> str | None:
    contrat = ligne.get("numero_contrat")
    police = ligne.get("numero_police")
    return str(contrat) if contrat else (str(police) if police else None)


def _choisit_ligne_datee(
    lot: LotSimule, alea: Random, contextes: dict[str, ContextePorteur]
) -> int | None:
    """Position d'une ligne dont le porteur a une date d'effet connue.

    Necessaire pour l'anomalie de periode incoherente : elle doit etre construite **a partir** de la
    date d'effet du porteur, sinon un decalage fixe tombe parfois encore dans la periode de garantie et
    l'anomalie annoncee au manifeste n'existe pas dans le fichier. Le manifeste mentirait, et le rappel
    mesure deviendrait faux.
    """
    positions = [
        rang
        for rang, ligne in enumerate(lot.lignes)
        if (porteur := _porteur_de_la_ligne(ligne))
        and porteur in contextes
        and contextes[porteur].date_effet is not None
    ]
    return alea.choice(positions) if positions else None


def _injecte_anomalie_bloquante(
    lot: LotSimule,
    code: CodeAnomalie,
    variante: str,
    alea: Random,
    contextes: dict[str, ContextePorteur],
    candidats_hors_delegation: list[tuple[str, str, str, str]],
) -> None:
    """Pose une et une seule anomalie bloquante sur le lot, selon le code et la variante demandes."""
    if not lot.lignes:
        return
    position = alea.randrange(len(lot.lignes))
    if code is CodeAnomalie.PERIODE_INCOHERENTE:
        choix = _choisit_ligne_datee(lot, alea, contextes)
        if choix is None:
            return
        position = choix
    ligne = lot.lignes[position]
    rang_ligne = position + 1
    cle_metier = CLE_METIER[lot.type_flux]
    reference = str(ligne[cle_metier])

    if code is CodeAnomalie.CONTRAT_INCONNU:
        ancien = str(ligne["numero_contrat"])
        ligne["numero_contrat"] = NUMERO_CONTRAT_INEXISTANT
        lot.note_anomalie(
            code,
            SeveriteAnomalie.BLOQUANT,
            rang_ligne,
            reference,
            f"numero de contrat {ancien} remplace par {NUMERO_CONTRAT_INEXISTANT}, inexistant",
        )
        return

    if code is CodeAnomalie.ADHERENT_INCONNU:
        ancien = str(ligne["numero_adherent"])
        ligne["numero_adherent"] = NUMERO_ADHERENT_INEXISTANT
        lot.note_anomalie(
            code,
            SeveriteAnomalie.BLOQUANT,
            rang_ligne,
            reference,
            f"numero d'adherent {ancien} remplace par {NUMERO_ADHERENT_INEXISTANT}, inexistant",
        )
        return

    if code is CodeAnomalie.DOUBLON_CLE_METIER:
        if len(lot.lignes) < 2:
            return
        autre = lot.lignes[(position + 1) % len(lot.lignes)]
        cle_dupliquee = str(autre[cle_metier])
        ligne[cle_metier] = cle_dupliquee
        lot.note_anomalie(
            code,
            SeveriteAnomalie.BLOQUANT,
            rang_ligne,
            cle_dupliquee,
            f"cle metier {reference} remplacee par {cle_dupliquee}, deja presente dans le fichier",
        )
        return

    if code is CodeAnomalie.TOTAL_INCOHERENT:
        colonnes = colonnes_totalisees(lot.type_flux)
        colonne = colonnes[-1]
        somme = sum(int(autre[colonne]) for autre in lot.lignes)
        lot.totaux_forces = {colonne: somme + ECART_TOTAL_INJECTE_CENTS}
        lot.note_anomalie(
            code,
            SeveriteAnomalie.BLOQUANT,
            None,
            None,
            f"ligne de total de la colonne {colonne} majoree de {ECART_TOTAL_INJECTE_CENTS} centimes",
        )
        return

    if code is CodeAnomalie.PERIODE_INCOHERENTE:
        porteur = _porteur_de_la_ligne(ligne)
        contexte = contextes.get(porteur) if porteur else None
        if contexte is None or contexte.date_effet is None:
            return
        # La periode est construite a partir de la date d'effet, et placee juste avant elle. Un
        # decalage fixe ne garantirait rien : sur un porteur ancien, reculer de deux ans reste dans la
        # periode de garantie, et l'anomalie annoncee n'existerait pas.
        avant_effet = contexte.date_effet - timedelta(days=JOURS_AVANT_DATE_EFFET)
        if lot.type_flux is TypeFlux.PRESTATIONS:
            ancienne = ligne["date_soins"]
            ligne["date_soins"] = avant_effet
            detail = f"date de soins reportee du {ancienne} au {avant_effet}"
        else:
            ancienne = ligne["periode_debut"]
            ligne["periode_debut"] = avant_effet
            ligne["periode_fin"] = avant_effet + timedelta(days=JOURS_PERIODE_INJECTEE)
            ligne["date_echeance"] = avant_effet
            detail = f"periode reportee du {ancienne} au {avant_effet}"
        detail += f", pour un porteur {porteur} dont la date d'effet est le {contexte.date_effet}"
        lot.note_anomalie(code, SeveriteAnomalie.BLOQUANT, rang_ligne, reference, detail)
        return

    if code is CodeAnomalie.ACTE_NON_DELEGUE:
        # Le bordereau d'une compagnie dont la convention est echue : elle continue d'envoyer, le
        # cabinet n'a plus le droit de quittancer. La ligne est reaffectee a un contrat reel porte par
        # un produit hors mandat, et toutes ses autres colonnes restent coherentes pour que le lot ne
        # soit rejete que pour cette seule raison.
        prefere = [candidat for candidat in candidats_hors_delegation if candidat[3] == lot.code_compagnie]
        retenus = prefere or candidats_hors_delegation
        if not retenus:
            return
        numero_contrat, numero_adherent, code_produit, _code_compagnie = retenus[0]
        ligne["numero_contrat"] = numero_contrat
        ligne["numero_adherent"] = numero_adherent
        ligne["numero_police"] = None
        ligne["nombre_affilies"] = None
        ligne["code_produit"] = code_produit
        lot.note_anomalie(
            code,
            SeveriteAnomalie.BLOQUANT,
            rang_ligne,
            reference,
            f"ligne reaffectee au contrat {numero_contrat}, porte par le produit {code_produit} dont"
            f" le quittancement n'est plus delegue sur la periode {lot.periode}",
        )
        return

    if code is CodeAnomalie.SCHEMA_INVALIDE:
        if variante == "montant_non_numerique":
            colonne = colonnes_totalisees(lot.type_flux)[0]
            ligne[colonne] = MONTANT_NON_NUMERIQUE
            detail = f"colonne {colonne} remplacee par {MONTANT_NON_NUMERIQUE}"
        else:
            colonne = "date_soins" if lot.type_flux is TypeFlux.PRESTATIONS else "date_echeance"
            if lot.type_flux is TypeFlux.ENCAISSEMENTS:
                colonne = "date_operation"
            ligne[colonne] = DATE_IMPOSSIBLE
            detail = f"colonne {colonne} remplacee par la date impossible {DATE_IMPOSSIBLE}"
        lot.note_anomalie(code, SeveriteAnomalie.BLOQUANT, rang_ligne, reference, detail)
        return

    raise ValueError(f"code d'anomalie bloquante non pris en charge par le generateur : {code}")


# --- Ecriture ------------------------------------------------------------------------------------


def _ecrit_lot(lot: LotSimule, dossier: Path) -> dict[str, object]:
    """Ecrit le fichier d'un lot et rend sa ligne de manifeste."""
    chemin = dossier / lot.nom_fichier
    # La colonne technique du taux de taxe ne doit pas sortir dans le fichier.
    lignes = [{cle: valeur for cle, valeur in ligne.items() if not cle.startswith("_")} for ligne in lot.lignes]
    if lot.format_fichier is FormatFichier.XLSX:
        ecrit_xlsx(chemin, lot.type_flux, lignes, lot.totaux_forces)
    else:
        ecrit_csv(chemin, lot.type_flux, lignes, lot.totaux_forces)
    empreinte = empreinte_fichier(chemin)
    return {
        "rang": lot.rang,
        # Meme convention que la chaine d'ingestion : nom du fichier, puis debut de l'empreinte.
        "reference_lot": f"{Path(lot.nom_fichier).stem}-{empreinte[:8]}",
        "nom_fichier": lot.nom_fichier,
        "type_flux": lot.type_flux.value,
        "format": lot.format_fichier.value,
        "periode": lot.periode.isoformat(),
        "code_compagnie": lot.code_compagnie,
        "nombre_lignes": len(lot.lignes),
        "derive_de": lot.derive_de,
        "sha256": empreinte,
        "taille_octets": chemin.stat().st_size,
    }


def _copie_pour_correction(lot: LotSimule) -> LotSimule:
    """Duplique un lot sans son anomalie bloquante : le fichier que l'emetteur renvoie corrige."""
    suffixe = f".{lot.format_fichier.value}"
    stem = lot.nom_fichier[: -len(suffixe)]
    nom = f"{stem}{SUFFIXE_CORRECTION}{suffixe}"
    return LotSimule(
        rang=lot.rang,
        reference=Path(nom).stem,
        nom_fichier=nom,
        type_flux=lot.type_flux,
        format_fichier=lot.format_fichier,
        periode=lot.periode,
        code_compagnie=lot.code_compagnie,
        lignes=[dict(ligne) for ligne in lot.lignes],
        totaux_forces=None,
        derive_de=lot.reference,
    )


def genere_fichiers(
    session: Session, graine: int, dossier: Path | None = None, vide_le_dossier: bool = True
) -> ResultatGenerationFichiers:
    """Produit les fichiers recus et le manifeste de verite terrain.

    `vide_le_dossier` supprime les fichiers de flux presents dans le dossier d'arrivee avant d'ecrire,
    pour qu'une seconde generation ne laisse pas cohabiter deux versions du meme bordereau. Seuls les
    fichiers de flux sont supprimes, jamais autre chose.
    """
    racine = racine_flux()
    dossier_arrivee = dossier or racine / NOM_DOSSIER_ARRIVEE
    dossier_arrivee.mkdir(parents=True, exist_ok=True)
    if vide_le_dossier:
        for ancien in sorted(dossier_arrivee.iterdir()):
            if ancien.is_file() and ancien.suffix.lower() in (".csv", ".xlsx"):
                ancien.unlink()

    alea = Random(graine)
    referentiel = charge_referentiel(session)
    contextes = referentiel.contrats | referentiel.polices

    lots = _construit_lots(session, alea)
    _injecte_anomalies_non_bloquantes(lots, alea, referentiel)

    # Les corrections sont construites **avant** l'injection bloquante, pour qu'elles portent les
    # anomalies non bloquantes du lot d'origine et aucune des bloquantes.
    candidats_par_periode = {
        periode: _candidats_hors_delegation(session, referentiel, periode)
        for periode in PERIODES_SIMULEES
    }

    corrections: list[LotSimule] = []
    for lot in lots:
        consigne = LOTS_A_ANOMALIE_BLOQUANTE.get(lot.rang)
        if consigne is None:
            continue
        correction = _copie_pour_correction(lot)
        for anomalie in lot.anomalies:
            correction.anomalies.append({**anomalie, "nom_fichier": correction.nom_fichier,
                                         "reference_lot": correction.reference})
        corrections.append(correction)
        code, variante = consigne
        _injecte_anomalie_bloquante(
            lot, code, variante, alea, contextes, candidats_par_periode.get(lot.periode, [])
        )

    lignes_manifeste: list[dict[str, object]] = []
    anomalies: list[dict[str, object]] = []
    for lot in [*lots, *corrections]:
        lignes_manifeste.append(_ecrit_lot(lot, dossier_arrivee))
        anomalies.extend(lot.anomalies)

    # Redepot d'un lot sain sous un autre nom, octet pour octet : le controle d'empreinte doit le voir.
    origine = next((lot for lot in lots if lot.rang == LOT_REDEPOSE_SOUS_AUTRE_NOM), None)
    if origine is not None:
        suffixe = f".{origine.format_fichier.value}"
        stem = origine.nom_fichier[: -len(suffixe)]
        nom_copie = f"{stem}{SUFFIXE_REDEPOT}{suffixe}"
        chemin_origine = dossier_arrivee / origine.nom_fichier
        chemin_copie = dossier_arrivee / nom_copie
        chemin_copie.write_bytes(chemin_origine.read_bytes())
        lignes_manifeste.append(
            {
                "rang": origine.rang,
                "reference_lot": f"{Path(nom_copie).stem}-{empreinte_fichier(chemin_copie)[:8]}",
                "nom_fichier": nom_copie,
                "type_flux": origine.type_flux.value,
                "format": origine.format_fichier.value,
                "periode": origine.periode.isoformat(),
                "code_compagnie": origine.code_compagnie,
                "nombre_lignes": len(origine.lignes),
                "derive_de": origine.reference,
                "sha256": empreinte_fichier(chemin_copie),
                "taille_octets": chemin_copie.stat().st_size,
            }
        )
        # La copie porte les memes lignes que son original, donc les memes anomalies de ligne. Les
        # omettre du manifeste ferait passer pour des faux positifs des detections exactes.
        for anomalie in origine.anomalies:
            anomalies.append(
                {**anomalie, "nom_fichier": nom_copie, "reference_lot": Path(nom_copie).stem}
            )
        anomalies.append(
            {
                "nom_fichier": nom_copie,
                "reference_lot": Path(nom_copie).stem,
                "ligne_numero": None,
                "reference_ligne": None,
                "code": CodeAnomalie.FICHIER_DEJA_CHARGE.value,
                "severite": SeveriteAnomalie.BLOQUANT.value,
                "detail": f"copie octet pour octet de {origine.nom_fichier}, meme empreinte SHA-256",
            }
        )

    lignes_totales = sum(int(ligne["nombre_lignes"]) for ligne in lignes_manifeste)
    lignes_anormales = len({(a["nom_fichier"], a["ligne_numero"]) for a in anomalies if a["ligne_numero"]})

    manifeste = {
        "graine": graine,
        "part_visee_lignes_anormales": PART_LIGNES_ANORMALES,
        "lignes_totales": lignes_totales,
        "lignes_anormales": lignes_anormales,
        "part_lignes_anormales": round(lignes_anormales / lignes_totales, 4) if lignes_totales else 0.0,
        "lots": lignes_manifeste,
        "anomalies": anomalies,
    }
    chemin_manifeste = racine / NOM_MANIFESTE
    chemin_manifeste.parent.mkdir(parents=True, exist_ok=True)
    chemin_manifeste.write_text(
        json.dumps(manifeste, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    return ResultatGenerationFichiers(
        graine=graine,
        dossier_arrivee=str(dossier_arrivee),
        manifeste=str(chemin_manifeste),
        lots=lignes_manifeste,
        anomalies=anomalies,
        lignes_totales=lignes_totales,
        lignes_anormales=lignes_anormales,
    )
