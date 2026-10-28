"""Reglages de la chaine des flux : dossiers, formats de fichier, fenetres de tolerance, taux d'anomalies.

Toutes les valeurs numeriques de ce fichier sont des **hypotheses de simulation** ou des **parametres
d'exploitation**, jamais des regles legales. Elles sont consignees dans `docs/HYPOTHESES.md`.

Distinction a garder en tete : les taux de taxe, de commission et de retrocession ne sont pas ici. Ils
vivent en base avec leurs dates de validite, comme au module 1, et le calcul va les y chercher a la date
de l'echeance. Ce fichier ne contient que les reglages du traitement.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from courtage.config import RACINE_PROJET
from courtage.flux.enums import CodeAnomalie

# --- Dossiers de la chaine ----------------------------------------------------------------------


def racine_flux() -> Path:
    """Racine de l'espace de travail des flux : dossier d'arrivee, archive, rejets.

    Lue dans l'environnement a chaque appel, et non figee a l'import, pour que le conteneur Airflow
    puisse la pointer sur un volume partage et que les tests puissent la pointer sur un dossier
    temporaire sans recharger le module.
    """
    valeur = os.environ.get("COURTAGE_FLUX_RACINE")
    return Path(valeur) if valeur else RACINE_PROJET / "data" / "flux"


#: Dossier d'arrivee, ou les emetteurs deposent leurs fichiers.
NOM_DOSSIER_ARRIVEE = "arrivee"
#: Archive du brut, organisee par periode. Un fichier archive n'est plus jamais modifie.
NOM_DOSSIER_ARCHIVE = "archive"
#: Fichiers dont le lot a ete rejete. Ils sont conserves : c'est la piece renvoyee a l'emetteur.
NOM_DOSSIER_REJETS = "rejets"
#: Manifeste de verite terrain produit par le generateur, lu par le rapport de detection.
NOM_MANIFESTE = "manifeste-anomalies.json"

# --- Format des fichiers recus ------------------------------------------------------------------

#: Separateur de colonnes. Le point-virgule est l'usage des exports francais, parce que la virgule est
#: deja prise par les decimales.
SEPARATEUR_CSV = ";"
#: Encodage retenu pour les CSV produits et lus. Choix assume : l'UTF-8 avec marque d'ordre des octets
#: est ce qu'un tableur francais produit et relit sans abimer les accents.
ENCODAGE_CSV = "utf-8-sig"
#: Format de date des fichiers. Format francais, celui des bordereaux papier.
FORMAT_DATE_FICHIER = "%d/%m/%Y"
#: Libelle de la premiere cellule de la ligne de total.
MARQUEUR_TOTAL = "TOTAL"

# --- Periodes couvertes par les fichiers simules ------------------------------------------------

#: Mois couverts par les fichiers recus, du plus ancien au plus recent. Trois mois suffisent a montrer
#: un bordereau de commissions mensuel, une reprise sur rejet et l'idempotence, sans produire un volume
#: qui rend les preuves illisibles.
PERIODES_SIMULEES: tuple[date, ...] = (date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1))

# --- Taux d'anomalies injectees -----------------------------------------------------------------

#: Part de lignes porteuses d'une anomalie, toutes severites confondues. La specification impose
#: 3 a 5 pour cent ; la valeur visee est au milieu de la fourchette.
PART_LIGNES_ANORMALES = 0.04

#: Repartition des anomalies de ligne non bloquantes, en poids relatifs. Les anomalies bloquantes ne
#: sont pas tirees au hasard : elles sont posees lot par lot, voir `LOTS_A_ANOMALIE_BLOQUANTE`.
POIDS_ANOMALIES_LIGNE = {
    CodeAnomalie.MONTANT_ATYPIQUE: 60,
    CodeAnomalie.PRESTATION_PROCHE_PLAFOND: 40,
}

#: Facteurs appliques au montant pour fabriquer un montant atypique. Un facteur proche de 1 ne serait
#: pas detectable, et ne serait pas une anomalie : il serait dans la variation normale du portefeuille.
FACTEURS_MONTANT_ATYPIQUE = (0.32, 0.38, 2.6, 3.1)

#: Part du plafond annuel que vise une prestation « proche du plafond ».
PART_PLAFOND_VISEE = 0.94

#: Combien de lots recoivent une anomalie bloquante, et laquelle. La cle est le rang du lot dans l'ordre
#: de generation ; la valeur est le code injecte. Pose a la main, et non tire au hasard, pour deux
#: raisons : chaque controle bloquant doit etre exerce au moins une fois, et une anomalie bloquante
#: rejette tout le lot, donc en semer au hasard reviendrait a rejeter la moitie des donnees.
#: La valeur est le couple code injecte et variante, la variante precisant comment l'anomalie est
#: fabriquee quand un meme code peut l'etre de plusieurs facons.
#:
#: L'ordre des lots est celui de la generation : par periode, les bordereaux de cotisations par
#: compagnie, puis le releve bancaire, puis les bordereaux de prestations. Trois periodes de six lots
#: donnent dix-huit rangs, et les rangs ci-dessous repartissent les huit controles bloquants sur les
#: trois flux, le refus d'acte non delegue compris.
LOTS_A_ANOMALIE_BLOQUANTE: dict[int, tuple[CodeAnomalie, str]] = {
    2: (CodeAnomalie.CONTRAT_INCONNU, "numero_contrat_inexistant"),
    7: (CodeAnomalie.ACTE_NON_DELEGUE, "convention_echue"),
    5: (CodeAnomalie.TOTAL_INCOHERENT, "total_declare_fausse"),
    8: (CodeAnomalie.DOUBLON_CLE_METIER, "cle_dupliquee"),
    11: (CodeAnomalie.PERIODE_INCOHERENTE, "avant_date_effet"),
    14: (CodeAnomalie.SCHEMA_INVALIDE, "date_impossible"),
    16: (CodeAnomalie.SCHEMA_INVALIDE, "montant_non_numerique"),
    17: (CodeAnomalie.ADHERENT_INCONNU, "numero_adherent_inexistant"),
}

#: Ecart ajoute a la ligne de total quand l'anomalie « total incoherent » est injectee, en centimes.
ECART_TOTAL_INJECTE_CENTS = 1_250

#: Suffixe du fichier corrige que l'emetteur renvoie apres le rejet d'un lot. La chaine doit montrer la
#: boucle complete : rejet du lot entier, renvoi corrige, chargement. Le suffixe commence par un tiret
#: bas pour que le fichier corrige se trie apres l'original, l'ordre de traitement etant l'ordre
#: alphabetique des noms.
SUFFIXE_CORRECTION = "_R2"

#: Rang du lot dont le fichier est redeposse sous un autre nom, pour exercer le controle d'empreinte.
#: Le lot d'origine doit etre un lot sain, sinon le doublon serait rejete pour deux raisons a la fois et
#: le rapport de detection deviendrait ambigu.
LOT_REDEPOSE_SOUS_AUTRE_NOM = 1

#: Suffixe du nom du fichier redeposse. Imite ce qu'un emetteur fait reellement en renvoyant son
#: bordereau renomme. Commence par un tiret bas pour se trier apres l'original.
SUFFIXE_REDEPOT = "_copie"

# --- Releve bancaire simule ---------------------------------------------------------------------

#: Part des mouvements du releve dont la reference d'appel est absente. Ce n'est pas une anomalie :
#: c'est le payeur qui a fait un virement a la main sans reprendre la reference de sa quittance, et
#: c'est exactement le cas que le rapprochement par montant et date doit traiter. Sans ces lignes, la
#: deuxieme methode de rapprochement ne serait jamais exercee et la chaine ne prouverait rien.
PART_RELEVE_SANS_REFERENCE = 0.12

#: Nombre de reglements d'un montant different de l'appel, ajoutes a chaque releve. Ils produisent le
#: statut « ecart », que l'ecran des anomalies de l'ERP doit afficher.
NB_ECARTS_RELEVE = 8

#: Supplement verse par un payeur qui regle trop, en centimes.
SURPLUS_ECART_CENTS = 1_500

#: Part de l'appel versee par un payeur qui regle beaucoup trop peu. En dessous de la part minimale du
#: reglement partiel, ce n'est plus un acompte : c'est un ecart a instruire.
PART_REGLEMENT_INSUFFISANT = 0.18

#: Nombre de mouvements bancaires inattribuables ajoutes a chaque releve : ni reference, ni contrat,
#: ni montant correspondant. Ils produisent le statut « non trouve ».
NB_MOUVEMENTS_NON_IDENTIFIES = 5

# --- Controles ----------------------------------------------------------------------------------

#: Tolerance sur la ligne de total, en centimes. Zero : un bordereau qui ne tombe pas au centime pres
#: n'est pas rapprochable, et l'accepter a un euro pres reviendrait a accepter un euro d'ecart par
#: bordereau, soit quelques milliers d'euros par an sur un portefeuille de cette taille.
TOLERANCE_TOTAL_CENTS = 0

#: Seuil de declenchement du controle « montant atypique » : ecart relatif au tarif mensuel median du
#: contrat. 60 pour cent laisse passer les revalorisations annuelles et les periodes tronquees, et
#: retient les erreurs de saisie d'un facteur deux ou plus.
SEUIL_MONTANT_ATYPIQUE = 0.60

#: Nombre minimal d'appels connus sur un contrat avant de pouvoir parler d'historique. En dessous, le
#: controle se tait : signaler un montant « atypique » sur un contrat qui n'a qu'une echeance connue
#: serait une invention.
MINIMUM_APPELS_POUR_HISTORIQUE = 3

#: Part du plafond annuel au-dela de laquelle une prestation est signalee.
SEUIL_PROCHE_PLAFOND = 0.85

# --- Rapprochement ------------------------------------------------------------------------------

#: Fenetre de tolerance du rapprochement par montant et date, en jours autour de la date d'echeance.
#: Parametrable, comme la specification l'exige, et modifiable par l'option `--fenetre-jours`.
FENETRE_RAPPROCHEMENT_JOURS = 15

#: Tolerance d'ecart de montant pour declarer un rapprochement exact, en centimes. Zero, pour la meme
#: raison que la ligne de total.
TOLERANCE_RAPPROCHEMENT_CENTS = 0

#: En dessous de cette part de l'appel, un encaissement n'est pas un reglement partiel du meme appel
#: mais une autre operation. Au-dessus, et en dessous du total, le statut est « partiel ».
PART_MINIMALE_PARTIEL = 0.30
