"""Rapprochement des cotisations appelees et des encaissements : d'abord la reference, puis le montant.

Le probleme, en une phrase de gestionnaire : le cabinet a quittance des cotisations, la banque lui envoie
des mouvements, et il faut dire pour chaque mouvement quelle quittance il regle. Quand le payeur a
conserve la reference de l'appel, c'est immediat. Quand il a saisi un virement a la main, il faut
retrouver l'appel par son montant et sa date, et le resultat n'est plus une certitude mais une
presomption. Les deux cas existent et ils ne meritent pas la meme confiance, c'est pourquoi la methode
employee est conservee ligne a ligne.

Ordre impose par la specification, et respecte ici :

1. **par reference** : la reference d'appel portee par la ligne de releve designe un appel connu ;
2. **par montant et date** : a defaut, parmi les appels du meme contrat, ceux dont la date d'echeance est
   dans la fenetre de tolerance et dont le montant correspond. Plusieurs candidats sont possibles sur un
   contrat mensuel a cotisation constante : l'echeance la plus proche de la date du mouvement l'emporte,
   et le plus petit identifiant tranche une egalite, pour que deux executions donnent le meme resultat.

Statuts rendus, les quatre de la specification :

| Statut | Quand |
| --- | --- |
| `rapproche` | un appel trouve, montant encaisse egal au montant appele |
| `partiel` | un appel trouve, montant encaisse inferieur, mais au-dessus de la part minimale |
| `ecart` | un appel trouve, montant encaisse superieur, ou trop faible pour etre un reglement partiel |
| `non_trouve` | aucun appel ne correspond, ni par reference ni par montant et date |

**La delegation de gestion est opposee avant tout traitement.** Un mouvement rattache a un contrat dont
la compagnie n'a pas delegue l'encaissement n'est pas rapproche, meme si le fichier le contient et meme
si un appel correspondrait. Le cabinet n'encaisse pas pour cette compagnie : prononcer un rapprochement
reviendrait a enregistrer un acte qu'il n'a pas le droit d'accomplir. Ces lignes sont comptees a part et
figurent au journal d'execution.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from courtage.domaine.delegation import perimetre_delegue
from courtage.flux.enums import MethodeRapprochement, StatutLot
from courtage.flux.modeles import BrutEncaissement, LotFichier, RapprochementLigne
from courtage.flux.parametres import (
    FENETRE_RAPPROCHEMENT_JOURS,
    PART_MINIMALE_PARTIEL,
    TOLERANCE_RAPPROCHEMENT_CENTS,
)
from courtage.modeles.collectif import PoliceCollective
from courtage.modeles.enums import ActeGestion, StatutAppel, StatutRapprochement
from courtage.modeles.financier import AppelCotisation, Encaissement
from courtage.modeles.portefeuille import Contrat


@dataclass(slots=True)
class _AppelConnu:
    """Appel de cotisation du portefeuille, charge en memoire pour le rapprochement.

    `numero_porteur` est le numero de contrat pour un appel individuel et le numero de police pour un
    appel groupe. Le rapprochement n'a pas besoin de savoir lequel : il cherche les appels du meme
    porteur que la ligne de releve, et la ligne de releve designe son porteur de la meme facon.
    """

    identifiant: int
    reference: str
    numero_porteur: str
    produit_id: int
    date_echeance: date
    montant_total_cents: int


@dataclass(slots=True)
class ResultatRapprochement:
    """Compte rendu du rapprochement, pour le journal d'execution et pour la sortie de la commande."""

    fenetre_jours: int
    lignes_examinees: int = 0
    par_statut: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    #: Appels dont le statut a ete rededuit parce que leur rattachement bancaire a change.
    appels_reexamines: int = 0
    par_methode: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    actes_non_delegues: int = 0
    mouvements_inconnus_erp: int = 0
    ecart_total_cents: int = 0
    lots: list[str] = field(default_factory=list)

    def en_dictionnaire(self) -> dict[str, object]:
        return {
            "fenetre_jours": self.fenetre_jours,
            "lignes_examinees": self.lignes_examinees,
            "par_statut": dict(sorted(self.par_statut.items())),
            "appels_reexamines": self.appels_reexamines,
            "par_methode": dict(sorted(self.par_methode.items())),
            "actes_non_delegues_ignores": self.actes_non_delegues,
            "mouvements_absents_du_coeur_erp": self.mouvements_inconnus_erp,
            "ecart_total_cents": self.ecart_total_cents,
            "lots": self.lots,
        }


