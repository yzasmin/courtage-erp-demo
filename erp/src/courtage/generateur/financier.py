"""Generation des flux : appels de cotisation, encaissements et prestations.

Perimetre volontairement limite, et c'est un choix de conception, pas un oubli. Le generateur du
module 1 produit la **realite operationnelle** du cabinet : ce qu'il a quittance, ce qu'il a encaisse, ce
qu'il a rembourse. Les commissions, les retrocessions et les bordereaux ne sont pas generes ici : ils
sont **calcules** par la chaine Airflow et dbt du module 2, a partir de ces memes donnees et des taux
dates du referentiel. Les generer aussi reviendrait a ecrire deux fois la meme regle de calcul, puis a
comparer un calcul a lui-meme.

Deux regles metier appliquees ici meritent d'etre signalees.

**Le quittancement respecte la delegation.** Un produit dont la compagnie n'a pas delegue le
quittancement ne recoit aucun appel de cotisation. Dans le referentiel genere, les trois compagnies
delegent le quittancement, donc tous les contrats sont quittances ; mais le controle est bien la, et il
porte sur les memes conventions que l'API.

**Les prestations ne sont generees que sur les produits dont la gestion des prestations est deleguee.**
C'est ce qui rend le refus visible dans les donnees : les contrats de **prevoyance**, portes par
Littoral, n'ont aucune prestation, parce que cette compagnie instruit elle-meme ses sinistres. De meme,
les produits **Pyrene** n'ont aucun encaissement, parce que cette compagnie preleve elle-meme les
cotisations. Un ecran qui afficherait des prestations sur un contrat de prevoyance, ou un encaissement
sur un contrat Pyrene, serait un defaut.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from random import Random

from sqlalchemy.orm import Session

from courtage.domaine.montants import applique_taux
from courtage.generateur.delegation_datee import DelegationDatee
from courtage.generateur.outils import (
    age_a_la_date,
    ajoute_mois,
    dernier_jour_du_mois,
    insere_par_lots,
    mois_entre,
    tire_pondere,
    tire_pondere_indices,
)
from courtage.generateur.parametres import (
    DEBUT_HISTORIQUE,
    DELAI_ENCAISSEMENT_JOURS,
    DELAI_REGLEMENT_JOURS,
    ECHELLE_SINISTRALITE,
    PART_APPEL_IMPAYE,
    PART_APPEL_PARTIEL,
    PART_PRESTATION_REJETEE,
    POIDS_MOYEN_PAIEMENT,
    POIDS_SAISON_PRESTATIONS,
    PRESTATIONS_PAR_AN_SELON_AGE,
    PROFILS_CONSOMMATION,
    TAUX_TAXE_BP,
)
from courtage.generateur.portefeuille import ContratGenere, PoliceGeneree, Portefeuille
from courtage.generateur.referentiel import GARANTIES_PAR_BRANCHE, Referentiel
from courtage.modeles.collectif import LigneAppelCotisation
from courtage.modeles.enums import (
    ActeGestion,
    MoyenPaiement,
    StatutAppel,
    StatutContrat,
    StatutPrestation,
    StatutRapprochement,
)
from courtage.modeles.financier import AppelCotisation, Encaissement, Prestation

#: Nombre de mois couverts par un appel, selon le fractionnement.
MOIS_PAR_APPEL = {"mensuel": 1, "trimestriel": 3, "semestriel": 6, "annuel": 12}

#: Motifs de rejet tires au hasard. Le plafond annuel n'y figure pas : il est **calcule** a partir du
#: cumul reel de l'annee, et non tire au sort. Un motif de rejet qui ne correspond a aucune situation
#: verifiable dans les donnees est exactement ce qu'un gestionnaire repere en ouvrant le dossier.
MOTIFS_REJET = (
    "garantie non souscrite a la date des soins",
    "declaration hors delai",
    "justificatif manquant",
    "beneficiaire non affilie a la date des soins",
)


@dataclass
class Flux:
    appels: int = 0
    lignes_appel: int = 0
    encaissements: int = 0
    prestations: int = 0


def genere_flux(
    session: Session,
    alea: Random,
    ref: Referentiel,
    portefeuille: Portefeuille,
    date_reference: date,
    delegation: DelegationDatee,
) -> Flux:
    """Produit les appels de cotisation, les encaissements et les prestations de tout le portefeuille."""
    contrats = portefeuille.contrats or []

    appels, lignes_appel, encaissements = _genere_quittancement(
        alea, ref, contrats, portefeuille.polices or [], date_reference, delegation
    )
    insere_par_lots(session, AppelCotisation, appels)
    insere_par_lots(session, LigneAppelCotisation, lignes_appel)
    insere_par_lots(session, Encaissement, encaissements)

    prestations = _genere_prestations(alea, ref, contrats, date_reference, delegation)
    insere_par_lots(session, Prestation, prestations)

    return Flux(
        appels=len(appels),
        lignes_appel=len(lignes_appel),
        encaissements=len(encaissements),
        prestations=len(prestations),
    )


def _valeur_a_la_date(paliers: list[tuple[date, int]], jour: date) -> int:
    """Valeur en vigueur a une date donnee, lue dans une suite de paliers ordonnee par date.

    Sert pour la cotisation, pour l'apporteur et pour la formule. C'est la meme operation que la
    reconstitution par avenants du module `domaine.avenants`, en version memoire : le generateur ne
    relit pas la base pendant qu'il la remplit.
    """
    courante = paliers[0][1]
    for debut, valeur in paliers:
        if debut <= jour:
            courante = valeur
        else:
            break
    return courante


def _periode_de_garantie(contrat: ContratGenere, date_reference: date) -> tuple[date, date] | None:
    """Fenetre pendant laquelle le contrat est en garantie, bornee par la date de reference."""
    if contrat.date_effet is None:
        return None
    fin = contrat.date_resiliation or date_reference
    fin = min(fin, date_reference)
    if fin <= contrat.date_effet:
        return None
    return contrat.date_effet, fin


def _debut_de_quittancement(debut_garantie: date) -> date:
    """Premier mois quittance : jamais avant l'ouverture de la fenetre de donnees.

    Un contrat peut avoir pris effet en 2019 ; le cabinet ne conserve en ligne que trois ans de flux.
    Le quittancement demarre donc au premier jour du mois d'ouverture de l'historique.
    """
    if debut_garantie >= DEBUT_HISTORIQUE:
        return debut_garantie
    return DEBUT_HISTORIQUE.replace(day=1)


def _quittance_une_police(
    alea: Random,
    ref: Referentiel,
    police: PoliceGeneree,
    affilies: list[ContratGenere],
    date_reference: date,
    delegation: DelegationDatee,
    appels: list[dict],
    lignes: list[dict],
    encaissements: list[dict],
    compteurs: dict[str, int],
) -> None:
    """Emet les appels groupes d'une police collective, adresses a l'entreprise souscriptrice.

    Un appel par periode et par police, et non un appel par salarie. Son montant est la somme des
    parts des affilies couverts pendant la periode, et le detail de ces parts est enregistre ligne a
    ligne : c'est ce qui permet de justifier le montant a l'entreprise, de repartir un reglement
    partiel, et de commissionner au bon niveau.

    Un salarie affilie en cours de periode est compte pour la periode entiere. C'est une simplification
    assumee, consignee dans `docs/HYPOTHESES.md` : le prorata temporis existe, il varie d'une convention
    a l'autre, et le reproduire n'ajouterait rien a ce que le projet demontre.
    """
    if not affilies:
        return
    if delegation.jamais_delegue(police.code_produit, ActeGestion.QUITTANCEMENT):
        return

    code_branche = ref.code_branche_de(police.code_produit)
    taux_taxe_bp = TAUX_TAXE_BP[code_branche]
    pas = MOIS_PAR_APPEL[police.fractionnement.value]
    # Une entreprise regle par virement, pas par mandat de prelevement sur un compte de salarie.
    moyen = MoyenPaiement.VIREMENT

    dates_effet = [c.date_effet for c in affilies if c.date_effet is not None]
    if not dates_effet:
        return
    fin_police = min(
        date_reference, max((c.date_resiliation or date_reference) for c in affilies)
    )
    periode_debut = _debut_de_quittancement(max(police.date_effet, min(dates_effet)))

    while periode_debut < fin_police:
        fin_periode_theorique = dernier_jour_du_mois(ajoute_mois(periode_debut, pas - 1))
        periode_fin = min(fin_periode_theorique, fin_police - timedelta(days=1))
        if periode_fin < periode_debut:
            break
        date_echeance = periode_debut
        if not delegation.autorise(
            police.code_produit, ActeGestion.QUITTANCEMENT, date_echeance
        ):
            periode_debut = ajoute_mois(periode_debut, pas)
            continue
        encaissement_delegue = delegation.autorise(
            police.code_produit, ActeGestion.ENCAISSEMENT, date_echeance
        )
        nb_mois = max(1, mois_entre(periode_debut, periode_fin) + 1)

        # Parts des affilies couverts pendant la periode.
        parts: list[tuple[int, int, int]] = []
        for contrat in affilies:
            if contrat.date_effet is None or contrat.date_effet > periode_fin:
                continue
            if contrat.date_resiliation is not None and contrat.date_resiliation <= periode_debut:
                continue
            cotisation = _valeur_a_la_date(contrat.paliers_cotisation, date_echeance)
            part_ht = cotisation * nb_mois
            parts.append((contrat.id, part_ht, applique_taux(part_ht, taux_taxe_bp)))

        if not parts:
            periode_debut = ajoute_mois(periode_debut, pas)
            continue

        montant_ht = sum(part for _id, part, _taxe in parts)
        montant_taxe = sum(taxe for _id, _part, taxe in parts)

        compteurs["appel"] += 1
        id_appel = compteurs["appel"]

        tirage = alea.random()
        if not encaissement_delegue:
            statut_appel = StatutAppel.ATTENDU
        elif tirage < PART_APPEL_IMPAYE:
            statut_appel = StatutAppel.IMPAYE
        elif tirage < PART_APPEL_IMPAYE + PART_APPEL_PARTIEL:
            statut_appel = StatutAppel.PARTIEL
        else:
            statut_appel = StatutAppel.ENCAISSE

        appels.append(
            {
                "id": id_appel,
                "reference_appel": f"APP-{police.numero_police}-{date_echeance:%Y%m}",
                "contrat_id": None,
                "police_id": police.id,
                "nombre_affilies": len(parts),
                "periode_debut": periode_debut,
                "periode_fin": periode_fin,
                "date_echeance": date_echeance,
                "montant_ht_cents": montant_ht,
                "montant_taxe_cents": montant_taxe,
                "montant_total_cents": montant_ht + montant_taxe,
                "taux_taxe_bp": taux_taxe_bp,
                "statut": statut_appel,
                "bordereau_id": None,
            }
        )
        for contrat_id, part_ht, part_taxe in parts:
            compteurs["ligne"] += 1
            lignes.append(
                {
                    "id": compteurs["ligne"],
                    "appel_id": id_appel,
                    "contrat_id": contrat_id,
                    "montant_ht_cents": part_ht,
                    "montant_taxe_cents": part_taxe,
                    "montant_total_cents": part_ht + part_taxe,
                }
            )

        if encaissement_delegue and statut_appel in (StatutAppel.ENCAISSE, StatutAppel.PARTIEL):
            borne_basse, borne_haute = DELAI_ENCAISSEMENT_JOURS[moyen.value]
            date_encaissement = date_echeance + timedelta(
                days=alea.randint(borne_basse, borne_haute)
            )
            if date_encaissement > date_reference:
                appels[-1]["statut"] = StatutAppel.ATTENDU
            else:
                montant_total = montant_ht + montant_taxe
                if statut_appel is StatutAppel.PARTIEL:
                    montant_encaisse = applique_taux(
                        montant_total, alea.randrange(5_000, 9_000, 100)
                    )
                    statut_rapprochement = StatutRapprochement.PARTIEL
                else:
                    montant_encaisse = montant_total
                    statut_rapprochement = StatutRapprochement.RAPPROCHE
                compteurs["encaissement"] += 1
                numero = compteurs["encaissement"]
                encaissements.append(
                    {
                        "id": numero,
                        "reference_bancaire": f"ENC-{date_encaissement:%Y%m%d}-{numero:07d}",
                        "appel_id": id_appel,
                        "contrat_id": None,
                        "date_encaissement": date_encaissement,
                        "montant_cents": montant_encaisse,
                        "moyen": moyen,
                        "statut_rapprochement": statut_rapprochement,
                        "libelle_releve": (
                            f"COTIS COLLECTIVE {police.numero_police} {date_echeance:%m/%Y}"
                        ),
                    }
                )

        periode_debut = ajoute_mois(periode_debut, pas)


def _genere_quittancement(
    alea: Random,
    ref: Referentiel,
    contrats: list[ContratGenere],
    polices: list[PoliceGeneree],
    date_reference: date,
    delegation: DelegationDatee,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Appels de cotisation et encaissements correspondants, sur toute la duree de garantie."""
    appels: list[dict] = []
    lignes: list[dict] = []
    encaissements: list[dict] = []
    compteurs = {"appel": 0, "ligne": 0, "encaissement": 0}

    # --- Collectif : un appel groupe par police et par periode, adresse a l'entreprise -----------
    affilies: dict[int, list[ContratGenere]] = {}
    for contrat in contrats:
        if contrat.police_id is not None and contrat.statut is not StatutContrat.DEVIS:
            affilies.setdefault(contrat.police_id, []).append(contrat)

    for police in polices:
        _quittance_une_police(
            alea, ref, police, affilies.get(police.id, []), date_reference, delegation,
            appels, lignes, encaissements, compteurs,
        )

    # --- Individuel et prevoyance : un appel par contrat -----------------------------------------
    for contrat in contrats:
        # Un devis n'est pas quittance : il n'a pas pris effet.
        if contrat.statut is StatutContrat.DEVIS:
            continue
        # Une affiliation collective est quittancee par sa police, pas pour elle-meme.
        if contrat.police_id is not None:
            continue
        # Pas de quittancement si la compagnie ne l'a jamais delegue sur ce produit. Les periodes
        # ou la delegation est suspendue sont ecartees appel par appel, plus bas.
        if delegation.jamais_delegue(contrat.produit_code, ActeGestion.QUITTANCEMENT):
            continue
        fenetre = _periode_de_garantie(contrat, date_reference)
        if fenetre is None:
            continue
        debut_garantie, fin_garantie = fenetre

        code_branche = ref.code_branche_de(contrat.produit_code)
        taux_taxe_bp = TAUX_TAXE_BP[code_branche]
        pas = MOIS_PAR_APPEL[contrat.fractionnement.value]
        moyen = MoyenPaiement(tire_pondere(alea, POIDS_MOYEN_PAIEMENT))

        # Les flux ne remontent pas avant l'ouverture de la fenetre de donnees, meme si le contrat
        # est plus ancien : le cabinet a un portefeuille plus vieux que les flux qu'il conserve.
        periode_debut = _debut_de_quittancement(debut_garantie)
        while periode_debut < fin_garantie:
            # `fin_garantie` est la date de **sortie** : la garantie ne court plus ce jour-la. Elle est
            # donc une borne exclusive, et la periode couverte s'arrete la veille. Sans cette
            # distinction, un contrat resilie au 1er juillet etait quittance pour juillet entier,
            # ce qui surfacturait un mois sur les contrats non mensuels.
            fin_periode_theorique = dernier_jour_du_mois(ajoute_mois(periode_debut, pas - 1))
            periode_fin = min(fin_periode_theorique, fin_garantie - timedelta(days=1))
            if periode_fin < periode_debut:
                break
            # L'echeance est le premier jour de la periode couverte : la cotisation est payable
            # d'avance, usage constant en assurance de personnes.
            date_echeance = periode_debut
            # La delegation est appreciee a la date de l'echeance : une convention echue arrete le
            # quittancement a cette date, elle n'efface pas les appels deja emis.
            if not delegation.autorise(
                contrat.produit_code, ActeGestion.QUITTANCEMENT, date_echeance
            ):
                periode_debut = ajoute_mois(periode_debut, pas)
                continue
            encaissement_delegue = delegation.autorise(
                contrat.produit_code, ActeGestion.ENCAISSEMENT, date_echeance
            )
            nb_mois = max(1, mois_entre(periode_debut, periode_fin) + 1)
            cotisation = _valeur_a_la_date(contrat.paliers_cotisation, date_echeance)
            montant_ht = cotisation * nb_mois
            montant_taxe = applique_taux(montant_ht, taux_taxe_bp)

            compteurs['appel'] += 1
            id_appel = compteurs['appel']
            tirage = alea.random()
            if not encaissement_delegue:
                # La compagnie preleve elle-meme : le cabinet a emis l'appel mais ne sait pas s'il a
                # ete regle. Le dire « encaisse » serait faux, le dire « impaye » le serait tout
                # autant et ferait remonter 28 000 faux impayes sur l'ecran des anomalies. Le statut
                # juste est « attendu », qui existe pour ce cas.
                statut_appel = StatutAppel.ATTENDU
            elif tirage < PART_APPEL_IMPAYE:
                statut_appel = StatutAppel.IMPAYE
            elif tirage < PART_APPEL_IMPAYE + PART_APPEL_PARTIEL:
                statut_appel = StatutAppel.PARTIEL
            else:
                statut_appel = StatutAppel.ENCAISSE

            appels.append(
                {
                    "id": id_appel,
                    "reference_appel": f"APP-{contrat.numero_contrat}-{date_echeance:%Y%m}",
                    "contrat_id": contrat.id,
                    "police_id": None,
                    "nombre_affilies": 1,
                    "periode_debut": periode_debut,
                    "periode_fin": periode_fin,
                    "date_echeance": date_echeance,
                    "montant_ht_cents": montant_ht,
                    "montant_taxe_cents": montant_taxe,
                    "montant_total_cents": montant_ht + montant_taxe,
                    "taux_taxe_bp": taux_taxe_bp,
                    "statut": statut_appel,
                    "bordereau_id": None,
                }
            )

            # Encaissement, quand il a lieu et quand le cabinet en a la delegation.
            if encaissement_delegue and statut_appel in (StatutAppel.ENCAISSE, StatutAppel.PARTIEL):
                borne_basse, borne_haute = DELAI_ENCAISSEMENT_JOURS[moyen.value]
                date_encaissement = date_echeance + timedelta(days=alea.randint(borne_basse, borne_haute))
                if date_encaissement > date_reference:
                    # Le reglement n'est pas encore arrive a la date de reference : l'appel est
                    # attendu, pas encaisse. Le laisser « encaisse » creerait un appel encaisse sans
                    # aucune ligne d'encaissement, et le module 2 commissionnerait dans le vide.
                    appels[-1]["statut"] = StatutAppel.ATTENDU
                else:
                    montant_total = montant_ht + montant_taxe
                    if statut_appel is StatutAppel.PARTIEL:
                        # Reglement partiel : entre la moitie et 90 pour cent de l'appel.
                        montant_encaisse = applique_taux(montant_total, alea.randrange(5_000, 9_000, 100))
                        statut_rapprochement = StatutRapprochement.PARTIEL
                    else:
                        montant_encaisse = montant_total
                        statut_rapprochement = StatutRapprochement.RAPPROCHE
                    compteurs['encaissement'] += 1
                    id_encaissement = compteurs['encaissement']
                    encaissements.append(
                        {
                            "id": id_encaissement,
                            "reference_bancaire": f"ENC-{date_encaissement:%Y%m%d}-{id_encaissement:07d}",
                            "appel_id": id_appel,
                            "contrat_id": contrat.id,
                            "date_encaissement": date_encaissement,
                            "montant_cents": montant_encaisse,
                            "moyen": moyen,
                            "statut_rapprochement": statut_rapprochement,
                            "libelle_releve": f"COTIS {contrat.numero_contrat} {date_echeance:%m/%Y}",
                        }
                    )

            periode_debut = ajoute_mois(periode_debut, pas)

    return appels, lignes, encaissements


