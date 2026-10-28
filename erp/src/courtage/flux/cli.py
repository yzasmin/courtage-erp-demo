"""Commandes en ligne de commande de la chaine des flux financiers.

Pourquoi la chaine est pilotee par des commandes et non par du code importe depuis le graphe Airflow.
Un graphe qui importe le code metier impose a l'orchestrateur et a l'application de partager leurs
dependances, et Airflow contraint les siennes de facon stricte. L'installation devient alors fragile, et
la premiere montee de version la casse. Ici, le graphe appelle `courtage flux <etape>` dans un
environnement virtuel dedie : les deux mondes ne se rencontrent qu'a travers une ligne de commande et un
code de retour.

Consequence utile pour la demonstration : chaque etape du graphe s'execute aussi a la main, sur le poste,
sans Docker ni Airflow. C'est ce qui permet a la suite de tests de verifier le rejet integral et
l'idempotence en quelques secondes.

Chaque commande ecrit son compte rendu dans `results/`. Tout chiffre publie dans la documentation vient
de l'un de ces fichiers.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

from courtage.config import RACINE_PROJET, parametres
from courtage.db import session_portee
from courtage.flux import bordereaux as module_bordereaux
from courtage.flux import commissions as module_commissions
from courtage.flux import ingestion, journal, rapport
from courtage.flux.generateur_fichiers import genere_fichiers
from courtage.flux.rapprochement import rapproche


def _fenetre_valide(valeur: str) -> int:
    """Valide la fenetre de rapprochement. Une valeur negative desactiverait la seconde methode.

    Sans cette validation, `--fenetre-jours -1` ne trouvait plus aucun candidat par montant et date, et
    tous les mouvements sans reference devenaient « non trouves », sans la moindre erreur.
    """
    nombre = int(valeur)
    if nombre < 0:
        raise argparse.ArgumentTypeError(
            f"fenetre negative : {nombre}. Une fenetre se compte en jours autour de l'echeance."
        )
    return nombre


def dossier_resultats() -> Path:
    """Dossier ou sont ecrits les comptes rendus.

    Lu dans l'environnement, et c'est necessaire plutot que confortable. `RACINE_PROJET` est deduite de
    l'emplacement du module : dans un depot clone elle vaut bien la racine du depot, mais pour un paquet
    **installe** elle vaut le dossier des bibliotheques de l'environnement virtuel. Les comptes rendus
    produits par les taches du graphe Airflow atterrissaient donc dans l'environnement virtuel du
    conteneur, hors de tout volume, et disparaissaient avec lui. Le defaut a ete constate en executant
    le graphe, pas en relisant le code.
    """
    valeur = os.environ.get("COURTAGE_RESULTATS")
    return Path(valeur) if valeur else RACINE_PROJET / "results"


def _ecrit(nom: str, contenu: dict) -> Path:
    """Enregistre un compte rendu dans le dossier des resultats, en JSON lisible."""
    dossier = dossier_resultats()
    dossier.mkdir(parents=True, exist_ok=True)
    chemin = dossier / nom
    chemin.write_text(json.dumps(contenu, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return chemin


#: Clefs trop volumineuses pour le journal de la tache : elles restent dans le fichier JSON.
CLEFS_VOLUMINEUSES = ("details", "lots", "fichiers", "bordereaux_en_base", "noms_redeposes")


def _affiche(titre: str, contenu: dict, chemin: Path) -> None:
    """Affiche le compte rendu, valeurs simples **et** ventilations.

    La premiere version n'imprimait que les valeurs scalaires. Les journaux des taches Airflow
    affichaient donc « lots_ignores 3 » sans dire lesquels, et ne montraient jamais la ventilation des
    appels ecartes ni celle des statuts de rapprochement. Or le journal de la tache est le premier
    endroit ou un gestionnaire regarde quand un mois lui parait faux.
    """
    print(titre)
    for cle, valeur in contenu.items():
        if cle in CLEFS_VOLUMINEUSES:
            continue
        if isinstance(valeur, str | int | float) or valeur is None:
            print(f"  {cle:36s} {valeur}")
        elif isinstance(valeur, dict):
            print(f"  {cle} :")
            for sous_cle, sous_valeur in valeur.items():
                print(f"      {sous_cle:32s} {sous_valeur}")
        elif isinstance(valeur, list) and valeur:
            print(f"  {cle:36s} {', '.join(str(element) for element in valeur[:12])}")
    print(f"compte rendu : {chemin}")


def commande_generer(args: argparse.Namespace) -> int:
    """Produit les fichiers recus et le manifeste de verite terrain."""
    graine = args.graine if args.graine is not None else parametres.graine
    debut = time.monotonic()
    with session_portee() as session:
        resultat = genere_fichiers(session, graine=graine)
    contenu = resultat.en_dictionnaire()
    contenu["duree_s"] = round(time.monotonic() - debut, 2)
    chemin = _ecrit("flux_generation.json", contenu)
    _affiche("fichiers recus generes", contenu, chemin)
    return 0


def commande_detecter(args: argparse.Namespace) -> int:
    """Inscrit au journal les fichiers presents dans le dossier d'arrivee."""
    with session_portee() as session:
        compte_rendu = ingestion.detecte(session)
    contenu = compte_rendu.en_dictionnaire()
    chemin = _ecrit("flux_detection.json", contenu)
    _affiche("detection", contenu, chemin)
    return 0


