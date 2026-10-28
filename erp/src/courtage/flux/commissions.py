"""Calcul des commissions dues par les compagnies, et des retrocessions dues aux apporteurs.

C'est le coeur economique d'un courtier grossiste. La compagnie lui verse une commission sur les
cotisations, et il en reverse une part a l'apporteur qui a amene l'affaire. Deux calculs, deux
destinataires, et trois regles qui doivent etre exactes sous peine de produire un bordereau indefendable.

**L'assiette est la cotisation encaissee, hors taxe.** Hypothese de simulation documentee dans
`docs/HYPOTHESES.md`, et surtout **parametre en base** : la colonne `assiette` de `taux_commission`
accepte aussi la cotisation appelee, et le calcul la respecte. Les deux pratiques existent dans le
metier, et le cabinet qui lirait ce code doit pouvoir y retrouver la sienne sans le modifier.

**Le taux retenu est celui en vigueur a la date d'echeance de l'appel**, pas celui d'aujourd'hui. Une
echeance de janvier commissionnee en avril applique le bareme de janvier. Sans cela, une revalorisation
du bareme reecrirait les bordereaux deja regles, ce qu'aucune compagnie n'accepte.

**L'apporteur retenu est celui en vigueur sur le contrat a la date d'echeance.** Il est reconstitue par
les avenants, comme au module 1. Un changement d'apporteur en septembre ne redonne pas a l'arrivant les
commissions de janvier. Sur une police collective, l'apporteur est celui de la police : une police n'a
pas d'avenants dans ce modele, et c'est une limite assumee, consignee dans `docs/HYPOTHESES.md`.

**Les deux formes d'appel sont commissionnees.** Un appel individuel donne une commission qui porte le
contrat, un appel groupe une commission qui porte la police. Designer un affilie au hasard sur une
cotisation appelee aupres de l'entreprise produirait un bordereau que la compagnie refuserait de lire.

**Quand la compagnie encaisse elle-meme, l'assiette bascule sur la cotisation appelee.** C'est la
correction d'un contresens metier qui coutait cher : ces appels etaient purement **ecartes**, et
31 pour cent du portefeuille, 1 184 appels sur 3 776, disparaissaient des bordereaux de commissions.

Le raisonnement derriere l'ancien comportement n'etait pas absurde : sans delegation de
l'encaissement, le cabinet ne voit aucun mouvement bancaire, donc il ne connait pas la cotisation
encaissee. Mais la conclusion l'etait. Un courtier dont la compagnie encaisse directement est
commissionne quand meme : la compagnie encaisse, calcule la commission due et la lui verse avec son
**bordereau de commissions**. Ne rien commissionner revient a declarer que ces affaires ne rapportent
rien, ce qu'aucun dirigeant de cabinet ne lirait sans sursauter.

Ce que fait le calcul maintenant : il retient la **cotisation appelee hors taxe**, telle que le
bordereau de cotisations de la compagnie la declare, qui est la meilleure assiette observable par le
cabinet pour ces produits. Le cas est compte a part, sous
`assiette_appelee_car_encaissement_non_delegue`, parce que ces lignes sont precisement celles qu'un
gestionnaire doit rapprocher du bordereau de commissions recu de la compagnie.

Limite assumee, et elle est consignee dans `docs/DECISIONS.md` : le bordereau de commissions recu de
la compagnie n'est pas un flux entrant de ce module. Le rapprochement entre la commission calculee
ici et la commission reellement versee par la compagnie est donc hors perimetre. Le jour ou ce
quatrieme flux existe, c'est lui qui fait foi sur ces produits, et le calcul ci-dessous devient une
attente a comparer.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, literal, or_, select
from sqlalchemy.orm import Session

from courtage.domaine.delegation import perimetre_delegue
from courtage.domaine.montants import (
    TauxIntrouvable,
    applique_taux,
    taux_commission_applicable,
    taux_retrocession_applicable,
)
from courtage.flux.enums import StatutLot, TypeFlux
from courtage.flux.modeles import LotFichier, RapprochementLigne
from courtage.generateur.outils import dernier_jour_du_mois
from courtage.modeles.collectif import PoliceCollective
from courtage.modeles.enums import ActeGestion, AssietteCommission, StatutRapprochement
from courtage.modeles.financier import AppelCotisation, Commission, Retrocession
from courtage.modeles.portefeuille import Avenant, Contrat
from courtage.modeles.referentiel import Produit

#: Statuts de rapprochement qui temoignent d'un encaissement reellement constate. `non_trouve` en est
#: exclu : aucun appel n'a ete identifie, donc rien n'est encaissable sur un appel.
STATUTS_ENCAISSES = (
    StatutRapprochement.RAPPROCHE.value,
    StatutRapprochement.PARTIEL.value,
    StatutRapprochement.ECART.value,
)


@dataclass(slots=True)
class ResultatCommissions:
    """Compte rendu du calcul, pour le journal d'execution et pour la sortie de la commande."""

    periodes: list[str] = field(default_factory=list)
    appels_examines: int = 0
    commissions_creees: int = 0
    commissions_mises_a_jour: int = 0
    commissions_inchangees: int = 0
    commissions_supprimees: int = 0
    retrocessions_creees: int = 0
    retrocessions_mises_a_jour: int = 0
    retrocessions_inchangees: int = 0
    retrocessions_supprimees: int = 0
    montant_commissions_cents: int = 0
    montant_retrocessions_cents: int = 0
    ecartes: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    #: Appels commissionnes sur une assiette de repli, et la raison du repli. A distinguer des appels
    #: ecartes : ici la commission existe, mais son assiette n'est pas celle que le taux declare.
    assiettes_de_repli: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    #: Mois qui comptent des echeances mais dont aucun releve bancaire n'a ete charge. Sans cette
    #: liste, un releve rejete faisait disparaitre le mois du perimetre sans un mot, et la chaine
    #: restait verte en ne calculant aucune commission.
    periodes_sans_releve: list[str] = field(default_factory=list)

    def en_dictionnaire(self) -> dict[str, object]:
        return {
            "periodes": self.periodes,
            "appels_examines": self.appels_examines,
            "commissions": {
                "creees": self.commissions_creees,
                "mises_a_jour": self.commissions_mises_a_jour,
                "inchangees": self.commissions_inchangees,
                "supprimees": self.commissions_supprimees,
                "montant_cents": self.montant_commissions_cents,
            },
            "retrocessions": {
                "creees": self.retrocessions_creees,
                "mises_a_jour": self.retrocessions_mises_a_jour,
                "inchangees": self.retrocessions_inchangees,
                "supprimees": self.retrocessions_supprimees,
                "montant_cents": self.montant_retrocessions_cents,
            },
            "appels_ecartes": dict(sorted(self.ecartes.items())),
            "assiettes_de_repli": dict(sorted(self.assiettes_de_repli.items())),
            "periodes_sans_releve_bancaire_charge": self.periodes_sans_releve,
        }


