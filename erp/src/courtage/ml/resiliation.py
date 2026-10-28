"""Prediction de resiliation : un modele de reference interpretable, puis un modele plus performant.

Ce que ce module doit prouver n'est pas une performance : les donnees sont synthetiques, et leurs causes
de resiliation sont connues puisqu'elles sont ecrites dans le generateur. Il doit prouver une demarche.

- **Aucune fuite.** Les variables viennent du modele dbt `ml_variables_resiliation`, qui ne lit aucun
  avenant posterieur a la date d'observation, et un test dbt le reverifie a chaque construction.
- **Validation temporelle.** Apprentissage sur les observations du 1er janvier 2023 et 2024, test sur
  celle du 1er janvier 2025. Jamais de melange aleatoire : un modele de resiliation sert a predire
  l'annee qui vient, et c'est ainsi qu'il est mesure. Le 1er janvier 2026 n'est pas utilisable pour le
  test : sa fenetre de douze mois depasse la fin de l'historique.
- **Hyperparametres fixes**, choisis avant de regarder le test et jamais retouches ensuite.
- **Explicabilite.** Coefficients de la regression logistique pour la lecture globale ; valeurs SHAP
  exactes des arbres de LightGBM (`pred_contrib`) pour l'importance globale et pour les trois facteurs
  de chaque contrat affiches sur la fiche adherent.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import date

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.calibration import calibration_curve
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sqlalchemy import Engine, text

GRAINE = 20261019
VERSION_MODELE = "logistique-resiliation-v1"
DATES_APPRENTISSAGE = (date(2023, 1, 1), date(2024, 1, 1))
DATE_TEST = date(2025, 1, 1)

NUMERIQUES = [
    "age",
    "anciennete_mois",
    "cotisation_mensuelle_cents",
    "revalorisation_bp",
    "nb_suspensions",
    "nb_changements_formule",
]
BOOLEENNES = ["est_une_affiliation", "est_suspendu"]
CATEGORIELLES = ["code_produit", "code_branche", "code_apporteur"]
VARIABLES = NUMERIQUES + BOOLEENNES + CATEGORIELLES
CIBLE = "resilie_12_mois"

#: Hyperparametres de LightGBM, fixes a priori : des arbres peu profonds et beaucoup de lignes par
#: feuille, parce qu'il y a quelques milliers de lignes et quelques centaines de resiliations.
PARAMETRES_LGBM = dict(
    n_estimators=300,
    learning_rate=0.03,
    num_leaves=15,
    min_child_samples=50,
    subsample=0.8,
    subsample_freq=1,
    colsample_bytree=0.8,
    reg_lambda=1.0,
    random_state=GRAINE,
    deterministic=True,
    force_row_wise=True,
    verbose=-1,
)


def schema_variables() -> str:
    """Schema ou dbt ecrit la table des variables : le schema du profil suivi de `_ml`."""
    return os.environ.get("COURTAGE_SCHEMA_ML", "analytique_ml")


def charge_variables(moteur: Engine, dates: list[date] | None = None) -> pd.DataFrame:
    """Lit les variables aux dates demandees, ou a toutes les dates si `dates` est vide."""
    requete = f"select * from {schema_variables()}.ml_variables_resiliation"
    parametres: dict = {}
    if dates:
        marques = ", ".join(f":d{i}" for i in range(len(dates)))
        requete += f" where date_observation in ({marques})"
        parametres = {f"d{i}": d for i, d in enumerate(dates)}
    with moteur.connect() as connexion:
        donnees = pd.read_sql(text(requete), connexion, params=parametres)
    donnees["date_observation"] = pd.to_datetime(donnees["date_observation"]).dt.date
    return donnees


def derniere_date_observation(moteur: Engine) -> date:
    with moteur.connect() as connexion:
        return connexion.execute(
            text(f"select max(date_observation) from {schema_variables()}.ml_variables_resiliation")
        ).scalar_one()


def matrice(donnees: pd.DataFrame, categories: dict[str, list[str]]) -> pd.DataFrame:
    """Met les variables au format attendu par les deux modeles.

    Les modalites des variables categorielles sont figees a l'apprentissage. Un apporteur apparu depuis
    devient une valeur manquante, que les deux modeles savent traiter, au lieu de decaler les codes.
    """
    x = donnees[VARIABLES].copy()
    for colonne in BOOLEENNES:
        x[colonne] = x[colonne].astype(int)
    for colonne in CATEGORIELLES:
        x[colonne] = pd.Categorical(x[colonne], categories=categories[colonne])
    return x


def reference_logistique() -> Pipeline:
    """Regression logistique : la reference qu'un actuaire ou un controleur de gestion sait relire."""
    preparation = ColumnTransformer(
        [
            ("numeriques", StandardScaler(), NUMERIQUES + BOOLEENNES),
            ("categorielles", OneHotEncoder(handle_unknown="ignore"), CATEGORIELLES),
        ]
    )
    return Pipeline([("preparation", preparation), ("modele", LogisticRegression(max_iter=2000))])