def _nb_prestations_annuelles(alea: Random, age: int) -> int:
    """Nombre de prestations sur une annee, selon la tranche d'age."""
    for age_limite, (borne_basse, borne_haute) in PRESTATIONS_PAR_AN_SELON_AGE:
        if age <= age_limite:
            return alea.randint(borne_basse, borne_haute)
    return 0


@dataclass(frozen=True, slots=True)
class _TirageSoin:
    """Un soin tire au hasard, avant toute decision sur le plafond annuel.

    Separer le tirage de la decision est ce qui permet de cumuler le plafond dans l'ordre des dates :
    tous les aleas d'un contrat sont tires d'abord, le tri vient ensuite, et le plafond s'epuise alors
    chronologiquement. Les deux aleas qui ne servent qu'en seconde passe, le delai de reglement et le
    motif de rejet libre, sont tires ici pour que la suite aleatoire ne depende pas de l'ordre du tri.
    """

    date_soins: date
    garantie_id: int
    taux_remboursement_bp: int
    plafond_annuel_cents: int | None
    montant_demande: int
    rejetee: bool
    delai_reglement_jours: int
    motif_rejet_libre: str


def _genere_prestations(
    alea: Random,
    ref: Referentiel,
    contrats: list[ContratGenere],
    date_reference: date,
    delegation: DelegationDatee,
) -> list[dict]:
    """Prestations servies, avec la saisonnalite de la consommation de soins.

    La saisonnalite est portee par `POIDS_SAISON_PRESTATIONS` : creux en juillet et aout, pic de novembre
    a janvier. C'est le signal que le module de pilotage doit faire apparaitre sur la courbe mensuelle du
    ratio sinistres sur primes.
    """
    prestations: list[dict] = []
    id_prestation = 0
    # Cumul deja rembourse par contrat, garantie et annee civile. C'est ainsi qu'un plafond annuel
    # fonctionne : il s'epuise au fil des soins de l'annee, il ne s'applique pas a chaque
    # prestation prise isolement. Appliquer le plafond prestation par prestation, comme le faisait
    # la version precedente, revenait a offrir le plafond autant de fois qu'il y a de soins.
    cumul_annuel: dict[tuple[int, int, int], int] = {}

    # Table des couvertures, construite une seule fois. La lire par la relation ORM a l'interieur de la
    # boucle declencherait une requete par prestation, soit des dizaines de milliers d'allers-retours.
    couvertures: dict[tuple[int, int], tuple[int, int | None]] = {}
    for formules in ref.formules.values():
        for formule in formules:
            for couverture in formule.couvertures:
                couvertures[(formule.id, couverture.garantie_id)] = (
                    couverture.taux_remboursement_bp,
                    couverture.plafond_annuel_cents,
                )

    for contrat in contrats:
        if contrat.statut is StatutContrat.DEVIS:
            continue
        if delegation.jamais_delegue(contrat.produit_code, ActeGestion.GESTION_PRESTATIONS):
            continue
        fenetre = _periode_de_garantie(contrat, date_reference)
        if fenetre is None:
            continue
        debut_garantie, fin_garantie = fenetre

        code_branche = ref.code_branche_de(contrat.produit_code)
        codes_garanties = GARANTIES_PAR_BRANCHE[code_branche]
        jours_de_garantie = (fin_garantie - debut_garantie).days
        if jours_de_garantie <= 0:
            continue

        age = age_a_la_date(contrat.adherent_naissance, debut_garantie)
        nb_par_an = _nb_prestations_annuelles(alea, age)
        facteur_consommation = _facteur_de_consommation(alea)
        nb_total = max(0, round(nb_par_an * jours_de_garantie / 365))

        # Premiere passe : les tirages aleatoires, dans leur ordre de tirage. Rien n'est decide ici sur
        # le plafond, parce que le plafond depend de l'ordre des dates et non de celui des tirages.
        tirages: list[_TirageSoin] = []
        for _ in range(nb_total):
            # Le mois est tire selon la saisonnalite, puis replie dans la fenetre de garantie.
            mois_tire = tire_pondere_indices(alea, POIDS_SAISON_PRESTATIONS) + 1
            # Toutes les annees de la garantie : limiter le tirage aux quatre premieres annees du contrat
            # concentrait les soins d'un contrat ancien sur ses debuts, et un contrat de 2019 ne
            # consommait plus rien en 2025.
            annees_possibles = list(range(debut_garantie.year, fin_garantie.year + 1))
            # Le jour est tire d'abord, puis la date complete est confrontee a la fenetre de
            # garantie. Filtrer sur le premier du mois laissait passer des soins posterieurs a la
            # resiliation, donc des prestations servies sur un contrat deja sorti.
            candidates = [
                jour
                for annee in annees_possibles
                for jour in [date(annee, mois_tire, alea.randint(1, 28))]
                if debut_garantie <= jour < fin_garantie
            ]
            if not candidates:
                continue
            date_soins = alea.choice(candidates)

            # Aucune prestation pendant une suspension de garanties : la garantie ne court pas, et un
            # remboursement servi a cette date serait une anomalie de gestion que la compagnie
            # reprendrait au cabinet.
            if any(debut <= date_soins < fin for debut, fin in contrat.suspensions):
                continue

            # La gestion des prestations doit etre deleguee **a la date des soins**.
            if not delegation.autorise(
                contrat.produit_code, ActeGestion.GESTION_PRESTATIONS, date_soins
            ):
                continue

            code_garantie = alea.choice(codes_garanties)
            garantie = ref.garanties[code_garantie]
            # Formule en vigueur a la date des soins, et non formule actuelle : un adherent passe en
            # formule confort en 2026 ne doit pas voir ses soins de 2024 rembourses au taux de 2026.
            formule_id = _valeur_a_la_date(contrat.paliers_formule, date_soins)
            couverture = couvertures.get((formule_id, garantie.id))
            if couverture is None:
                continue
            taux_remboursement_bp, plafond_annuel_cents = couverture

            tirages.append(
                _TirageSoin(
                    date_soins=date_soins,
                    garantie_id=garantie.id,
                    taux_remboursement_bp=taux_remboursement_bp,
                    plafond_annuel_cents=plafond_annuel_cents,
                    montant_demande=_montant_demande(alea, code_garantie),
                    rejetee=alea.random() < PART_PRESTATION_REJETEE,
                    delai_reglement_jours=alea.randint(*DELAI_REGLEMENT_JOURS),
                    motif_rejet_libre=alea.choice(MOTIFS_REJET),
                )
            )

        # Le plafond annuel s'epuise **dans l'ordre des dates de soins**, et c'est tout l'objet de ce
        # tri. Les soins sont tires au hasard dans la fenetre de garantie, donc dans un ordre qui n'a
        # rien de chronologique ; cumuler dans l'ordre de tirage produisait des donnees qu'un
        # gestionnaire relit comme fausses. Mesure faite avant correction : 55 pour cent des rejets
        # pour plafond atteint tombaient a une date ou le plafond n'etait pas encore consomme, et
        # 70 prestations reglees depassaient le reste de plafond a leur date. Le tri est stable, et les
        # identifiants sont attribues apres lui : la reference d'une prestation suit donc l'ordre des
        # soins du contrat.
        tirages = _calibre_sur_la_cotisation(
            tirages, contrat, debut_garantie, fin_garantie, ECHELLE_SINISTRALITE[code_branche] * facteur_consommation
        )
        tirages.sort(key=lambda tirage: tirage.date_soins)

        for tirage in tirages:
            id_prestation += 1
            date_soins = tirage.date_soins
            commun = {
                "id": id_prestation,
                "reference_prestation": f"PRE-{date_soins:%Y%m}-{id_prestation:07d}",
                "contrat_id": contrat.id,
                "adherent_id": contrat.adherent_id,
                "garantie_id": tirage.garantie_id,
                "date_soins": date_soins,
                "montant_demande_cents": tirage.montant_demande,
                "bordereau_id": None,
            }

            if tirage.rejetee:
                prestations.append(
                    {
                        **commun,
                        "date_reglement": None,
                        "montant_rembourse_cents": 0,
                        "statut": StatutPrestation.REJETEE,
                        "motif_rejet": tirage.motif_rejet_libre,
                    }
                )
                continue

            rembourse = applique_taux(tirage.montant_demande, tirage.taux_remboursement_bp)
            rembourse = min(rembourse, tirage.montant_demande)
            cle = (contrat.id, tirage.garantie_id, date_soins.year)
            plafond_atteint = False
            if tirage.plafond_annuel_cents is not None:
                reste = max(0, tirage.plafond_annuel_cents - cumul_annuel.get(cle, 0))
                if rembourse > reste:
                    rembourse = reste
                    plafond_atteint = True

            if plafond_atteint and rembourse == 0:
                prestations.append(
                    {
                        **commun,
                        "date_reglement": None,
                        "montant_rembourse_cents": 0,
                        "statut": StatutPrestation.REJETEE,
                        "motif_rejet": "plafond annuel de la garantie atteint",
                    }
                )
                continue

            date_reglement = date_soins + timedelta(days=tirage.delai_reglement_jours)
            reglee = date_reglement <= date_reference

            # Le plafond n'est consomme que par ce qui est reellement regle. Une prestation encore en
            # cours de traitement porte un montant rembourse nul en base ; si elle consommait quand
            # meme le plafond, aucune requete partant des donnees ne pourrait retrouver le
            # raisonnement, et un controle de coherence honnete deviendrait impossible a ecrire.
            if tirage.plafond_annuel_cents is not None and reglee:
                cumul_annuel[cle] = cumul_annuel.get(cle, 0) + rembourse

            prestations.append(
                {
                    **commun,
                    "date_reglement": date_reglement if reglee else None,
                    "montant_rembourse_cents": rembourse if reglee else 0,
                    "statut": StatutPrestation.REGLEE if reglee else StatutPrestation.EN_COURS,
                    "motif_rejet": None,
                }
            )

    return prestations


