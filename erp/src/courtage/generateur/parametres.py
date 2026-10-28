"""Parametres de la simulation. Toutes ces valeurs sont des hypotheses, pas des baremes reels.

Elles sont rassemblees ici, et nulle part ailleurs, pour deux raisons. D'abord parce que
`docs/HYPOTHESES.md` doit pouvoir etre relu en regard d'un seul fichier de code. Ensuite parce qu'un
lecteur du metier qui trouve une valeur invraisemblable doit pouvoir la corriger a un seul endroit.

A ne pas confondre avec les parametres **du produit**, qui eux vivent en base avec des dates de
validite : taux de taxe, taux de commission, taux de retrocession, tarifs de formule et plafonds de
garantie. Ce fichier ne contient que les reglages du tirage aleatoire qui remplit cette base.
"""

from __future__ import annotations

from datetime import date

#: Debut de la fenetre de donnees : les appels de cotisation, encaissements et prestations ne sont
#: generes qu'a partir de cette date. Elle donne les trois ans d'historique de flux demandes.
DEBUT_HISTORIQUE = date(2023, 10, 1)

#: Debut du portefeuille. Les contrats, eux, peuvent etre bien plus anciens que la fenetre de donnees.
#:
#: Sans cette distinction, le cabinet simule naissait le jour ou commence l'historique : 496 contrats
#: en vigueur au 1er janvier 2024 contre 3 986 deux ans plus tard. Le taux de resiliation de la
#: premiere annee, qui rapporte les sorties au portefeuille de debut de periode, devenait alors
#: illisible, et le portefeuille n'avait aucune anciennete. Un cabinet reel a un portefeuille plus
#: vieux que les donnees qu'il conserve en ligne, et c'est ce que reproduit cette date.
DEBUT_PORTEFEUILLE = date(2019, 1, 1)

#: Volumetrie visee, telle que la specification la fixe.
NB_ENTREPRISES = 40
NB_ADHERENTS_COLLECTIFS_MIN = 24
NB_ADHERENTS_COLLECTIFS_MAX = 95
NB_ADHERENTS_INDIVIDUELS = 3_000
NB_APPORTEURS = 10

#: Part des adherents qui souscrivent un second contrat, en general une prevoyance a cote d'une sante.
PART_SECOND_CONTRAT = 0.12

#: Part des contrats qui restent a l'etat de devis, sans jamais prendre effet. Un cabinet qui
#: transformerait tous ses devis n'existe pas, et le module de pilotage a besoin de ce denominateur.
PART_DEVIS_SANS_SUITE = 0.06

#: Repartition du fractionnement des cotisations. Les poids sont relatifs.
POIDS_FRACTIONNEMENT = {"mensuel": 62, "trimestriel": 24, "semestriel": 9, "annuel": 5}

#: Taxes sur les cotisations, en points de base, par code de branche.
#: Hypotheses de simulation. Aucun bareme legal n'est reproduit, voir `docs/HYPOTHESES.md`.
TAUX_TAXE_BP = {"SI": 1327, "SC": 1327, "PV": 900}

#: Bornes des taux de commission verses par la compagnie au cabinet, en points de base, par branche.
BORNES_COMMISSION_BP = {"SI": (1200, 1800), "SC": (800, 1200), "PV": (1500, 2200)}

#: Bornes de la part de commission reversee a l'apporteur, en points de base, par type d'apporteur.
#: La production directe ne donne lieu a aucune retrocession : l'affaire arrive sans intermediaire.
BORNES_RETROCESSION_BP = {
    "mandataire": (5000, 7000),
    "sous_courtier": (6000, 8000),
    "comparateur": (3000, 4500),
    "direct": (0, 0),
}

#: Revalorisation tarifaire appliquee au 1er janvier, en points de base. Une borne par annee, pour que
#: les echeances ne se ressemblent pas. La serie couvre toute la profondeur du portefeuille, et non
#: seulement la fenetre de donnees : un contrat de 2019 a bien ete revalorise chaque annee depuis.
REVALORISATION_BP_PAR_ANNEE = {
    2020: (150, 500),
    2021: (200, 600),
    2022: (250, 750),
    2023: (300, 850),
    2024: (200, 700),
    2025: (300, 900),
    2026: (250, 800),
}

#: Au-dela de ce seuil de revalorisation, la probabilite de resiliation a l'echeance suivante monte
#: nettement. C'est le signal principal que le module 5 doit retrouver.
SEUIL_REVALORISATION_SENSIBLE_BP = 600

#: Probabilite de resiliation annuelle de base, avant application des facteurs.
PROBA_RESILIATION_BASE = 0.055

#: Facteurs multiplicatifs de la probabilite de resiliation. Hypotheses de simulation, documentees pour
#: que le module 5 puisse etre juge sur sa capacite a les retrouver, et non sur une performance absolue.
FACTEUR_REVALORISATION_SENSIBLE = 2.4
FACTEUR_FAIBLE_ANCIENNETE = 1.8
FACTEUR_SINISTRALITE_FAIBLE = 1.6
FACTEUR_SINISTRALITE_FORTE = 0.55
FACTEUR_AGE_ELEVE = 0.6
AGE_ELEVE = 60
ANCIENNETE_FAIBLE_MOIS = 18

#: Deux apporteurs sur dix portent un portefeuille plus volatil. Leurs indices dans la liste generee.
INDICES_APPORTEURS_VOLATILS = (3, 7)
FACTEUR_APPORTEUR_VOLATIL = 1.7

