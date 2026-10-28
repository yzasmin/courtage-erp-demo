"""Controle de la delegation de gestion : le cabinet peut-il accomplir cet acte, sur ce produit, ce jour.

Ce que fait ce module, en une phrase : avant toute ecriture qui engage la compagnie, il verifie qu'une
convention en vigueur delegue bien l'acte au cabinet, et il refuse sinon.

Les actes concernes et le moment du controle :

| Acte | Controle avant |
| --- | --- |
| `affiliation` | la souscription d'un contrat |
| `avenant` | la saisie d'un avenant de modification |
| `radiation` | la resiliation d'un contrat |
| `quittancement` | l'emission d'un appel de cotisation |
| `encaissement` | l'enregistrement d'un encaissement |
| `gestion_prestations` | l'enregistrement d'une prestation sante |
| `gestion_sinistres` | l'enregistrement d'un sinistre de prevoyance |

Deux precisions sur ce qui est volontairement refuse.

**Aucune autorisation par defaut.** Un produit sans convention en vigueur ne permet aucun acte delegue.
L'alternative, autoriser tant qu'aucune convention ne l'interdit, est celle qui produit les incidents :
une convention oubliee a la reprise de donnees laisserait passer des actes que la compagnie n'a jamais
delegues, et l'anomalie ne se verrait qu'au controle de la compagnie.

**La portee declaree n'autorise rien.** Une convention saisie « totale » sans ligne pour les prestations
ne permet pas de regler une prestation. Le libelle sert a l'affichage, la liste des actes seule fait foi.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from courtage.modeles.delegation import ActeDelegue, ConventionDelegation, ConventionProduit
from courtage.modeles.enums import ActeGestion
from courtage.modeles.referentiel import Produit

#: Libelles lisibles des actes, pour les messages d'erreur et l'interface.
#: Libelle metier de chaque acte de gestion. Table unique du projet : l'interface la reexporte au lieu
#: d'en tenir une seconde, parce que deux tables de libelles finissent toujours par diverger, et que
#: c'est arrive ici, l'une accentuee et l'autre non.
LIBELLE_ACTE: dict[ActeGestion, str] = {
    ActeGestion.AFFILIATION: "affiliation d'un adhérent",
    ActeGestion.AVENANT: "saisie d'un avenant",
    ActeGestion.RADIATION: "radiation, ou résiliation d'un contrat",
    ActeGestion.QUITTANCEMENT: "quittancement, ou émission des appels de cotisation",
    ActeGestion.ENCAISSEMENT: "encaissement des cotisations",
    ActeGestion.GESTION_PRESTATIONS: "gestion et règlement des prestations",
    ActeGestion.GESTION_SINISTRES: "gestion des sinistres",
}


class ActeNonDelegue(Exception):
    """L'acte demandé n'est pas délégué au cabinet pour ce produit à cette date.

    Porte l'acte et le produit, pour que l'API compose un message qu'un gestionnaire comprenne sans
    ouvrir la convention papier.

    Le message cite le **code** du produit, et non son identifiant technique. Il disait « produit 8 »,
    ce qui ne veut rien dire pour la personne qui lit le refus : elle connaît ses produits par leur
    code, `PV-ARRET` ou `SI-SENIOR`. L'identifiant reste disponible sur l'exception pour qui veut le
    rattacher a une ligne de base.
    """

    def __init__(
        self,
        acte: ActeGestion,
        produit_id: int,
        a_la_date: date,
        raison: str,
        code_produit: str | None = None,
    ) -> None:
        self.acte = acte
        self.produit_id = produit_id
        self.code_produit = code_produit
        self.a_la_date = a_la_date
        self.raison = raison
        designation = code_produit or f"d'identifiant {produit_id}"
        super().__init__(
            f"{LIBELLE_ACTE[acte]} : acte non délégué au cabinet pour le produit {designation}"
            f" le {a_la_date:%d/%m/%Y} ({raison})"
        )


class DelegationAmbigue(Exception):
    """Deux conventions en vigueur couvrent le meme produit a la meme date.

    Une base relationnelle ne sait pas exprimer de facon portable « pas deux periodes de validite qui se
    chevauchent ». Le controle est donc fait ici, et il est bloquant : deviner laquelle des deux
    conventions s'applique serait pire que s'arreter.
    """


@dataclass(frozen=True, slots=True)
class PerimetreDelegue:
    """Perimetre reellement delegue sur un produit a une date donnee."""

    produit_id: int
    a_la_date: date
    convention_reference: str | None
    portee_declaree: str | None
    actes: frozenset[ActeGestion]

    def autorise(self, acte: ActeGestion) -> bool:
        return acte in self.actes


def _convention_en_vigueur(session: Session, produit_id: int, a_la_date: date) -> ConventionDelegation | None:
    """Convention en vigueur pour un produit a une date donnee. Leve si deux conventions se chevauchent."""
    requete = (
        select(ConventionDelegation)
        .join(ConventionProduit, ConventionProduit.convention_id == ConventionDelegation.id)
        .where(
            ConventionProduit.produit_id == produit_id,
            ConventionDelegation.date_debut_validite <= a_la_date,
            or_(
                ConventionDelegation.date_fin_validite.is_(None),
                ConventionDelegation.date_fin_validite >= a_la_date,
            ),
        )
        .order_by(ConventionDelegation.date_debut_validite.desc())
    )
    conventions = list(session.scalars(requete))
    if len(conventions) > 1:
        references = ", ".join(c.reference for c in conventions)
        raise DelegationAmbigue(
            f"le produit {produit_id} est couvert par {len(conventions)} conventions en vigueur"
            f" le {a_la_date:%d/%m/%Y} : {references}"
        )
    return conventions[0] if conventions else None


def perimetre_delegue(session: Session, produit_id: int, a_la_date: date) -> PerimetreDelegue:
    """Lit le perimetre delegue sur un produit a une date donnee.

    Utilise par l'interface pour griser les actions indisponibles, plutot que de laisser un gestionnaire
    remplir un formulaire qui sera refuse a l'enregistrement.
    """
    convention = _convention_en_vigueur(session, produit_id, a_la_date)
    if convention is None:
        return PerimetreDelegue(
            produit_id=produit_id,
            a_la_date=a_la_date,
            convention_reference=None,
            portee_declaree=None,
            actes=frozenset(),
        )
    actes = frozenset(
        session.scalars(select(ActeDelegue.acte).where(ActeDelegue.convention_id == convention.id))
    )
    return PerimetreDelegue(
        produit_id=produit_id,
        a_la_date=a_la_date,
        convention_reference=convention.reference,
        portee_declaree=convention.portee.value,
        actes=actes,
    )


def verifie_acte_delegue(session: Session, produit_id: int, acte: ActeGestion, a_la_date: date) -> None:
    """Verifie qu'un acte est delegue, ou leve `ActeNonDelegue`.

    A appeler avant l'ecriture, dans le service, et non apres : le refus doit empecher l'ecriture, pas la
    constater.
    """
    perimetre = perimetre_delegue(session, produit_id, a_la_date)
    code_produit = session.scalar(select(Produit.code).where(Produit.id == produit_id))
    if perimetre.convention_reference is None:
        raise ActeNonDelegue(
            acte,
            produit_id,
            a_la_date,
            "aucune convention de délégation en vigueur pour ce produit",
            code_produit,
        )
    if not perimetre.autorise(acte):
        actes_delegues = ", ".join(sorted(a.value for a in perimetre.actes)) or "aucun"
        raise ActeNonDelegue(
            acte,
            produit_id,
            a_la_date,
            f"la convention {perimetre.convention_reference} délègue seulement : {actes_delegues}",
            code_produit,
        )