def commande_archiver(args: argparse.Namespace) -> int:
    """Archive le brut de chaque lot detecte et verifie son empreinte."""
    with session_portee() as session:
        compte_rendu = ingestion.archive(session)
    contenu = compte_rendu.en_dictionnaire()
    chemin = _ecrit("flux_archivage.json", contenu)
    _affiche("archivage", contenu, chemin)
    return 0


def commande_charger(args: argparse.Namespace) -> int:
    """Applique les controles, puis charge la couche brute ou rejette le lot entier."""
    with session_portee() as session:
        compte_rendu = ingestion.charge(session)
    contenu = compte_rendu.en_dictionnaire()
    chemin = _ecrit("flux_chargement.json", contenu)
    _affiche("chargement de la couche brute", contenu, chemin)
    return 0


def commande_rapprocher(args: argparse.Namespace) -> int:
    """Rapproche les encaissements des appels, par reference puis par montant et date."""
    with session_portee() as session:
        resultat = rapproche(session, fenetre_jours=args.fenetre_jours)
    contenu = resultat.en_dictionnaire()
    chemin = _ecrit("flux_rapprochement.json", contenu)
    _affiche("rapprochement", contenu, chemin)
    return 0


def commande_commissions(args: argparse.Namespace) -> int:
    """Calcule les commissions et les retrocessions au taux valide a la date d'echeance."""
    periodes = [date.fromisoformat(valeur) for valeur in args.periode] if args.periode else None
    with session_portee() as session:
        resultat = module_commissions.calcule(session, periodes=periodes)
    contenu = resultat.en_dictionnaire()
    chemin = _ecrit("flux_commissions.json", contenu)
    _affiche("commissions et retrocessions", contenu, chemin)
    return 0


def commande_bordereaux(args: argparse.Namespace) -> int:
    """Produit les bordereaux mensuels, par compagnie et par apporteur."""
    periodes = [date.fromisoformat(valeur) for valeur in args.periode] if args.periode else None
    with session_portee() as session:
        resultat = module_bordereaux.produit(session, periodes=periodes)
        contenu = resultat.en_dictionnaire()
        contenu["bordereaux_en_base"] = module_bordereaux.bordereaux_en_base(session)
    chemin = _ecrit("flux_bordereaux.json", contenu)
    _affiche("bordereaux mensuels", contenu, chemin)
    return 0