def mesures(y: np.ndarray, p: np.ndarray) -> dict:
    """ROC-AUC, PR-AUC, score de Brier, calibration par decile et lift du premier decile."""
    ordre = np.argsort(-p, kind="stable")
    premier_decile = ordre[: math.ceil(len(p) / 10)]
    taux_base = float(y.mean())
    taux_decile = float(y[premier_decile].mean())
    observe, predit = calibration_curve(y, p, n_bins=10, strategy="quantile")
    return {
        "roc_auc": round(float(roc_auc_score(y, p)), 4),
        "pr_auc": round(float(average_precision_score(y, p)), 4),
        "brier": round(float(brier_score_loss(y, p)), 4),
        "taux_resiliation_observe": round(taux_base, 4),
        "taux_resiliation_premier_decile": round(taux_decile, 4),
        "lift_premier_decile": round(taux_decile / taux_base, 2),
        "probabilite_moyenne_predite": round(float(p.mean()), 4),
        "calibration_par_decile": [
            {"predite": round(float(a), 4), "observee": round(float(b), 4)}
            for a, b in zip(predit, observe, strict=True)
        ],
    }


@dataclass
class ModeleEntraine:
    """Les deux modeles appris ensemble. Le modele deploye, celui qui score, est la regression logistique.

    LightGBM devait etre le « modele plus performant ». Mesure sur l'annee de test, il ne l'est pas : meme
    ROC-AUC, PR-AUC et lift plus faibles, calibration moins bonne. Le generateur combine quelques causes
    par des facteurs multiplicatifs, soit exactement la forme qu'une regression logistique sait
    representer, et il n'y a rien de plus a trouver. On deploie donc le modele simple, et LightGBM reste
    calcule a chaque apprentissage comme comparaison : sur des donnees reelles, l'ecart pourrait changer
    de sens, et c'est la mesure qui tranchera. Voir docs/eval-resiliation.md.
    """

    logistique: Pipeline
    lgbm: lgb.LGBMClassifier
    categories: dict[str, list[str]]
    #: Moyenne de chaque colonne preparee sur l'apprentissage : le point de reference des contributions.
    moyennes: np.ndarray
    version: str = VERSION_MODELE

    def probabilites(self, donnees: pd.DataFrame) -> np.ndarray:
        return self.logistique.predict_proba(donnees[VARIABLES])[:, 1]

    def probabilites_lgbm(self, donnees: pd.DataFrame) -> np.ndarray:
        return self.lgbm.predict_proba(matrice(donnees, self.categories))[:, 1]

    def contributions(self, donnees: pd.DataFrame) -> pd.DataFrame:
        """Valeurs SHAP du modele deploye, en log-odds, une colonne par variable d'origine.

        Pour un modele lineaire, la valeur SHAP d'une colonne est exacte et sans calcul couteux : son
        coefficient multiplie par l'ecart de la valeur a sa moyenne d'apprentissage. Les colonnes d'une
        meme variable categorielle (une par apporteur, par exemple) sont additionnees.
        """
        preparation = self.logistique.named_steps["preparation"]
        x = preparation.transform(donnees[VARIABLES])
        x = x.toarray() if hasattr(x, "toarray") else np.asarray(x)
        par_colonne = (x - self.moyennes) * self.logistique.named_steps["modele"].coef_[0]
        noms = [nom.split("__", 1)[1] for nom in preparation.get_feature_names_out()]
        resultat = pd.DataFrame(0.0, columns=VARIABLES, index=donnees.index)
        for j, nom in enumerate(noms):
            variable = nom if nom in VARIABLES else next(c for c in CATEGORIELLES if nom.startswith(f"{c}_"))
            resultat[variable] += par_colonne[:, j]
        return resultat

    def contributions_lgbm(self, donnees: pd.DataFrame) -> pd.DataFrame:
        """Valeurs SHAP exactes des arbres de LightGBM, sans la colonne finale du biais."""
        brut = self.lgbm.predict(matrice(donnees, self.categories), pred_contrib=True)
        return pd.DataFrame(brut[:, :-1], columns=VARIABLES, index=donnees.index)


