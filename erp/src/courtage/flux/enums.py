"""Enumerations propres a la chaine des flux financiers.

Elles sont ici, et non dans `modeles/enums.py`, parce qu'elles ne decrivent pas le portefeuille mais le
traitement des fichiers recus : un lot, son format, son etat d'avancement, la severite d'une anomalie et
la methode par laquelle un encaissement a ete rapproche.

La regle de partage du module 1 est conservee : ce que le code doit savoir distinguer est une
enumeration, ce que le metier doit pouvoir etendre est une table. Les codes d'anomalie sont une
enumeration, parce que chaque code correspond a une fonction de controle ecrite dans
`flux/controles.py` : un code inconnu du code n'aurait personne pour le produire.
"""

from __future__ import annotations

from enum import StrEnum


class TypeFlux(StrEnum):
    """Nature du fichier recu.

    Les trois flux de la specification, et ils n'ont ni le meme emetteur ni la meme cle metier.

    - `cotisations` : la compagnie envoie son bordereau de ce qui a ete quittance en son nom.
    - `encaissements` : la banque envoie le releve du compte du cabinet.
    - `prestations` : le **concentrateur de tiers payant** restitue au delegataire les remboursements
      regles dans son circuit. Ce n'est pas la compagnie qui l'envoie, et c'est important : une
      compagnie qui a delegue la gestion des prestations ne les gere plus, donc elle n'en a pas le
      detail. Le nom du fichier porte le code de la compagnie parce qu'il designe le perimetre des
      contrats concernes, pas l'expediteur.

    Le bordereau que le cabinet **emet** vers la compagnie pour se faire rembourser les prestations
    reglees pour son compte est un flux sortant, et il n'est pas modelise ici. Limite assumee,
    consignee dans `docs/DECISIONS.md`.
    """

    COTISATIONS = "cotisations"
    ENCAISSEMENTS = "encaissements"
    PRESTATIONS = "prestations"


class FormatFichier(StrEnum):
    CSV = "csv"
    XLSX = "xlsx"


class StatutLot(StrEnum):
    """Avancement d'un lot dans la chaine.

    Les quatre etats sont exactement les quatre taches d'ingestion du graphe Airflow, et c'est
    volontaire : l'etat du lot en base dit a quelle tache il s'est arrete, donc une reprise sur echec
    reprend le lot ou il en etait au lieu de recommencer depuis le depot.

    `rejete` est terminal. Un lot rejete n'est jamais rejoue automatiquement : la correction vient de
    l'emetteur, qui renvoie un fichier different, donc une autre empreinte, donc un autre lot.
    """

    DETECTE = "detecte"
    ARCHIVE = "archive"
    CHARGE = "charge"
    REJETE = "rejete"


class SeveriteAnomalie(StrEnum):
    """Severite d'une anomalie constatee sur un lot.

    `bloquant` rejette le lot entier, sans aucune ecriture partielle. `avertissement` est journalise et
    laisse passer le lot : un montant inhabituel n'est pas une erreur de fichier, c'est un point a
    verifier par un gestionnaire.
    """

    BLOQUANT = "bloquant"
    AVERTISSEMENT = "avertissement"


class CodeAnomalie(StrEnum):
    """Code de l'anomalie. Un code, un controle, une fonction dans `flux/controles.py`.

    Les huit premiers sont bloquants, les deux derniers non bloquants. L'ordre de declaration est
    celui du rapport d'anomalies.

    `acte_non_delegue` ne figure pas dans la liste de la specification, et il est pourtant le plus
    caracteristique du metier vise. Un courtier delegataire accomplit au nom de la compagnie des actes
    qui appartiennent normalement a l'assureur, dans le perimetre d'une convention datee. Quand la
    convention est echue, le cabinet doit cesser ces actes, meme si la compagnie continue d'envoyer ses
    bordereaux. Charger un bordereau de cotisations sur un produit dont le quittancement n'est plus
    delegue reviendrait a enregistrer un acte que le cabinet n'a pas le droit d'accomplir : le lot est
    donc rejete, et la compagnie informee.
    """

    SCHEMA_INVALIDE = "schema_invalide"
    FICHIER_DEJA_CHARGE = "fichier_deja_charge"
    DOUBLON_CLE_METIER = "doublon_cle_metier"
    CONTRAT_INCONNU = "contrat_inconnu"
    ADHERENT_INCONNU = "adherent_inconnu"
    TOTAL_INCOHERENT = "total_incoherent"
    PERIODE_INCOHERENTE = "periode_incoherente"
    ACTE_NON_DELEGUE = "acte_non_delegue"
    MONTANT_ATYPIQUE = "montant_atypique"
    PRESTATION_PROCHE_PLAFOND = "prestation_proche_plafond"