def _charge_appels(session: Session) -> tuple[dict[str, _AppelConnu], dict[str, list[_AppelConnu]]]:
    """Charge les appels une fois : index par reference, et index par numero de porteur.

    Une requete par ligne de releve serait la solution evidente et la mauvaise : sur un releve de mille
    lignes elle produit mille allers-retours, et la fenetre par montant et date en produirait autant.

    Deux requetes, parce que le produit se lit sur le contrat en individuel et sur la police en
    collectif. Les deux alimentent les memes index.
    """
    colonnes = (
        AppelCotisation.id,
        AppelCotisation.reference_appel,
        AppelCotisation.date_echeance,
        AppelCotisation.montant_total_cents,
    )
    requete_individuels = (
        select(Contrat.numero_contrat, Contrat.produit_id, *colonnes)
        .join(Contrat, Contrat.id == AppelCotisation.contrat_id)
    )
    requete_groupes = (
        select(PoliceCollective.numero_police, PoliceCollective.produit_id, *colonnes)
        .join(PoliceCollective, PoliceCollective.id == AppelCotisation.police_id)
    )

    par_reference: dict[str, _AppelConnu] = {}
    par_porteur: dict[str, list[_AppelConnu]] = defaultdict(list)
    for requete in (requete_individuels, requete_groupes):
        for numero_porteur, produit_id, identifiant, reference, echeance, montant in session.execute(
            requete
        ):
            appel = _AppelConnu(
                identifiant=identifiant,
                reference=reference,
                numero_porteur=numero_porteur,
                produit_id=produit_id,
                date_echeance=echeance,
                montant_total_cents=int(montant),
            )
            par_reference[reference] = appel
            par_porteur[numero_porteur].append(appel)
    for appels in par_porteur.values():
        appels.sort(key=lambda appel: (appel.date_echeance, appel.identifiant))
    return par_reference, dict(par_porteur)


def _candidat_par_montant_et_date(
    appels: list[_AppelConnu], montant_cents: int, jour: date, fenetre_jours: int
) -> _AppelConnu | None:
    """Appel du contrat dont le montant correspond et dont l'echeance tombe dans la fenetre.

    L'echeance la plus proche du mouvement l'emporte, et le plus petit identifiant tranche une egalite.
    Sans cette regle, un contrat mensuel a cotisation constante rendrait un resultat dependant de l'ordre
    de lecture, et deux executions ne donneraient pas le meme rapprochement.
    """
    candidats = [
        appel
        for appel in appels
        if abs(appel.montant_total_cents - montant_cents) <= TOLERANCE_RAPPROCHEMENT_CENTS
        and abs((appel.date_echeance - jour).days) <= fenetre_jours
    ]
    if not candidats:
        return None
    return min(candidats, key=lambda appel: (abs((appel.date_echeance - jour).days), appel.identifiant))


def _statut(montant_encaisse: int, montant_appele: int) -> StatutRapprochement:
    """Qualifie un rapprochement abouti, selon l'ecart entre l'encaisse et l'appele."""
    if abs(montant_encaisse - montant_appele) <= TOLERANCE_RAPPROCHEMENT_CENTS:
        return StatutRapprochement.RAPPROCHE
    if montant_appele * PART_MINIMALE_PARTIEL <= montant_encaisse < montant_appele:
        return StatutRapprochement.PARTIEL
    return StatutRapprochement.ECART


def _lignes_a_rapprocher(session: Session) -> list[BrutEncaissement]:
    """Lignes de releve des lots charges, dedoublonnees par reference bancaire.

    Une reference bancaire ne designe qu'un mouvement. Si deux lots charges la portent, par exemple deux
    mois dont les perimetres se recouvrent, la premiere occurrence dans l'ordre des lots est retenue :
    la seconde n'apporterait pas un second mouvement, elle le repeterait.
    """
    requete = (
        select(BrutEncaissement)
        .join(LotFichier, LotFichier.id == BrutEncaissement.lot_id)
        .where(LotFichier.statut == StatutLot.CHARGE)
        .order_by(BrutEncaissement.lot_id, BrutEncaissement.ligne_numero)
    )
    vues: set[str] = set()
    lignes: list[BrutEncaissement] = []
    for ligne in session.scalars(requete):
        if ligne.reference_bancaire in vues:
            continue
        vues.add(ligne.reference_bancaire)
        lignes.append(ligne)
    return lignes


