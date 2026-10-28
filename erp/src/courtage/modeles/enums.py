"""Enumerations du domaine.

Une regle de partage, appliquee dans tout le schema, et qui merite d'etre enoncee parce qu'elle revient
souvent en revue de modele.

**Ce que le code doit savoir distinguer est une enumeration. Ce que le metier doit pouvoir etendre est une
table.** Le statut d'un contrat est une enumeration : le code teste `statut == EN_COURS` pour autoriser un
avenant, donc une valeur inconnue du code n'aurait aucun sens. La branche d'assurance est une table :
ajouter l'automobile ou les dommages doit etre une insertion, pas une livraison de code. Les garanties
sont une table pour la meme raison.

Les enumerations sont rendues en base comme un `VARCHAR`, et non comme le type natif `ENUM` de
PostgreSQL. C'est volontaire : la colonne se comporte a l'identique sur les deux moteurs, et ajouter une
valeur ne demande pas de `ALTER TYPE`, operation que l'on ne veut pas sur la base d'un client.

**Ce que la base ne verifie pas, et pourquoi.** Le domaine des valeurs n'est pas tenu par une contrainte
de verification, mais par la validation applicative de SQLAlchemy (`validate_strings`). La contrainte a
ete essayee : avec la convention de nommage du projet, l'autogeneration d'Alembic la rend deux fois,
sous son nom brut et sous son nom conventionne, ce que PostgreSQL refuse. Les contraintes de
verification **metier** du schema, elles, sont bien en base : elles sont ecrites a la main et listees
dans `docs/modele-donnees.md`.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Enum as EnumSQL


class StatutContrat(StrEnum):
    """Cycle de vie du contrat. Les transitions autorisees sont dans `domaine.cycle_vie`."""

    DEVIS = "devis"
    EN_COURS = "en_cours"
    SUSPENDU = "suspendu"
    RESILIE = "resilie"


class MotifResiliation(StrEnum):
    """Motif de la sortie du portefeuille. Obligatoire des que le contrat passe a l'etat resilie."""

    ECHEANCE = "echeance"
    INFRA_ANNUELLE = "infra_annuelle"
    NON_PAIEMENT = "non_paiement"
    CHANGEMENT_SITUATION = "changement_situation"
    DECES = "deces"
    PAR_COMPAGNIE = "par_compagnie"
    TRANSFERT_PORTEFEUILLE = "transfert_portefeuille"


class TypeAvenant(StrEnum):
    """Nature de l'evenement qui a produit une nouvelle version du contrat."""

    SOUSCRIPTION = "souscription"
    MISE_EN_VIGUEUR = "mise_en_vigueur"
    CHANGEMENT_FORMULE = "changement_formule"
    REVALORISATION_TARIFAIRE = "revalorisation_tarifaire"
    CHANGEMENT_APPORTEUR = "changement_apporteur"
    CHANGEMENT_FRACTIONNEMENT = "changement_fractionnement"
    SUSPENSION = "suspension"
    REMISE_EN_VIGUEUR = "remise_en_vigueur"
    RESILIATION = "resiliation"


class TypeApporteur(StrEnum):
    """Qualite du canal de distribution auquel une affaire est rattachee.

    `direct` remplace un ancien `salarie`, qui etait un contresens de vocabulaire. Un salarie du
    cabinet n'est pas un apporteur : il ne detient pas de mandat, il n'est pas immatricule a l'ORIAS,
    et il ne percoit pas de retrocession puisqu'il est paye par son salaire. Ce que la table decrit
    reellement sur ces lignes, c'est la **production directe** du cabinet, le canal par lequel une
    affaire arrive sans intermediaire. La valeur reste courte, `direct`, pour ne pas allonger la
    colonne et imposer une migration de type.
    """

    MANDATAIRE = "mandataire"
    SOUS_COURTIER = "sous_courtier"
    COMPARATEUR = "comparateur"
    DIRECT = "direct"


class Fractionnement(StrEnum):
    """Periodicite des appels de cotisation."""

    MENSUEL = "mensuel"
    TRIMESTRIEL = "trimestriel"
    SEMESTRIEL = "semestriel"
    ANNUEL = "annuel"