#: Repartition des motifs de resiliation. Les poids sont relatifs.
POIDS_MOTIF_RESILIATION = {
    "echeance": 38,
    "infra_annuelle": 22,
    "non_paiement": 14,
    "changement_situation": 16,
    "deces": 3,
    "par_compagnie": 2,
    "transfert_portefeuille": 5,
}

#: Part des contrats en cours qui passent par une suspension de garanties pour non-paiement.
PART_SUSPENSION = 0.035

#: Anciennete minimale, en mois, avant qu'une resiliation infra-annuelle soit possible.
#: Le motif « infra-annuelle » designe la faculte de resilier a tout moment **apres** la premiere
#: annee de contrat. L'employer avant douze mois d'anciennete est une contradiction dans les termes,
#: et un gestionnaire le reperera immediatement. Avant ce seuil, une sortie prend un autre motif.
ANCIENNETE_MINIMALE_INFRA_ANNUELLE_MOIS = 12

#: Part des contrats qui changent de formule au moins une fois, hors revalorisation.
PART_CHANGEMENT_FORMULE = 0.09

#: Part des contrats qui changent d'apporteur. Arrive quand un mandataire quitte le reseau et que son
#: portefeuille est repris.
PART_CHANGEMENT_APPORTEUR = 0.04

#: Saisonnalite de la consommation de soins, poids relatifs par mois de janvier a decembre.
#: Creux en juillet et aout, pic de novembre a janvier.
POIDS_SAISON_PRESTATIONS = (118, 105, 104, 96, 94, 88, 62, 58, 97, 108, 122, 128)

#: Nombre de prestations par adherent et par an, selon la tranche d'age.
#: Bornes (minimum, maximum) par age limite superieur.
PRESTATIONS_PAR_AN_SELON_AGE = ((25, (0, 4)), (40, (1, 6)), (55, (2, 9)), (70, (3, 12)), (200, (4, 14)))

#: Echelle de sinistralite par branche : rapport du remboursement **theorique** d'un contrat (taux de la
#: formule, avant plafond) a la cotisation hors taxe que ce contrat a courue. Le montant des demandes de
#: chaque contrat est calibre sur sa cotisation : sans ce lien, les prestations etaient tirees par tranche
#: d'age et par garantie, sans rapport avec ce que le contrat paie, et le ratio sinistres sur primes
#: depassait 150 pour cent. Cette echelle n'est pas le ratio publie : les contrats sans aucun soin, les
#: plafonds annuels, les rejets et les prestations encore en cours en retranchent une part. Les valeurs
#: sont donc **mesurees**, branche par branche, pour que le ratio publie (`docs/kpi.md`) tombe dans la
#: fourchette d'un portefeuille sante viable. Une valeur par branche, car les formules n'ont ni les memes
#: taux de remboursement ni les memes plafonds.
ECHELLE_SINISTRALITE = {"SI": 1.42, "SC": 1.62}

#: Profils de consommation d'un contrat : (poids, borne basse, borne haute) du multiplicateur de la
#: echelle de sinistralite. Beaucoup de contrats consomment peu, une minorite consomme beaucoup : un ratio uniforme
#: serait aussi faux qu'un ratio demesure. Les multiplicateurs sont ensuite divises par leur moyenne
#: theorique, pour que la sinistralite d'ensemble reste celle de `ECHELLE_SINISTRALITE`.
PROFILS_CONSOMMATION = ((55, 0.05, 0.45), (35, 0.8, 1.6), (10, 2.5, 5.0))

#: Part des prestations rejetees, par exemple hors garantie ou hors delai de declaration.
PART_PRESTATION_REJETEE = 0.045

#: Delai entre la date des soins et le reglement, en jours.
DELAI_REGLEMENT_JOURS = (2, 30)

#: Part des appels de cotisation qui restent impayes, et part de ceux regles partiellement.
PART_APPEL_IMPAYE = 0.022
PART_APPEL_PARTIEL = 0.013

#: Delai d'encaissement apres l'echeance, en jours. Un mandat de prelevement passe vite, un virement
#: ou un cheque prend plus de temps.
DELAI_ENCAISSEMENT_JOURS = {"mandat_sepa": (0, 3), "virement": (1, 12), "cheque": (3, 21)}

#: Repartition des moyens de paiement. Poids relatifs.
POIDS_MOYEN_PAIEMENT = {"mandat_sepa": 78, "virement": 17, "cheque": 5}

#: Preavis de resiliation a l'echeance, en mois. Parametre de simulation, et non une regle legale
#: affirmee.
PREAVIS_ECHEANCE_MOIS = 2

#: Departements du bassin d'activite du cabinet simule, avec leur poids. Le cabinet est installe en
#: Occitanie, ce qui donne une repartition geographique credible plutot qu'uniforme.
POIDS_DEPARTEMENTS = {"34": 34, "30": 14, "11": 10, "66": 11, "81": 7, "31": 16, "12": 4, "48": 4}

#: Bornes de cotisation mensuelle, en centimes, par branche. Le tarif effectif part du tarif de la
#: formule, puis recoit une majoration d'age.
BORNES_COTISATION_CENTS = {"SI": (3_500, 18_000), "SC": (2_800, 9_500), "PV": (1_200, 7_000)}

#: Majoration de cotisation par tranche de cinq ans au-dela de 30 ans, en points de base.
MAJORATION_AGE_PAR_TRANCHE_BP = 420
AGE_PIVOT_MAJORATION = 30

#: Taille des lots d'insertion. Compromis entre le nombre d'allers-retours vers la base et la memoire
#: consommee par le lot en attente. Calibre pour un poste dont la memoire libre est faible.
TAILLE_LOT_INSERTION = 4_000