def rapproche(session: Session, fenetre_jours: int | None = None) -> ResultatRapprochement:
    """Rapproche toutes les lignes de releve des lots charges, et ecrit les verdicts.

    Idempotente par construction : le verdict d'une ligne est ecrit en place, repere par sa reference
    bancaire. Rejouer le rapprochement sur les memes lots recalcule les memes verdicts et n'ajoute aucune
    ligne. Ce n'est pas une precaution de confort : le graphe doit pouvoir etre relance apres un echec
    sans que les commissions du mois doublent.
    """
    fenetre = FENETRE_RAPPROCHEMENT_JOURS if fenetre_jours is None else fenetre_jours
    resultat = ResultatRapprochement(fenetre_jours=fenetre)
    par_reference, par_porteur = _charge_appels(session)

    encaissements_erp = {
        reference: (identifiant, appel_id)
        for reference, identifiant, appel_id in session.execute(
            select(Encaissement.reference_bancaire, Encaissement.id, Encaissement.appel_id)
        )
    }
    verdicts_existants = {
        verdict.reference_bancaire: verdict
        for verdict in session.scalars(select(RapprochementLigne))
    }
    # Le perimetre delegue est lu une fois par produit et par date d'echeance utile. Un cache suffit :
    # les dates d'echeance se repetent beaucoup, et la convention ne change pas en cours de mois.
    cache_delegation: dict[tuple[int, date], bool] = {}

    def encaissement_delegue(produit_id: int, jour: date) -> bool:
        cle = (produit_id, jour)
        if cle not in cache_delegation:
            perimetre = perimetre_delegue(session, produit_id, jour)
            cache_delegation[cle] = perimetre.autorise(ActeGestion.ENCAISSEMENT)
        return cache_delegation[cle]

    references_lots: set[str] = set()
    #: Appels dont le rattachement bancaire change pendant ce passage, et dont le statut devra donc
    #: etre rededuit a la fin : celui qu'un mouvement quitte comme celui qu'il rejoint.
    appels_a_reexaminer: set[int] = set()
    for ligne in _lignes_a_rapprocher(session):
        resultat.lignes_examinees += 1
        references_lots.add(str(ligne.lot_id))

        appel = None
        methode = MethodeRapprochement.AUCUNE
        if ligne.reference_appel:
            appel = par_reference.get(ligne.reference_appel)
            if appel is not None:
                methode = MethodeRapprochement.REFERENCE
        porteur = ligne.numero_contrat or ligne.numero_police
        if appel is None and porteur:
            appel = _candidat_par_montant_et_date(
                par_porteur.get(porteur, []),
                ligne.montant_cents,
                ligne.date_encaissement,
                fenetre,
            )
            if appel is not None:
                methode = MethodeRapprochement.MONTANT_DATE

        # Delegation : opposee avant d'ecrire quoi que ce soit.
        if appel is not None and not encaissement_delegue(appel.produit_id, appel.date_echeance):
            resultat.actes_non_delegues += 1
            continue

        if appel is None:
            statut = StatutRapprochement.NON_TROUVE
            montant_appele = None
            ecart = None
        else:
            statut = _statut(ligne.montant_cents, appel.montant_total_cents)
            montant_appele = appel.montant_total_cents
            ecart = ligne.montant_cents - appel.montant_total_cents
            resultat.ecart_total_cents += ecart

        reference_erp = encaissements_erp.get(ligne.reference_bancaire)
        if reference_erp is None:
            resultat.mouvements_inconnus_erp += 1
        encaissement_id = reference_erp[0] if reference_erp else None

        verdict = verdicts_existants.get(ligne.reference_bancaire)
        if verdict is None:
            verdict = RapprochementLigne(reference_bancaire=ligne.reference_bancaire)
            session.add(verdict)
            verdicts_existants[ligne.reference_bancaire] = verdict
        verdict.lot_id = ligne.lot_id
        verdict.encaissement_id = encaissement_id
        verdict.appel_id = appel.identifiant if appel else None
        verdict.methode = methode
        verdict.statut = statut.value
        verdict.montant_encaisse_cents = ligne.montant_cents
        verdict.montant_appele_cents = montant_appele
        verdict.ecart_cents = ecart
        verdict.fenetre_jours = fenetre if methode is MethodeRapprochement.MONTANT_DATE else None

        # Report sur le coeur ERP, pour que l'ecran des anomalies et les marts lisent une seule verite.
        if encaissement_id is not None:
            mouvement = session.get(Encaissement, encaissement_id)
            if mouvement is not None:
                mouvement.statut_rapprochement = statut
                nouvel_appel = appel.identifiant if appel else None
                if mouvement.appel_id != nouvel_appel:
                    # Les deux appels sont a reexaminer : celui que le mouvement quitte, qui perd une
                    # recette, et celui qu'il rejoint, qui en gagne une.
                    appels_a_reexaminer.update(
                        identifiant
                        for identifiant in (mouvement.appel_id, nouvel_appel)
                        if identifiant is not None
                    )
                mouvement.appel_id = nouvel_appel

        resultat.par_statut[statut.value] += 1
        resultat.par_methode[methode.value] += 1

    resultat.appels_reexamines = _reexamine_les_appels(session, appels_a_reexaminer)
    session.commit()
    resultat.lots = sorted(references_lots)
    return resultat


