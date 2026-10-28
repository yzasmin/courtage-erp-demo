"""Commandes du projet : migrer, generer, empreinte, verifier, servir.

Un seul point d'entree pour le poste et pour le conteneur. Le compose appelle exactement les memes
commandes qu'un developpeur sur son poste, ce qui evite qu'un chemin fonctionne et l'autre pas.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

from alembic import command
from alembic.config import Config

from courtage.config import RACINE_PROJET, parametres
from courtage.db import moteur, session_portee
from courtage.flux.cli import ajoute_commandes as ajoute_commandes_flux
from courtage.generateur import base_est_vide, genere_tout
from courtage.generateur.empreinte import empreinte_complete

DOSSIER_RESULTATS = RACINE_PROJET / "results"


def _configuration_alembic() -> Config:
    config = Config(str(RACINE_PROJET / "alembic.ini"))
    config.set_main_option("script_location", str(RACINE_PROJET / "migrations"))
    return config


def _ecrit_resultat(nom: str, contenu: dict) -> Path:
    """Enregistre une sortie dans `results/`. Tout chiffre publie doit venir de l'un de ces fichiers."""
    DOSSIER_RESULTATS.mkdir(parents=True, exist_ok=True)
    chemin = DOSSIER_RESULTATS / nom
    chemin.write_text(json.dumps(contenu, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return chemin


def _moteur_lisible() -> str:
    return moteur().dialect.name


def commande_migrer(args: argparse.Namespace) -> int:
    """Monte le schema jusqu'a la revision demandee, ou le redescend."""
    config = _configuration_alembic()
    if args.vers == "base":
        command.downgrade(config, "base")
    else:
        command.upgrade(config, args.vers)
    print(f"schema a la revision {args.vers} sur {_moteur_lisible()}")
    return 0


def commande_generer(args: argparse.Namespace) -> int:
    """Genere le jeu de demonstration. Refuse d'ecrire sur une base non vide, sauf `--si-vide`."""
    date_reference = date.fromisoformat(args.date_reference or parametres.date_reference)
    graine = args.graine if args.graine is not None else parametres.graine

    with session_portee() as session:
        if not base_est_vide(session):
            if args.si_vide:
                print("la base contient deja des adherents : generation ignoree")
                return 0
            print(
                "la base contient deja des adherents. Relancer sur une base vide,"
                " par exemple `courtage migrer --vers base && courtage migrer`,"
                " ou utiliser --si-vide pour ne rien faire.",
                file=sys.stderr,
            )
            return 1

        debut = time.monotonic()
        resultat = genere_tout(session, graine=graine, date_reference=date_reference)
        duree = time.monotonic() - debut

    contenu = resultat.en_dictionnaire()
    contenu["moteur"] = _moteur_lisible()
    contenu["duree_generation_s"] = round(duree, 2)
    chemin = _ecrit_resultat("generation.json", contenu)

    print(f"genere en {duree:.1f} s sur {contenu['moteur']}, avec la graine {graine}")
    for cle in (
        "entreprises",
        "adherents",
        "contrats",
        "avenants",
        "polices_collectives",
        "appels_cotisation",
        "lignes_appel_cotisation",
        "encaissements",
        "prestations",
    ):
        print(f"  {cle:20s} {contenu[cle]:>9d}")
    if resultat.sequences_recalees:
        print(f"  sequences recalees   {len(resultat.sequences_recalees):>9d}")
    print(f"compte rendu : {chemin}")
    return 0


def commande_empreinte(args: argparse.Namespace) -> int:
    """Calcule l'empreinte SHA-256 du jeu de donnees et l'enregistre."""
    with session_portee() as session:
        empreinte = empreinte_complete(session)
    empreinte["moteur"] = _moteur_lisible()
    chemin = _ecrit_resultat(args.sortie, empreinte)
    print(f"empreinte globale : {empreinte['sha256_global']}")
    print(f"lignes prises en compte : {empreinte['total_lignes']}")
    print(f"detail par table : {chemin}")
    return 0


def commande_servir(args: argparse.Namespace) -> int:
    """Demarre l'API et l'interface de gestionnaire."""
    import uvicorn

    uvicorn.run("courtage.api.app:application", host=args.hote, port=args.port, log_level="info")
    return 0


def construit_analyseur() -> argparse.ArgumentParser:
    analyseur = argparse.ArgumentParser(
        prog="courtage",
        description="Mini-ERP de courtage sante et prevoyance. Commandes de gestion du projet.",
    )
    sous = analyseur.add_subparsers(dest="commande", required=True)

    migrer = sous.add_parser("migrer", help="monte ou descend le schema de la base")
    migrer.add_argument(
        "--vers",
        default="head",
        help="revision cible : head pour la derniere, base pour tout redescendre, ou un numero",
    )
    migrer.set_defaults(fonction=commande_migrer)

    generer = sous.add_parser("generer", help="genere le jeu de demonstration deterministe")
    generer.add_argument("--graine", type=int, default=None, help="graine du tirage, par defaut celle des parametres")
    generer.add_argument("--date-reference", default=None, help="fin de l'historique simule, au format AAAA-MM-JJ")
    generer.add_argument(
        "--si-vide", action="store_true", help="ne rien faire si la base contient deja des donnees"
    )
    generer.set_defaults(fonction=commande_generer)

    empreinte = sous.add_parser("empreinte", help="calcule l'empreinte SHA-256 du jeu de donnees")
    empreinte.add_argument("--sortie", default="empreinte.json", help="nom du fichier ecrit dans results/")
    empreinte.set_defaults(fonction=commande_empreinte)

    servir = sous.add_parser("servir", help="demarre l'API et l'interface")
    servir.add_argument("--hote", default="127.0.0.1")
    servir.add_argument("--port", type=int, default=8000)
    servir.set_defaults(fonction=commande_servir)

    # Les commandes de la chaine des flux financiers sont definies dans leur propre module, pour que le
    # module 2 n'alourdisse pas ce fichier et que son perimetre reste lisible.
    ajoute_commandes_flux(sous)
    # La prediction de resiliation n'existe que si l'option `ml` est installee : l'image de l'ERP s'en
    # passe, seuls le poste de developpement et l'image Airflow l'embarquent.
    try:
        from courtage.ml.cli import ajoute_commandes as ajoute_commandes_ml
    except ImportError:
        pass
    else:
        ajoute_commandes_ml(sous)
    # Meme principe pour l'agent de pre-remplissage et l'option `ia`.
    try:
        from courtage.ia.cli import ajoute_commandes as ajoute_commandes_ia
    except ImportError:
        pass
    else:
        ajoute_commandes_ia(sous)

    return analyseur


def main(argv: list[str] | None = None) -> int:
    analyseur = construit_analyseur()
    args = analyseur.parse_args(argv)
    return int(args.fonction(args))


if __name__ == "__main__":
    raise SystemExit(main())
