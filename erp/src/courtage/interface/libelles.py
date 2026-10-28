"""Libellés français des valeurs d'énumération, pour l'interface et les listes déroulantes.

Pourquoi ce module existe. Les écrans affichaient les valeurs techniques telles qu'elles sont stockées :
`en_cours`, `mandat_sepa`, `changement_situation`, `infra_annuelle`. Ce sont des identifiants de base de
données, pas du français, et un gestionnaire qui lit « mandat_sepa » dans une colonne comprend surtout
qu'on ne lui a pas destiné cet écran.

La valeur technique reste celle de la base, de l'API et des exports : elle est stable, elle ne bouge pas
quand la traduction change. Seul l'affichage passe par ici.

Un mot sur « mandat SEPA », qui était faux. Un mandat est l'autorisation signée par l'adhérent ; ce qui
figure sur un relevé bancaire est le **prélèvement** opéré en vertu de ce mandat. Un gestionnaire ne dit
pas « encaissé par mandat », il dit « prélevé ».
"""

from __future__ import annotations

from courtage.domaine.delegation import LIBELLE_ACTE
from courtage.flux.enums import (
    MethodeRapprochement,
    SeveriteAnomalie,
    StatutLot,
    TypeFlux,
)
from courtage.modeles.enums import (
    Fractionnement,
    MotifResiliation,
    MoyenPaiement,
    PorteeDelegation,
    StatutAppel,
    StatutContrat,
    StatutPrestation,
    StatutRapprochement,
    TypeApporteur,
    TypeAvenant,
)

STATUT_CONTRAT: dict[str, str] = {
    StatutContrat.DEVIS: "devis",
    StatutContrat.EN_COURS: "en cours",
    StatutContrat.SUSPENDU: "suspendu",
    StatutContrat.RESILIE: "résilié",
}

MOTIF_RESILIATION: dict[str, str] = {
    MotifResiliation.ECHEANCE: "résiliation à l'échéance",
    MotifResiliation.INFRA_ANNUELLE: "résiliation infra-annuelle",
    MotifResiliation.NON_PAIEMENT: "non-paiement, après mise en demeure",
    MotifResiliation.CHANGEMENT_SITUATION: "changement de situation",
    MotifResiliation.DECES: "décès",
    MotifResiliation.PAR_COMPAGNIE: "résiliation par la compagnie",
    MotifResiliation.TRANSFERT_PORTEFEUILLE: "transfert de portefeuille",
}

TYPE_AVENANT: dict[str, str] = {
    TypeAvenant.SOUSCRIPTION: "souscription",
    TypeAvenant.MISE_EN_VIGUEUR: "mise en vigueur",
    TypeAvenant.CHANGEMENT_FORMULE: "changement de formule",
    TypeAvenant.REVALORISATION_TARIFAIRE: "revalorisation tarifaire",
    TypeAvenant.CHANGEMENT_APPORTEUR: "changement d'apporteur",
    TypeAvenant.CHANGEMENT_FRACTIONNEMENT: "changement de fractionnement",
    TypeAvenant.SUSPENSION: "suspension des garanties",
    TypeAvenant.REMISE_EN_VIGUEUR: "remise en vigueur",
    TypeAvenant.RESILIATION: "résiliation",
}

TYPE_APPORTEUR: dict[str, str] = {
    TypeApporteur.MANDATAIRE: "mandataire",
    TypeApporteur.SOUS_COURTIER: "sous-courtier",
    TypeApporteur.COMPARATEUR: "comparateur",
    TypeApporteur.DIRECT: "production directe du cabinet",
}

FRACTIONNEMENT: dict[str, str] = {
    Fractionnement.MENSUEL: "mensuel",
    Fractionnement.TRIMESTRIEL: "trimestriel",
    Fractionnement.SEMESTRIEL: "semestriel",
    Fractionnement.ANNUEL: "annuel",
}