def commande_rapport(args: argparse.Namespace) -> int:
    """Compare les anomalies detectees au manifeste et publie rappel et precision."""
    with session_portee() as session:
        resultat = rapport.construit_rapport(session)
    contenu = resultat.en_dictionnaire()
    chemin = _ecrit("flux_rapport_detection.json", contenu)
    ensemble = contenu["ensemble"]
    print("rapport de detection des anomalies")
    print(f"  injectees                            {ensemble['injectees']}")
    print(f"  signalees                            {ensemble['signalees']}")
    print(f"  vrais positifs                       {ensemble['vrais_positifs']}")
    print(f"  faux positifs                        {ensemble['faux_positifs']}")
    print(f"  faux negatifs                        {ensemble['faux_negatifs']}")
    print(f"  rappel                               {ensemble['rappel']}")
    print(f"  precision                            {ensemble['precision']}")
    print(f"compte rendu : {chemin}")
    return 0


def commande_journal(args: argparse.Namespace) -> int:
    """Reconstruit le journal d'execution par lot a partir de la base."""
    with session_portee() as session:
        contenu = journal.construit_journal(session)
    chemin = _ecrit("flux_journal_lots.json", contenu)
    print("journal d'execution par lot")
    print(f"  lots                                 {contenu['nombre_lots']}")
    for statut, nombre in contenu["lots_par_statut"].items():  # type: ignore[union-attr]
        print(f"    {statut:35s} {nombre}")
    print(f"  lignes chargees                      {contenu['lignes_chargees']}")
    print(f"  anomalies bloquantes                 {contenu['anomalies_bloquantes']}")
    print(f"  anomalies d'avertissement            {contenu['anomalies_avertissement']}")
    print(f"compte rendu : {chemin}")
    return 0


def commande_alerter(args: argparse.Namespace) -> int:
    """Sort en echec quand un lot a ete rejete depuis l'horodatage donne. C'est l'alerte du graphe.

    Pourquoi cette commande existe, et pourquoi elle est separee des etapes de la chaine.

    Un lot rejete n'est pas un echec technique : les controles ont fait exactement leur travail, les
    autres lots du meme depot ont ete charges, et les commissions ont ete calculees dessus. Faire
    echouer la tache `charger` serait donc faux deux fois : elle a reussi, et son echec priverait les
    lots sains de la suite de la chaine.

    Mais la specification exige que le rejet soit **visible dans Airflow**, et un graphe entierement
    vert quand neuf bordereaux sur vingt-sept ont ete refuses ne previent personne. Un bordereau refuse
    demande un appel a la compagnie, pas un classement sans suite.

    Le mecanisme retenu est donc explicite : une tache terminale dediee,
    `comptes_rendus.alerter_sur_les_lots_rejetes`, qui s'execute **apres** que tout le reste a tourne,
    et qui echoue si le depot du jour contient un lot rejete. Le graphe devient rouge, l'alerte part,
    et rien n'a ete empeche. La tache ne se reessaie pas : rejouer un rejet metier ne changerait rien,
    la correction vient de l'emetteur qui renvoie un fichier different.

    La fenetre par defaut couvre les vingt-quatre dernieres heures, parce que la chaine traite le
    courrier du jour : un lot rejete avant-hier et corrige depuis ne doit plus faire rougir le graphe
    d'aujourd'hui.

    Elle est bornee sur l'**horloge de la base**, et non sur celle du poste, parce que `detecte_le` est
    rempli par `func.now()`, donc par le serveur. Les deux horloges ne portent pas le meme fuseau :
    SQLite rend `CURRENT_TIMESTAMP` en temps universel tandis que PostgreSQL suit le fuseau de son
    serveur, et un minuit local calcule en Python aurait laisse passer les rejets de la nuit sans
    qu'aucune alerte ne parte. Lire les deux bornes sur la meme horloge supprime la question.
    """
    with session_portee() as session:
        depuis = args.depuis if getattr(args, "depuis", None) else journal.debut_de_fenetre(session)
        rejetes = journal.lots_rejetes_depuis(session, depuis)

    contenu = {
        "depuis": depuis.isoformat(),
        "lots_rejetes": len(rejetes),
        "detail": rejetes,
    }
    chemin = _ecrit("flux_alerte_rejets.json", contenu)
    print(f"alerte sur les lots rejetes depuis le {depuis:%d/%m/%Y a %H:%M}, horloge de la base")
    for lot in rejetes:
        print(f"  {lot['reference_lot']:45s} {lot['motif_rejet']}")
    print(f"  lots rejetes                         {len(rejetes)}")
    print(f"compte rendu : {chemin}")
    if rejetes:
        print(
            f"ECHEC VOLONTAIRE : {len(rejetes)} lot(s) rejete(s) par les controles bloquants."
            " Les fichiers et leur rapport d'anomalies sont dans le dossier des rejets, a renvoyer a"
            " leur emetteur. Les lots sains du meme depot ont ete traites normalement.",
            file=sys.stderr,
        )
        return 1
    print("aucun lot rejete sur la fenetre : rien a signaler")
    return 0