_MOYENNE_PROFILS = sum(poids * (bas + haut) / 2 for poids, bas, haut in PROFILS_CONSOMMATION) / sum(
    poids for poids, _bas, _haut in PROFILS_CONSOMMATION
)


def _facteur_de_consommation(alea: Random) -> float:
    """Multiplicateur de consommation d'un contrat, de moyenne un, a queue lourde.

    Tire une seule fois par contrat, avant tout autre alea de ses soins, pour que la suite aleatoire ne
    depende pas du nombre de soins retenus.
    """
    indice = tire_pondere_indices(alea, [poids for poids, _bas, _haut in PROFILS_CONSOMMATION])
    _poids, bas, haut = PROFILS_CONSOMMATION[indice]
    return alea.uniform(bas, haut) / _MOYENNE_PROFILS


def _cotisation_garantie_par_annee(
    contrat: ContratGenere, debut_garantie: date, fin_garantie: date
) -> dict[int, float]:
    """Cotisation mensuelle hors taxe, en centimes, **courue** par annee civile pendant la garantie.

    Mois par mois, au tarif en vigueur le premier du mois, au prorata des jours de garantie du mois et hors
    suspension : une garantie suspendue ne donne lieu a aucun appel, donc a aucune cotisation courue.
    """
    par_annee: dict[int, float] = {}
    mois = debut_garantie.replace(day=1)
    while mois < fin_garantie:
        suivant = ajoute_mois(mois, 1)
        debut = max(mois, debut_garantie)
        fin = min(suivant, fin_garantie)
        jours = (fin - debut).days
        for debut_suspension, fin_suspension in contrat.suspensions:
            jours -= max(0, (min(fin, fin_suspension) - max(debut, debut_suspension)).days)
        if jours > 0:
            tarif = _valeur_a_la_date(contrat.paliers_cotisation, mois)
            par_annee[mois.year] = par_annee.get(mois.year, 0.0) + tarif * jours / (suivant - mois).days
        mois = suivant
    return par_annee