STATUT_APPEL: dict[str, str] = {
    StatutAppel.ATTENDU: "attendu",
    StatutAppel.ENCAISSE: "encaissé",
    StatutAppel.PARTIEL: "réglé partiellement",
    StatutAppel.IMPAYE: "impayé",
}

STATUT_RAPPROCHEMENT: dict[str, str] = {
    StatutRapprochement.NON_RAPPROCHE: "non rapproché",
    StatutRapprochement.RAPPROCHE: "rapproché",
    StatutRapprochement.PARTIEL: "partiel",
    StatutRapprochement.ECART: "écart",
    StatutRapprochement.NON_TROUVE: "non trouvé",
}

MOYEN_PAIEMENT: dict[str, str] = {
    # « Prélèvement » et non « mandat » : le mandat est l'autorisation, le prélèvement est l'opération.
    MoyenPaiement.MANDAT_SEPA: "prélèvement SEPA",
    MoyenPaiement.VIREMENT: "virement",
    MoyenPaiement.CHEQUE: "chèque",
}

STATUT_PRESTATION: dict[str, str] = {
    StatutPrestation.EN_COURS: "en cours d'instruction",
    StatutPrestation.REGLEE: "réglée",
    StatutPrestation.REJETEE: "rejetée",
}

PORTEE_DELEGATION: dict[str, str] = {
    PorteeDelegation.TOTALE: "gestion totale",
    PorteeDelegation.PARTIELLE: "gestion partielle",
}

#: Reexport de la table du domaine, et non une seconde table. Les deux ont coexiste, l'une accentuee
#: et l'autre non, et l'ecran d'accueil affichait la version sans accents parce qu'il lisait celle du
#: domaine. Une seule table, un seul endroit a corriger.
ACTE_GESTION: dict[str, str] = dict(LIBELLE_ACTE)

TYPE_FLUX: dict[str, str] = {
    TypeFlux.COTISATIONS: "bordereau de cotisations",
    TypeFlux.ENCAISSEMENTS: "relevé bancaire",
    TypeFlux.PRESTATIONS: "bordereau de prestations",
}

METHODE_RAPPROCHEMENT: dict[str, str] = {
    MethodeRapprochement.REFERENCE: "par référence",
    MethodeRapprochement.MONTANT_DATE: "par montant et date",
    MethodeRapprochement.AUCUNE: "aucune correspondance",
}

STATUT_LOT: dict[str, str] = {
    StatutLot.DETECTE: "détecté",
    StatutLot.ARCHIVE: "archivé",
    StatutLot.CHARGE: "chargé",
    StatutLot.REJETE: "rejeté",
}

SEVERITE_ANOMALIE: dict[str, str] = {
    SeveriteAnomalie.BLOQUANT: "bloquante",
    SeveriteAnomalie.AVERTISSEMENT: "avertissement",
}

#: Tous les dictionnaires, pour que le filtre de gabarit trouve le libellé sans savoir lequel utiliser.
_TOUS: tuple[dict[str, str], ...] = (
    STATUT_CONTRAT,
    MOTIF_RESILIATION,
    TYPE_AVENANT,
    TYPE_APPORTEUR,
    FRACTIONNEMENT,
    STATUT_APPEL,
    STATUT_RAPPROCHEMENT,
    MOYEN_PAIEMENT,
    STATUT_PRESTATION,
    PORTEE_DELEGATION,
    ACTE_GESTION,
    TYPE_FLUX,
    METHODE_RAPPROCHEMENT,
    STATUT_LOT,
    SEVERITE_ANOMALIE,
)


def libelle(valeur: object) -> str:
    """Libellé français d'une valeur d'énumération, ou la valeur elle-même si elle n'en a pas.

    Rendre la valeur brute plutôt que de lever est volontaire : un écran doit continuer à s'afficher
    quand une valeur nouvelle n'a pas encore sa traduction, et le code technique reste alors lisible.
    """
    if valeur is None:
        return ""
    cle = getattr(valeur, "value", valeur)
    for table in _TOUS:
        trouve = table.get(cle)
        if trouve is not None:
            return trouve
    return str(cle)
