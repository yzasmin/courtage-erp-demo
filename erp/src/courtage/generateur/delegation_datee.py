"""Lecture de la delegation de gestion a une date donnee, pour le generateur.

Module separe du paquet `generateur` pour une raison technique simple : `generateur.financier` en a
besoin, et `generateur/__init__.py` importe `generateur.financier`. Les loger ensemble creerait un
cycle d'imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from courtage.modeles.enums import ActeGestion


@dataclass(frozen=True, slots=True)
class PerimetreDate:
    """Perimetre delegue sur un produit pendant une periode de validite."""

    date_debut: date
    date_fin: date | None
    actes: frozenset[ActeGestion]

    def couvre(self, jour: date) -> bool:
        return self.date_debut <= jour and (self.date_fin is None or jour <= self.date_fin)


class DelegationDatee:
    """Actes delegues sur un produit, **a une date donnee**.

    Pourquoi la date compte ici autant que dans l'API. Une convention de delegation se renouvelle par
    avenant annuel, et le renouvellement prend parfois du retard : le cabinet perd alors son mandat du
    jour ou la convention expire. Lire la delegation une fois pour toute la generation, comme le faisait
    la version precedente, revenait a ignorer les dates de validite et a quittancer un produit dont la
    convention etait echue depuis des mois.

    La regle appliquee est celle du domaine : aucune convention en vigueur signifie aucun acte autorise,
    jamais l'inverse.
    """

    def __init__(self, periodes: dict[str, list[PerimetreDate]]) -> None:
        self._periodes = periodes

    def actes(self, code_produit: str, jour: date) -> frozenset[ActeGestion]:
        for periode in self._periodes.get(code_produit, ()):
            if periode.couvre(jour):
                return periode.actes
        return frozenset()

    def autorise(self, code_produit: str, acte: ActeGestion, jour: date) -> bool:
        return acte in self.actes(code_produit, jour)

    def jamais_delegue(self, code_produit: str, acte: ActeGestion) -> bool:
        """Dit si l'acte n'est delegue sur aucune periode. Sert a ecarter un produit d'emblee."""
        return all(acte not in periode.actes for periode in self._periodes.get(code_produit, ()))
