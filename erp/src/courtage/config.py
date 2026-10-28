"""Parametres de l'application, lus dans l'environnement.

Un seul parametre compte vraiment : `COURTAGE_BASE_URL`, l'adresse de la base. Elle vaut SQLite par
defaut, pour qu'un clone vierge se teste sans rien installer, et PostgreSQL dans le compose. C'est le
seul endroit du projet qui sait quel moteur est utilise.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

RACINE_PROJET = Path(__file__).resolve().parents[2]


class Parametres(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="COURTAGE_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # SQLite par defaut : aucun service a demarrer pour developper et tester.
    base_url: str = f"sqlite+pysqlite:///{(RACINE_PROJET / 'courtage.db').as_posix()}"

    # Graine du generateur. Fixee pour que deux executions donnent le meme jeu de donnees.
    graine: int = 20261019

    # Date de fin de l'historique simule. Le generateur produit trois ans d'historique qui s'y terminent.
    # Fixee et non calculee a partir d'aujourd'hui, sinon la generation ne serait pas reproductible.
    date_reference: str = "2026-09-30"

    titre_application: str = "Courtage ERP"
    # Nom du cabinet simule, affiche dans l'interface. Cabinet fictif.
    nom_cabinet: str = "Cabinet Grossiste Occitanie"

    echo_sql: bool = False

    # Demonstration publique en ligne : affiche un bandeau qui previent que les donnees sont fictives et
    # remises a zero a chaque redemarrage, puisque l'application n'a pas d'authentification.
    mode_demonstration: bool = False


parametres = Parametres()
