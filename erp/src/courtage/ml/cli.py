"""Commandes `courtage ml` : apprentissage avec evaluation temporelle, puis scoring mensuel.

Meme principe que `courtage flux` : le graphe Airflow appelle une ligne de commande dans un environnement
virtuel dedie, et chaque commande ecrit son compte rendu dans `results/`. Les metriques publiees dans
docs/eval-resiliation.md viennent de `results/ml_resiliation.json`, et de nulle part ailleurs.
"""

from __future__ import annotations

import os
import time
from datetime import date
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sqlalchemy import delete

from courtage.config import RACINE_PROJET
from courtage.db import moteur, session_portee
from courtage.flux.cli import _ecrit
from courtage.ml import resiliation as r
from courtage.modeles import ScoreResiliation


def chemin_modele() -> Path:
    return Path(os.environ.get("COURTAGE_MODELE_ML", RACINE_PROJET / "ml" / "modele_resiliation.joblib"))


def commande_entrainer(_args) -> int:
    debut = time.perf_counter()
    donnees = r.charge_variables(moteur(), [*r.DATES_APPRENTISSAGE, r.DATE_TEST])
    apprentissage = donnees[donnees["date_observation"].isin(r.DATES_APPRENTISSAGE)].reset_index(drop=True)
    test = donnees[donnees["date_observation"] == r.DATE_TEST].reset_index(drop=True)
    if apprentissage[r.CIBLE].isna().any() or test[r.CIBLE].isna().any():
        print("cible manquante sur une date d'apprentissage ou de test : la fenetre de douze mois n'est pas observee")
        return 1

    modele = r.entraine(apprentissage)
    y_test = test[r.CIBLE].astype(int).to_numpy()
    contributions = modele.contributions(test)

    chemin = chemin_modele()
    chemin.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(modele, chemin)

    compte_rendu = {
        "version_modele": modele.version,
        "graine": r.GRAINE,
        "dates_apprentissage": [d.isoformat() for d in r.DATES_APPRENTISSAGE],
        "date_test": r.DATE_TEST.isoformat(),
        "lignes_apprentissage": len(apprentissage),
        "resiliations_apprentissage": int(apprentissage[r.CIBLE].sum()),
        "lignes_test": len(test),
        "resiliations_test": int(y_test.sum()),
        "variables": r.VARIABLES,
        "hyperparametres_lgbm": {k: v for k, v in r.PARAMETRES_LGBM.items() if k != "verbose"},
        "modele_deploye": "reference_logistique",
        "reference_logistique": r.mesures(y_test, modele.probabilites(test)),
        "lightgbm": r.mesures(y_test, modele.probabilites_lgbm(test)),
        "plafond_theorique_formule_du_generateur": r.mesures(y_test, r.probabilites_du_generateur(test)),
        "rapports_de_cotes_logistique": r.coefficients_logistique(modele.logistique),
        "importance_shap_globale_logistique": r.importance_globale(contributions),
        "importance_shap_globale_lightgbm": r.importance_globale(modele.contributions_lgbm(test)),
        "causes_injectees_contre_modele": r.effet_par_groupe(test, contributions),
        "duree_secondes": round(time.perf_counter() - debut, 1),
    }
    _ecrit("ml_resiliation.json", compte_rendu)
    print(
        f"test {r.DATE_TEST} : ROC-AUC logistique {compte_rendu['reference_logistique']['roc_auc']}, "
        f"LightGBM {compte_rendu['lightgbm']['roc_auc']} ; modele ecrit dans {chemin}"
    )
    return 0


def commande_scorer(args) -> int:
    modele: r.ModeleEntraine = joblib.load(chemin_modele())
    date_observation: date = args.date or r.derniere_date_observation(moteur())
    donnees = r.charge_variables(moteur(), [date_observation]).reset_index(drop=True)
    if donnees.empty:
        print(f"aucun contrat au portefeuille le {date_observation}")
        return 1

    probabilites = modele.probabilites(donnees)
    contributions = modele.contributions(donnees)
    # Decile 10 = les 10 % les plus risques du portefeuille du mois.
    deciles = pd.qcut(pd.Series(probabilites).rank(method="first"), 10, labels=False).to_numpy() + 1

    lignes = []
    for i, ligne in donnees.iterrows():
        facteurs = [*r.trois_facteurs(ligne, contributions.loc[i]), None, None, None]
        lignes.append(
            ScoreResiliation(
                contrat_id=int(ligne["contrat_id"]),
                date_observation=date_observation,
                probabilite=float(probabilites[i]),
                decile=int(deciles[i]),
                facteur_1=facteurs[0],
                facteur_2=facteurs[1],
                facteur_3=facteurs[2],
                version_modele=modele.version,
            )
        )
    # Rejouer le scoring d'un mois remplace ses scores au lieu de les doubler.
    with session_portee() as session:
        session.execute(delete(ScoreResiliation).where(ScoreResiliation.date_observation == date_observation))
        session.add_all(lignes)

    _ecrit(
        "ml_scoring.json",
        {
            "date_observation": date_observation.isoformat(),
            "version_modele": modele.version,
            "contrats_scores": len(lignes),
            "probabilite_moyenne": round(float(np.mean(probabilites)), 4),
            "probabilite_seuil_decile_10": round(float(np.min(probabilites[deciles == 10])), 4),
        },
    )
    print(f"{len(lignes)} contrats scores au {date_observation}")
    return 0


def ajoute_commandes(sous_analyseurs) -> None:
    """Greffe le groupe de commandes `ml` sur l'analyseur principal du client."""
    ml = sous_analyseurs.add_parser("ml", help="prediction de resiliation : apprentissage et scoring")
    actions = ml.add_subparsers(dest="action", required=True)
    actions.add_parser(
        "entrainer", help="apprend sur 2023 et 2024, mesure sur 2025, ecrit results/ml_resiliation.json"
    ).set_defaults(fonction=commande_entrainer)
    scorer = actions.add_parser("scorer", help="score le portefeuille a une date d'observation")
    scorer.add_argument("--date", type=date.fromisoformat, default=None, help="par defaut la derniere disponible")
    scorer.set_defaults(fonction=commande_scorer)