def commande_chaine(args: argparse.Namespace) -> int:
    """Enchaine toutes les etapes, hors `dbt build`, dans l'ordre du graphe Airflow.

    Sert la demonstration sans Docker et les preuves d'idempotence. Le graphe Airflow, lui, appelle les
    etapes une par une : c'est ce qui rend la reprise sur echec possible au bon endroit.
    """
    for commande in (
        commande_detecter,
        commande_archiver,
        commande_charger,
        commande_rapprocher,
        commande_commissions,
        commande_bordereaux,
        commande_journal,
    ):
        code = commande(args)
        if code != 0:
            return code
    return 0


def ajoute_commandes(sous_analyseurs) -> None:
    """Greffe le groupe de commandes `flux` sur l'analyseur principal du client."""
    flux = sous_analyseurs.add_parser(
        "flux", help="chaine des flux financiers : fichiers recus, controles, commissions"
    )
    actions = flux.add_subparsers(dest="action", required=True)

    generer = actions.add_parser("generer", help="genere les fichiers recus et leur manifeste de verite terrain")
    generer.add_argument("--graine", type=int, default=None, help="graine du tirage, par defaut celle des parametres")
    generer.set_defaults(fonction=commande_generer)

    actions.add_parser("detecter", help="inscrit au journal les fichiers du dossier d'arrivee").set_defaults(
        fonction=commande_detecter
    )
    actions.add_parser("archiver", help="archive le brut et verifie l'empreinte SHA-256").set_defaults(
        fonction=commande_archiver
    )
    actions.add_parser("charger", help="controle puis charge la couche brute, ou rejette le lot entier").set_defaults(
        fonction=commande_charger
    )

    rapprocher = actions.add_parser("rapprocher", help="rapproche les encaissements des appels de cotisation")
    rapprocher.add_argument(
        "--fenetre-jours",
        type=_fenetre_valide,
        default=None,
        help="fenetre de tolerance du rapprochement par montant et date, en jours",
    )
    rapprocher.set_defaults(fonction=commande_rapprocher)

    commissions = actions.add_parser("commissions", help="calcule les commissions et les retrocessions")
    commissions.add_argument(
        "--periode", action="append", default=None, help="mois a calculer, au format AAAA-MM-01, repetable"
    )
    commissions.set_defaults(fonction=commande_commissions)

    bordereaux = actions.add_parser("bordereaux", help="produit les bordereaux mensuels en base et en CSV")
    bordereaux.add_argument(
        "--periode", action="append", default=None, help="mois a produire, au format AAAA-MM-01, repetable"
    )
    bordereaux.set_defaults(fonction=commande_bordereaux)

    actions.add_parser("rapport", help="rappel et precision des anomalies, contre le manifeste").set_defaults(
        fonction=commande_rapport
    )
    actions.add_parser("journal", help="journal d'execution par lot").set_defaults(fonction=commande_journal)

    alerter = actions.add_parser(
        "alerter", help="sort en echec si un lot a ete rejete, pour rendre le rejet visible dans Airflow"
    )
    alerter.add_argument(
        "--depuis",
        type=datetime.fromisoformat,
        default=None,
        help="horodatage ISO a partir duquel compter les rejets, par defaut il y a vingt-quatre heures",
    )
    alerter.set_defaults(fonction=commande_alerter)

    chaine = actions.add_parser("chaine", help="enchaine toutes les etapes, hors dbt")
    chaine.add_argument("--fenetre-jours", type=_fenetre_valide, default=None)
    chaine.add_argument("--periode", action="append", default=None)
    chaine.set_defaults(fonction=commande_chaine)
