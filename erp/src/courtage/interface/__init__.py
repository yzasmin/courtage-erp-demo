"""Interface de gestionnaire : gabarits Jinja2 et feuille de style, servis par l'application.

Direction retenue, et pourquoi. Le public de ces ecrans est un gestionnaire de courtage qui les ouvre
cinquante fois par jour. La priorite est donc la densite et la lisibilite, pas l'effet : tableaux
compacts, chiffres alignes a droite en police a largeur fixe, statut signale par une pastille, et le
contenu utile des la premiere hauteur d'ecran. Aucune page d'accroche, aucune animation.

Pas de chaine de compilation Node : les gabarits sont rendus cote serveur et HTMX suffit aux
interactions. Un seul processus a demarrer pour la demonstration, et rien a installer pour un relecteur.
"""

from __future__ import annotations

from pathlib import Path

DOSSIER_INTERFACE = Path(__file__).resolve().parent
DOSSIER_GABARITS = DOSSIER_INTERFACE / "templates"
DOSSIER_STATIQUES = DOSSIER_INTERFACE / "statiques"