class StatutAppel(StrEnum):
    """Suite donnee a un appel de cotisation, du point de vue de l'encaissement."""

    ATTENDU = "attendu"
    ENCAISSE = "encaisse"
    PARTIEL = "partiel"
    IMPAYE = "impaye"


class StatutRapprochement(StrEnum):
    """Resultat du rapprochement d'un encaissement avec un appel de cotisation.

    Les quatre etats exiges par la specification du module 2, poses des le module 1 pour que l'ecran
    des anomalies ait une colonne a afficher.
    """

    NON_RAPPROCHE = "non_rapproche"
    RAPPROCHE = "rapproche"
    PARTIEL = "partiel"
    ECART = "ecart"
    NON_TROUVE = "non_trouve"


class MoyenPaiement(StrEnum):
    MANDAT_SEPA = "mandat_sepa"
    VIREMENT = "virement"
    CHEQUE = "cheque"


class StatutPrestation(StrEnum):
    EN_COURS = "en_cours"
    REGLEE = "reglee"
    REJETEE = "rejetee"


class AssietteCommission(StrEnum):
    """Base de calcul de la commission. Voir `docs/HYPOTHESES.md` pour le choix retenu et pourquoi."""

    COTISATION_APPELEE = "cotisation_appelee"
    COTISATION_ENCAISSEE = "cotisation_encaissee"


class TypeBordereau(StrEnum):
    COTISATIONS = "cotisations"
    PRESTATIONS = "prestations"
    COMMISSIONS = "commissions"
    RETROCESSIONS = "retrocessions"


class StatutBordereau(StrEnum):
    EN_PREPARATION = "en_preparation"
    EMIS = "emis"
    REGLE = "regle"


class ActeGestion(StrEnum):
    """Actes de gestion qu'une compagnie peut deleguer au cabinet.

    Un courtier delegataire accomplit, au nom de la compagnie, des actes qui appartiennent normalement a
    l'assureur. Le perimetre est fixe par convention, produit par produit. Chaque valeur ci-dessous
    correspond a un point d'entree du code qui verifie la delegation avant d'agir.
    """

    AFFILIATION = "affiliation"
    AVENANT = "avenant"
    RADIATION = "radiation"
    QUITTANCEMENT = "quittancement"
    ENCAISSEMENT = "encaissement"
    GESTION_PRESTATIONS = "gestion_prestations"
    GESTION_SINISTRES = "gestion_sinistres"


class PorteeDelegation(StrEnum):
    """Libelle declare de la convention : gestion totale ou partielle.

    Ce libelle est informatif. Ce qui est oppose a un acte, c'est la liste des actes reellement delegues,
    jamais ce libelle, afin qu'une convention mal saisie ne puisse pas elargir un perimetre.
    """

    TOTALE = "totale"
    PARTIELLE = "partielle"


def colonne_enum(enumeration: type[StrEnum], nom: str) -> EnumSQL:
    """Rend une enumeration Python sous forme de `VARCHAR` contraint, portable sur les deux moteurs.

    `native_enum=False` evite le type ENUM natif de PostgreSQL, dont l'evolution demande un `ALTER TYPE`
    et qui n'a pas d'equivalent sur SQLite. `values_callable` stocke la valeur metier ("en_cours") et non
    le nom Python ("EN_COURS"), ce qui rend les donnees lisibles telles quelles dans une requete SQL.
    """
    return EnumSQL(
        enumeration,
        name=nom,
        native_enum=False,
        # `create_constraint` reste a sa valeur par defaut, c'est-a-dire desactive. Essai fait, et
        # resultat consigne : avec la convention de nommage du projet, l'autogeneration d'Alembic rend
        # la contrainte de l'enumeration **deux fois**, sous son nom brut et sous son nom conventionne.
        # SQLite l'accepte, PostgreSQL la refuse (« check constraint already exists »), et la migration
        # devient non portable. Le domaine des valeurs est donc tenu par la validation applicative de
        # SQLAlchemy, et non par la base. Voir `docs/modele-donnees.md`, section « Ce que la base ne
        # verifie pas ».
        validate_strings=True,
        values_callable=lambda e: [membre.value for membre in e],
    )