#: Severite de chaque code. Table unique, pour qu'un controle et le manifeste de verite terrain ne
#: puissent pas se contredire sur la severite d'un meme code.
SEVERITE_PAR_CODE: dict[CodeAnomalie, SeveriteAnomalie] = {
    CodeAnomalie.SCHEMA_INVALIDE: SeveriteAnomalie.BLOQUANT,
    CodeAnomalie.FICHIER_DEJA_CHARGE: SeveriteAnomalie.BLOQUANT,
    CodeAnomalie.DOUBLON_CLE_METIER: SeveriteAnomalie.BLOQUANT,
    CodeAnomalie.CONTRAT_INCONNU: SeveriteAnomalie.BLOQUANT,
    CodeAnomalie.ADHERENT_INCONNU: SeveriteAnomalie.BLOQUANT,
    CodeAnomalie.TOTAL_INCOHERENT: SeveriteAnomalie.BLOQUANT,
    CodeAnomalie.PERIODE_INCOHERENTE: SeveriteAnomalie.BLOQUANT,
    CodeAnomalie.ACTE_NON_DELEGUE: SeveriteAnomalie.BLOQUANT,
    CodeAnomalie.MONTANT_ATYPIQUE: SeveriteAnomalie.AVERTISSEMENT,
    CodeAnomalie.PRESTATION_PROCHE_PLAFOND: SeveriteAnomalie.AVERTISSEMENT,
}

#: Libelles lisibles, pour le rapport d'anomalies et l'ecran de l'ERP.
LIBELLE_ANOMALIE: dict[CodeAnomalie, str] = {
    CodeAnomalie.SCHEMA_INVALIDE: "schema ou type de donnee invalide",
    CodeAnomalie.FICHIER_DEJA_CHARGE: "fichier deja charge, meme empreinte SHA-256",
    CodeAnomalie.DOUBLON_CLE_METIER: "doublon de cle metier dans le fichier",
    CodeAnomalie.CONTRAT_INCONNU: "contrat inconnu du portefeuille",
    CodeAnomalie.ADHERENT_INCONNU: "adherent inconnu du portefeuille",
    CodeAnomalie.TOTAL_INCOHERENT: "somme des lignes differente de la ligne de total",
    CodeAnomalie.PERIODE_INCOHERENTE: "periode incoherente avec la date d'effet ou de resiliation",
    CodeAnomalie.ACTE_NON_DELEGUE: "acte non delegue au cabinet pour ce produit a cette date",
    CodeAnomalie.MONTANT_ATYPIQUE: "montant atypique au regard de l'historique du contrat",
    CodeAnomalie.PRESTATION_PROCHE_PLAFOND: "prestation proche du plafond annuel de la garantie",
}


class MethodeRapprochement(StrEnum):
    """Comment un encaissement a trouve son appel de cotisation.

    La specification impose l'ordre : d'abord la reference, ensuite le montant et la date dans une
    fenetre parametrable. Conserver la methode employee n'est pas cosmetique : un rapprochement par
    montant et date est une presomption, pas une certitude, et un gestionnaire qui releve un litige doit
    savoir lequel des deux il relit.
    """

    REFERENCE = "reference"
    MONTANT_DATE = "montant_date"
    AUCUNE = "aucune"