def entraine(donnees_apprentissage: pd.DataFrame) -> ModeleEntraine:
    categories = {c: sorted(donnees_apprentissage[c].dropna().unique().tolist()) for c in CATEGORIELLES}
    y = donnees_apprentissage[CIBLE].astype(int).to_numpy()
    lgbm = lgb.LGBMClassifier(**PARAMETRES_LGBM).fit(matrice(donnees_apprentissage, categories), y)
    logistique = reference_logistique().fit(donnees_apprentissage[VARIABLES], y)
    x = logistique.named_steps["preparation"].transform(donnees_apprentissage[VARIABLES])
    moyennes = np.asarray((x.toarray() if hasattr(x, "toarray") else x).mean(axis=0)).ravel()
    return ModeleEntraine(logistique, lgbm, categories, moyennes)


def coefficients_logistique(logistique: Pipeline) -> list[dict]:
    """Rapports de cotes : par ecart type pour les variables numeriques, par modalite pour les autres."""
    noms = logistique.named_steps["preparation"].get_feature_names_out()
    coefficients = logistique.named_steps["modele"].coef_[0]
    lignes = [
        {"variable": nom.split("__", 1)[1], "rapport_de_cotes": round(float(np.exp(c)), 3)}
        for nom, c in zip(noms, coefficients, strict=False)
    ]
    return sorted(lignes, key=lambda ligne: -abs(math.log(ligne["rapport_de_cotes"])))


def importance_globale(contributions: pd.DataFrame) -> list[dict]:
    moyenne = contributions.abs().mean().sort_values(ascending=False)
    return [{"variable": v, "shap_moyen_absolu": round(float(m), 4)} for v, m in moyenne.items()]


def effet_par_groupe(donnees: pd.DataFrame, contributions: pd.DataFrame) -> list[dict]:
    """Confronte le modele aux causes injectees par le generateur.

    Pour chaque cause documentee dans docs/HYPOTHESES.md : la contribution SHAP moyenne de la variable
    concernee dans le groupe expose et dans le groupe non expose, et le taux de resiliation observe. Un
    modele qui a appris la bonne chose donne une contribution plus forte au groupe expose, dans le sens
    annonce par l'hypothese.
    """
    groupes = [
        ("revalorisation de 6 % ou plus", "revalorisation_bp", donnees["revalorisation_bp"] >= 600),
        ("anciennete de moins de 18 mois", "anciennete_mois", donnees["anciennete_mois"] < 18),
        ("60 ans ou plus", "age", donnees["age"] >= 60),
        ("apporteurs AP04 et AP08", "code_apporteur", donnees["code_apporteur"].isin(["AP04", "AP08"])),
    ]
    resultat = []
    for libelle, variable, masque in groupes:
        resultat.append(
            {
                "cause": libelle,
                "variable": variable,
                "effectif_expose": int(masque.sum()),
                "shap_moyen_expose": round(float(contributions.loc[masque, variable].mean()), 4),
                "shap_moyen_non_expose": round(float(contributions.loc[~masque, variable].mean()), 4),
                "taux_observe_expose": round(float(donnees.loc[masque, CIBLE].mean()), 4),
                "taux_observe_non_expose": round(float(donnees.loc[~masque, CIBLE].mean()), 4),
            }
        )
    return resultat


