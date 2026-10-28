"""Cycle de vie du contrat : les transitions autorisees, et pourquoi les autres sont refusees.

```
   devis ──mise en vigueur──> en cours ──résiliation──> résilié
                                 │  ▲                      ▲
                      suspension │  │ remise en vigueur    │
                                 ▼  │                      │
                              suspendu ───résiliation──────┘
```

Deux refus demandent une justification, parce qu'un relecteur du metier va les chercher.

**Un devis ne se resilie pas.** Un devis qui ne se transforme pas n'a jamais pris effet : il n'y a rien a
resilier, aucun preavis a respecter, aucune date de fin de garantie a notifier. Il reste au statut devis.
La distinction n'est pas cosmetique : elle protege le taux de resiliation, dont le denominateur est le
portefeuille en vigueur. Compter les devis sans suite parmi les resiliations ferait apparaitre un cabinet
qui perd ses clients, alors qu'il transforme seulement mal ses devis. Ce sont deux problemes differents,
avec deux remedes differents.

**Un contrat resilie ne revient pas.** La reprise d'un ancien adherent est une nouvelle souscription, avec
un nouveau numero de contrat et une nouvelle date d'effet. Rouvrir l'ancien contrat ferait disparaitre la
rupture de garantie entre les deux periodes, qui est precisement ce qu'une compagnie veut voir.
"""

from __future__ import annotations

from courtage.modeles.enums import StatutContrat, TypeAvenant

#: Transitions autorisees : statut de depart vers les statuts d'arrivee possibles.
TRANSITIONS_AUTORISEES: dict[StatutContrat, frozenset[StatutContrat]] = {
    StatutContrat.DEVIS: frozenset({StatutContrat.EN_COURS}),
    StatutContrat.EN_COURS: frozenset({StatutContrat.SUSPENDU, StatutContrat.RESILIE}),
    StatutContrat.SUSPENDU: frozenset({StatutContrat.EN_COURS, StatutContrat.RESILIE}),
    StatutContrat.RESILIE: frozenset(),
}

#: Type d'avenant attendu pour chaque changement de statut. Sert a refuser un avenant dont le type ne
#: correspond pas a ce qu'il fait reellement, par exemple une « revalorisation tarifaire » qui resilierait.
TYPE_ATTENDU_PAR_TRANSITION: dict[tuple[StatutContrat, StatutContrat], TypeAvenant] = {
    (StatutContrat.DEVIS, StatutContrat.EN_COURS): TypeAvenant.MISE_EN_VIGUEUR,
    (StatutContrat.EN_COURS, StatutContrat.SUSPENDU): TypeAvenant.SUSPENSION,
    (StatutContrat.SUSPENDU, StatutContrat.EN_COURS): TypeAvenant.REMISE_EN_VIGUEUR,
    (StatutContrat.EN_COURS, StatutContrat.RESILIE): TypeAvenant.RESILIATION,
    (StatutContrat.SUSPENDU, StatutContrat.RESILIE): TypeAvenant.RESILIATION,
}

#: Avenants qui modifient le contrat sans toucher a son statut.
TYPES_SANS_CHANGEMENT_STATUT: frozenset[TypeAvenant] = frozenset(
    {
        TypeAvenant.CHANGEMENT_FORMULE,
        TypeAvenant.REVALORISATION_TARIFAIRE,
        TypeAvenant.CHANGEMENT_APPORTEUR,
        TypeAvenant.CHANGEMENT_FRACTIONNEMENT,
    }
)


class TransitionInterdite(Exception):
    """La transition de statut demandee n'est pas autorisee par le cycle de vie."""


class MotifResiliationManquant(Exception):
    """Une resiliation a ete demandee sans motif. La specification l'exige, le code le refuse."""


class ContratResilie(Exception):
    """Le contrat est resilie : il n'accepte plus aucun avenant."""


def transition_possible(depart: StatutContrat, arrivee: StatutContrat) -> bool:
    """Dit si le passage d'un statut a un autre est autorise."""
    return arrivee in TRANSITIONS_AUTORISEES[depart]


def verifie_transition(
    depart: StatutContrat,
    arrivee: StatutContrat,
    type_avenant: TypeAvenant,
    motif_resiliation: object | None = None,
) -> None:
    """Verifie qu'un avenant est recevable. Leve une exception explicite sinon.

    Appelee par le service avant toute ecriture. Les trois refus possibles portent des exceptions
    distinctes, pour que l'API puisse repondre un message utile au gestionnaire plutot qu'un refus
    generique.
    """
    if depart is StatutContrat.RESILIE:
        raise ContratResilie(
            "le contrat est resilie : la reprise d'un adherent se fait par une nouvelle souscription,"
            " avec un nouveau numero de contrat"
        )

    if type_avenant in TYPES_SANS_CHANGEMENT_STATUT:
        if arrivee is not depart:
            raise TransitionInterdite(
                f"un avenant de type {type_avenant.value} ne change pas le statut du contrat,"
                f" or il est demande de passer de {depart.value} a {arrivee.value}"
            )
        return

    if arrivee is depart:
        raise TransitionInterdite(
            f"un avenant de type {type_avenant.value} doit changer le statut, or le contrat reste"
            f" a {depart.value}"
        )

    if not transition_possible(depart, arrivee):
        raise TransitionInterdite(
            f"passage de {depart.value} a {arrivee.value} refuse : les seules suites possibles de"
            f" {depart.value} sont {sorted(s.value for s in TRANSITIONS_AUTORISEES[depart]) or ['aucune']}"
        )

    type_attendu = TYPE_ATTENDU_PAR_TRANSITION[(depart, arrivee)]
    if type_avenant is not type_attendu:
        raise TransitionInterdite(
            f"passage de {depart.value} a {arrivee.value} attend un avenant de type"
            f" {type_attendu.value}, or le type fourni est {type_avenant.value}"
        )

    if arrivee is StatutContrat.RESILIE and motif_resiliation is None:
        raise MotifResiliationManquant(
            "une resiliation exige un motif : echeance, infra_annuelle, non_paiement,"
            " changement_situation, deces, par_compagnie ou transfert_portefeuille"
        )