def _premier_jour(jour: date) -> date:
    return date(jour.year, jour.month, 1)


def prorata_hors_taxe(montant_encaisse: int, montant_ht: int, montant_total: int) -> int:
    """Part hors taxe d'un montant encaisse, au prorata de la decomposition de l'appel.

    Un reglement partiel regle la taxe et la cotisation dans la meme proportion : c'est la convention
    retenue, et la seule qui preserve l'egalite entre le total encaisse et la somme de ses deux parts.
    """
    if montant_total <= 0:
        return 0
    if montant_encaisse >= montant_total:
        return montant_ht
    produit = Decimal(montant_encaisse) * Decimal(montant_ht) / Decimal(montant_total)
    return int(produit.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def periodes_chargees(session: Session) -> list[date]:
    """Mois pour lesquels un releve bancaire a ete charge.

    C'est le perimetre du calcul, et le choix se defend : l'assiette retenue est la cotisation
    encaissee, donc commissionner un mois dont le releve n'est pas arrive produirait une assiette nulle
    et un bordereau faux. Le mois attend son releve.
    """
    requete = (
        select(LotFichier.periode)
        .where(LotFichier.statut == StatutLot.CHARGE, LotFichier.type_flux == TypeFlux.ENCAISSEMENTS)
        .distinct()
        .order_by(LotFichier.periode)
    )
    return list(session.scalars(requete))


def _periodes_attendues_sans_releve(session: Session, mois_calcules: list[date]) -> list[str]:
    """Mois couverts par du courrier recu, mais dont aucun releve bancaire n'a pu etre charge.

    C'est le defaut le plus couteux que la relecture ait releve : un releve rejete pour une seule ligne
    fautive faisait sortir le mois entier du perimetre, sans message, et la chaine se terminait en
    succes sans avoir calcule la moindre commission. Le mois attend son releve, et il faut le dire.

    Le perimetre est celui du courrier recu, et non tout l'historique du portefeuille. Signaler les
    trente-six mois anterieurs a la mise en service noierait l'information utile : ce qui compte, c'est
    qu'un bordereau de cotisations soit arrive pour un mois dont le releve manque.
    """
    periodes_recues = set(
        session.scalars(
            select(LotFichier.periode).where(LotFichier.statut == StatutLot.CHARGE).distinct()
        )
    )
    calcules = set(mois_calcules)
    return [mois.isoformat() for mois in sorted(periodes_recues - calcules)]


def _apporteurs_a_la_date(session: Session, contrats: set[int]) -> dict[int, list[tuple[date, int, int]]]:
    """Prepare la recherche de l'apporteur en vigueur, par chargement unique des avenants.

    Rend un index intermediaire : pour chaque contrat, la liste de ses avenants triee. La recherche
    elle-meme est faite par `_apporteur_en_vigueur`. Appeler `etat_a_la_date` appel par appel donnerait
    une requete par appel, soit plusieurs milliers.
    """
    requete = (
        select(Avenant.contrat_id, Avenant.date_effet, Avenant.numero_ordre, Avenant.apporteur_id)
        .where(Avenant.contrat_id.in_(contrats))
        .order_by(Avenant.contrat_id, Avenant.date_effet, Avenant.numero_ordre)
    )
    index: dict[int, list[tuple[date, int, int]]] = defaultdict(list)
    for contrat_id, date_effet, numero_ordre, apporteur_id in session.execute(requete):
        index[contrat_id].append((date_effet, numero_ordre, apporteur_id))
    return dict(index)


def _apporteur_en_vigueur(
    avenants: list[tuple[date, int, int]], a_la_date: date
) -> int | None:
    """Apporteur porte par le dernier avenant pris effet a cette date.

    Meme regle d'ordre que `domaine.avenants` : date d'effet, puis numero d'ordre. Deux avenants le meme
    jour arrivent reellement, et c'est le dernier saisi qui vaut.
    """
    retenu: tuple[date, int, int] | None = None
    for candidat in avenants:
        if candidat[0] <= a_la_date and (retenu is None or candidat[:2] > retenu[:2]):
            retenu = candidat
    return retenu[2] if retenu else None


def _annule(
    session: Session,
    resultat: ResultatCommissions,
    commissions_existantes: dict[int, Commission],
    retrocessions_existantes: dict[int, Retrocession],
    appel_id: int,
) -> None:
    """Supprime la commission et la retrocession d'un appel que le recalcul vient d'ecarter.

    Sans cette suppression, le calcul n'etait pas reellement idempotent : il comptait l'appel comme
    ecarte tout en laissant en base la commission calculee au passage precedent, et le bordereau la
    reprenait. Concretement, une convention d'encaissement revoquee, un apporteur sorti ou un
    rapprochement defait continuaient a payer une retrocession que plus rien ne justifiait.
    """
    commission = commissions_existantes.pop(appel_id, None)
    if commission is None:
        return
    retrocession = retrocessions_existantes.pop(commission.id, None)
    if retrocession is not None:
        session.delete(retrocession)
        resultat.retrocessions_supprimees += 1
    session.delete(commission)
    resultat.commissions_supprimees += 1


def _annule_retrocession(
    session: Session,
    resultat: ResultatCommissions,
    retrocessions_existantes: dict[int, Retrocession],
    commission_id: int,
) -> None:
    """Supprime la seule retrocession, quand la commission reste due mais plus sa part a l'apporteur."""
    retrocession = retrocessions_existantes.pop(commission_id, None)
    if retrocession is not None:
        session.delete(retrocession)
        resultat.retrocessions_supprimees += 1


def calcule(session: Session, periodes: list[date] | None = None) -> ResultatCommissions:
    """Calcule les commissions et les retrocessions des periodes demandees.

    Idempotente : une commission est reperee par son appel, une retrocession par sa commission, et
    chacune est mise a jour en place. Rejouer le calcul sur les memes donnees ne cree rien et ne modifie
    rien, ce que la preuve d'idempotence verifie en comparant les comptes et les montants avant et apres.
    """
    resultat = ResultatCommissions()
    # Toute periode demandee est ramenee au premier du mois. Une date au milieu du mois produisait un
    # calcul partiel silencieux : les appels n'etaient pris qu'a partir de ce jour, et les bordereaux,
    # qui cherchent la periode au premier du mois, ne trouvaient ensuite aucune commission.
    mois = sorted({_premier_jour(jour) for jour in (periodes if periodes is not None else periodes_chargees(session))})
    resultat.periodes = [jour.isoformat() for jour in mois]
    resultat.periodes_sans_releve = _periodes_attendues_sans_releve(session, mois)
    if not mois:
        return resultat

    bornes = [(jour, dernier_jour_du_mois(jour)) for jour in mois]

    # Appels du perimetre, avec leur contrat et leur produit.
    conditions = [AppelCotisation.date_echeance.between(debut, fin) for debut, fin in bornes]
    colonnes = (
        AppelCotisation.id,
        AppelCotisation.date_echeance,
        AppelCotisation.montant_ht_cents,
        AppelCotisation.montant_total_cents,
    )
    # Deux requetes, une par forme d'appel. Le produit et l'apporteur se lisent sur le contrat en
    # individuel et sur la police en collectif, et une jointure conditionnelle rendrait la requete
    # illisible pour qui veut la relire.
    requete_individuels = (
        select(
            Contrat.id.label("contrat_id"),
            literal(None).label("police_id"),
            Contrat.produit_id,
            literal(None).label("apporteur_police"),
            *colonnes,
        )
        .join(Contrat, Contrat.id == AppelCotisation.contrat_id)
        .where(or_(*conditions))
    )
    requete_groupes = (
        select(
            literal(None).label("contrat_id"),
            PoliceCollective.id.label("police_id"),
            PoliceCollective.produit_id,
            PoliceCollective.apporteur_id.label("apporteur_police"),
            *colonnes,
        )
        .join(PoliceCollective, PoliceCollective.id == AppelCotisation.police_id)
        .where(or_(*conditions))
    )
    appels = sorted(
        [*session.execute(requete_individuels), *session.execute(requete_groupes)],
        key=lambda ligne: ligne[4],
    )
    if not appels:
        return resultat

    identifiants_appels = {ligne[4] for ligne in appels}
    contrats = {ligne[0] for ligne in appels if ligne[0] is not None}

    # Encaissements constates par le rapprochement, agreges par appel.
    encaisse_par_appel: dict[int, int] = {}
    requete_encaisse = (
        select(RapprochementLigne.appel_id, func.sum(RapprochementLigne.montant_encaisse_cents))
        .where(
            RapprochementLigne.appel_id.in_(identifiants_appels),
            RapprochementLigne.statut.in_(STATUTS_ENCAISSES),
        )
        .group_by(RapprochementLigne.appel_id)
    )
    for appel_id, somme in session.execute(requete_encaisse):
        encaisse_par_appel[appel_id] = int(somme or 0)

    commissions_existantes = {
        commission.appel_id: commission
        for commission in session.scalars(
            select(Commission).where(Commission.appel_id.in_(identifiants_appels))
        )
    }
    identifiants_commissions = [commission.id for commission in commissions_existantes.values()]
    retrocessions_existantes = {
        retrocession.commission_id: retrocession
        for retrocession in session.scalars(
            select(Retrocession).where(Retrocession.commission_id.in_(identifiants_commissions))
        )
    }
    produit_vers_compagnie = {
        produit_id: compagnie_id
        for produit_id, compagnie_id in session.execute(select(Produit.id, Produit.compagnie_id))
    }

    avenants = _apporteurs_a_la_date(session, contrats)
    cache_taux_commission: dict[tuple[int, date], tuple[int, str] | None] = {}
    cache_delegation: dict[tuple[int, date], bool] = {}
    cache_taux_retrocession: dict[tuple[int, int, date], int | None] = {}

    for (
        contrat_id,
        police_id,
        produit_id,
        apporteur_police,
        appel_id,
        echeance,
        montant_ht,
        montant_total,
    ) in appels:
        resultat.appels_examines += 1
        periode = _premier_jour(echeance)

        cle_taux = (produit_id, echeance)
        if cle_taux not in cache_taux_commission:
            try:
                taux = taux_commission_applicable(session, produit_id, echeance)
                # `AssietteCommission(...)` normalise : la colonne rend une enumeration quand elle
                # vient de la base, et une chaine quand elle vient d'etre affectee en Python.
                cache_taux_commission[cle_taux] = (
                    taux.taux_bp,
                    AssietteCommission(taux.assiette).value,
                )
            except TauxIntrouvable:
                cache_taux_commission[cle_taux] = None
        taux_connu = cache_taux_commission[cle_taux]
        if taux_connu is None:
            resultat.ecartes["aucun_taux_de_commission_en_vigueur"] += 1
            _annule(session, resultat, commissions_existantes, retrocessions_existantes, appel_id)
            continue
        taux_bp, assiette_declaree = taux_connu

        if assiette_declaree == AssietteCommission.COTISATION_ENCAISSEE.value:
            cle_delegation = (produit_id, echeance)
            if cle_delegation not in cache_delegation:
                perimetre = perimetre_delegue(session, produit_id, echeance)
                cache_delegation[cle_delegation] = perimetre.autorise(ActeGestion.ENCAISSEMENT)
            if not cache_delegation[cle_delegation]:
                # La compagnie encaisse elle-meme : le cabinet ne voit aucun mouvement bancaire sur
                # ces contrats, mais il est commissionne quand meme, sur le bordereau de commissions
                # que la compagnie lui adresse. L'assiette retenue est donc la cotisation appelee
                # hors taxe, la meilleure assiette observable ici. Le cas est compte a part : ce sont
                # les lignes a rapprocher du bordereau de la compagnie.
                resultat.assiettes_de_repli["assiette_appelee_car_encaissement_non_delegue"] += 1
                assiette = int(montant_ht)
            else:
                # L'excedent d'un trop-percu n'est pas commissionnable : l'assiette est plafonnee a
                # l'appel.
                encaisse = min(encaisse_par_appel.get(appel_id, 0), int(montant_total))
                if encaisse <= 0:
                    resultat.ecartes["aucun_encaissement_rapproche"] += 1
                    _annule(
                        session, resultat, commissions_existantes, retrocessions_existantes, appel_id
                    )
                    continue
                assiette = prorata_hors_taxe(encaisse, int(montant_ht), int(montant_total))
        else:
            assiette = int(montant_ht)

        montant = applique_taux(assiette, taux_bp)
        compagnie_id = produit_vers_compagnie[produit_id]

        commission = commissions_existantes.get(appel_id)
        if commission is None:
            commission = Commission(
                appel_id=appel_id,
                contrat_id=contrat_id,
                police_id=police_id,
                compagnie_id=compagnie_id,
                produit_id=produit_id,
                periode=periode,
                assiette_cents=assiette,
                taux_bp=taux_bp,
                montant_cents=montant,
            )
            session.add(commission)
            session.flush()
            commissions_existantes[appel_id] = commission
            resultat.commissions_creees += 1
        elif (
            commission.assiette_cents != assiette
            or commission.taux_bp != taux_bp
            or commission.montant_cents != montant
        ):
            commission.assiette_cents = assiette
            commission.taux_bp = taux_bp
            commission.montant_cents = montant
            commission.periode = periode
            resultat.commissions_mises_a_jour += 1
        else:
            resultat.commissions_inchangees += 1
        resultat.montant_commissions_cents += montant

        # Individuel : l'apporteur en vigueur a la date d'echeance, reconstitue par les avenants.
        # Collectif : l'apporteur de la police, qui n'a pas d'historique d'avenants dans ce modele.
        if contrat_id is not None:
            apporteur_id = _apporteur_en_vigueur(avenants.get(contrat_id, []), echeance)
        else:
            apporteur_id = apporteur_police
        if apporteur_id is None:
            resultat.ecartes["aucun_apporteur_en_vigueur"] += 1
            _annule_retrocession(session, resultat, retrocessions_existantes, commission.id)
            continue

        cle_retro = (apporteur_id, produit_id, echeance)
        if cle_retro not in cache_taux_retrocession:
            try:
                taux_retro = taux_retrocession_applicable(session, apporteur_id, produit_id, echeance)
                cache_taux_retrocession[cle_retro] = taux_retro.taux_bp
            except TauxIntrouvable:
                cache_taux_retrocession[cle_retro] = None
        taux_retro_bp = cache_taux_retrocession[cle_retro]
        if taux_retro_bp is None:
            resultat.ecartes["aucun_taux_de_retrocession_en_vigueur"] += 1
            _annule_retrocession(session, resultat, retrocessions_existantes, commission.id)
            continue
        if taux_retro_bp == 0:
            # Production directe : aucun intermediaire a remunerer, donc aucune retrocession. Aucune ligne, plutot
            # qu'une ligne a zero euro qui encombrerait son bordereau sans rien lui apprendre.
            resultat.ecartes["apporteur_sans_retrocession"] += 1
            _annule_retrocession(session, resultat, retrocessions_existantes, commission.id)
            continue

        montant_retro = applique_taux(montant, taux_retro_bp)
        retrocession = retrocessions_existantes.get(commission.id)
        if retrocession is None:
            retrocession = Retrocession(
                commission_id=commission.id,
                apporteur_id=apporteur_id,
                periode=periode,
                assiette_cents=montant,
                taux_bp=taux_retro_bp,
                montant_cents=montant_retro,
            )
            session.add(retrocession)
            retrocessions_existantes[commission.id] = retrocession
            resultat.retrocessions_creees += 1
        elif (
            retrocession.apporteur_id != apporteur_id
            or retrocession.assiette_cents != montant
            or retrocession.taux_bp != taux_retro_bp
            or retrocession.montant_cents != montant_retro
        ):
            retrocession.apporteur_id = apporteur_id
            retrocession.assiette_cents = montant
            retrocession.taux_bp = taux_retro_bp
            retrocession.montant_cents = montant_retro
            retrocession.periode = periode
            resultat.retrocessions_mises_a_jour += 1
        else:
            resultat.retrocessions_inchangees += 1
        resultat.montant_retrocessions_cents += montant_retro

    session.commit()
    return resultat