def _reexamine_les_appels(session: Session, identifiants: set[int]) -> int:
    """Recalcule le statut des appels dont le rattachement bancaire vient de changer.

    Defaut constate en executant la chaine puis le controle de coherence, et non en relisant le code :
    onze appels se declaraient encaisses ou partiels alors qu'aucun encaissement ne pointait plus sur
    eux. Le rapprochement detachait bien le mouvement, puisque sa correspondance n'etait pas retrouvee,
    mais il laissait a l'appel le statut que le quittancement lui avait donne. L'ecran des anomalies et
    les marts lisaient alors deux verites contradictoires sur la meme ligne, ce que tout ce module
    s'attache a empecher.

    Le statut est rededuit de la seule chose qui fasse foi, la somme reellement encaissee sur l'appel.
    Un appel sans recette redevient attendu, et non impaye : l'impaye est un constat de gestion, qui
    suppose une echeance passee et une relance, alors qu'ici le cabinet constate seulement qu'il n'a
    rien recu a rattacher. Un appel qui portait **deja** le constat d'impaye et qui n'a toujours aucune
    recette le garde, pour la meme raison : ce n'est pas au rapprochement de l'effacer. En revanche une
    recette retrouvee le solde, en partie ou en totalite, et le constat d'impaye tombe alors de
    lui-meme, ce qui est le comportement voulu.
    """
    if not identifiants:
        return 0
    encaisse_par_appel = dict(
        session.execute(
            select(Encaissement.appel_id, func.coalesce(func.sum(Encaissement.montant_cents), 0))
            .where(Encaissement.appel_id.in_(identifiants))
            .group_by(Encaissement.appel_id)
        ).all()
    )
    modifies = 0
    for appel in session.scalars(
        select(AppelCotisation).where(AppelCotisation.id.in_(identifiants))
    ):
        encaisse = int(encaisse_par_appel.get(appel.id, 0))
        if encaisse <= 0:
            # Un appel deja declare impaye le reste : l'impaye est un constat de gestion, pose apres
            # une echeance passee et une relance, et le rapprochement n'a pas de quoi le defaire. Seul
            # un appel qui n'avait pas ce constat redevient attendu.
            statut = (
                StatutAppel.IMPAYE if appel.statut is StatutAppel.IMPAYE else StatutAppel.ATTENDU
            )
        elif encaisse < appel.montant_total_cents:
            statut = StatutAppel.PARTIEL
        else:
            statut = StatutAppel.ENCAISSE
        if appel.statut is not statut:
            appel.statut = statut
            modifies += 1
    return modifies