def probabilites_du_generateur(donnees: pd.DataFrame) -> np.ndarray:
    """La probabilite que le generateur a reellement employee : le plafond qu'aucun modele ne peut depasser.

    Sur des donnees reelles, ce plafond est inconnu. Ici il est calculable, puisque la regle est ecrite
    dans `courtage.generateur`. Il dit ce que vaut une ROC-AUC de 0,65 : le tirage aleatoire de chaque
    resiliation laisse peu de place, et un modele proche du plafond a appris presque tout ce qui est
    apprenable. Les resiliations pour impaye passent par un autre chemin du generateur et restent hors
    de cette formule.
    """
    from courtage.generateur.parametres import INDICES_APPORTEURS_VOLATILS
    from courtage.generateur.portefeuille import _probabilite_resiliation

    # Les apporteurs sont generes dans l'ordre de leur code : l'indice i porte le code AP0{i+1}.
    indices = {f"AP{i + 1:02d}": i for i in INDICES_APPORTEURS_VOLATILS}
    return np.array(
        [
            _probabilite_resiliation(
                ligne.revalorisation_bp, ligne.anciennete_mois, ligne.age, indices.get(ligne.code_apporteur, -1)
            )
            for ligne in donnees.itertuples()
        ]
    )


def _phrase(variable: str, ligne: pd.Series) -> str:
    """Phrase affichee au gestionnaire : elle decrit la situation du contrat, pas le modele."""
    valeur = ligne[variable]
    if variable == "revalorisation_bp":
        return f"Revalorisation de {valeur / 100:.1f} % au dernier 1er janvier".replace(".", ",")
    if variable == "anciennete_mois":
        return f"Ancienneté de {valeur} mois"
    if variable == "age":
        return f"Adhérent de {valeur} ans"
    if variable == "code_apporteur":
        return f"Apporté par {valeur}"
    if variable == "code_produit":
        return f"Produit {valeur}"
    if variable == "code_branche":
        return f"Branche {str(valeur).replace('_', ' ')}"
    if variable == "cotisation_mensuelle_cents":
        return f"Cotisation de {valeur / 100:.2f} euros par mois".replace(".", ",")
    if variable == "nb_suspensions":
        return f"{valeur} suspension(s) pour impayé dans l'historique"
    if variable == "nb_changements_formule":
        return f"{valeur} changement(s) de formule"
    if variable == "est_suspendu":
        return "Contrat suspendu à la date du score"
    if variable == "est_une_affiliation":
        return "Affiliation à un contrat collectif" if valeur else "Contrat individuel"
    return f"{variable} : {valeur}"


def trois_facteurs(ligne: pd.Series, contribution: pd.Series) -> list[str]:
    """Les trois variables qui poussent le plus le risque a la hausse pour ce contrat.

    Seules les contributions positives sont retenues : afficher l'age comme facteur d'un score eleve,
    alors qu'il fait baisser ce risque chez cet adherent, induirait le gestionnaire en erreur.
    """
    positives = contribution[contribution > 0].sort_values(ascending=False).head(3)
    return [_phrase(variable, ligne) for variable in positives.index]