def _calibre_sur_la_cotisation(
    tirages: list[_TirageSoin],
    contrat: ContratGenere,
    debut_garantie: date,
    fin_garantie: date,
    sinistralite_du_contrat: float,
) -> list[_TirageSoin]:
    """Met a l'echelle les montants demandes pour que les remboursements suivent la cotisation du contrat.

    Sur toute la duree de garantie du contrat : le remboursement theorique des soins non rejetes (taux de
    la formule en vigueur, avant plafond) est rendu egal a `sinistralite_du_contrat` fois la cotisation
    courue. Le rapport des prestations aux cotisations est donc porte par chaque contrat, a sa propre
    echelle, au lieu d'un facteur global. Le calibrage porte sur la duree entiere et non sur chaque annee,
    pour garder des annees sans aucun soin : un contrat jeune n'en a pas chaque annee, et les imposer
    gonflerait la consommation des contrats qui consomment peu. Les plafonds, les rejets et les
    reglements en cours, appliques ensuite, retranchent leur part. Les montants restent multiples de
    50 centimes, comme les tirages d'origine, et d'au moins 50 centimes.
    """
    cotisation_courue = sum(_cotisation_garantie_par_annee(contrat, debut_garantie, fin_garantie).values())
    theorique = sum(
        applique_taux(tirage.montant_demande, tirage.taux_remboursement_bp)
        for tirage in tirages
        if not tirage.rejetee
    )
    if theorique <= 0 or cotisation_courue <= 0:
        return tirages
    echelle = sinistralite_du_contrat * cotisation_courue / theorique
    return [
        replace(tirage, montant_demande=max(50, round(tirage.montant_demande * echelle / 50) * 50))
        for tirage in tirages
    ]


def _montant_demande(alea: Random, code_garantie: str) -> int:
    """Montant demande, en centimes, selon le poste de garantie.

    Les ordres de grandeur sont des hypotheses de simulation : des soins courants a quelques dizaines
    d'euros, une hospitalisation a plusieurs centaines, un capital deces a plusieurs milliers.
    """
    bornes = {
        "SOINS": (1_500, 12_000),
        "HOSPI": (20_000, 180_000),
        "OPTIQUE": (8_000, 45_000),
        "DENTAIRE": (6_000, 120_000),
        "AUDIO": (40_000, 160_000),
        "DECES": (500_000, 4_000_000),
        "RENTE-EDU": (80_000, 400_000),
        "INCAP": (30_000, 250_000),
        "INVAL": (60_000, 500_000),
    }
    borne_basse, borne_haute = bornes[code_garantie]
    return alea.randrange(borne_basse, borne_haute + 1, 50)
