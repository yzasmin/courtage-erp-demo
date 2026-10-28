"""Arithmetique des montants et des taux, et lecture des taux dates.

Deux regles, et elles ne sont pas negociables dans un ERP qui produit des bordereaux.

**Les montants sont des entiers de centimes.** Aucun flottant n'entre ici. Un bordereau de commissions
dont le total ne tombe pas au centime pres est une anomalie comptable, et il suffit d'additionner
quelques milliers de flottants pour en fabriquer une.

**L'arrondi est explicite et unique.** Appliquer un taux produit presque toujours une fraction de
centime. Le projet arrondit au centime le plus proche, en arrondissant la demie vers le haut, c'est-a-dire
la regle commerciale habituelle. Elle est implementee une seule fois, dans `applique_taux`, et tout le
reste du projet passe par cette fonction. Deux arrondis differents dans deux coins du code produiraient
des bordereaux qui ne se reconcilient pas.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from courtage.modeles.referentiel import TauxCommission, TauxRetrocession, TauxTaxe

#: Taux plein, en points de base. 10 000 points de base valent 100 pour cent.
TAUX_PLEIN_BP = 10_000

#: Un pour cent, en points de base.
UN_POURCENT_BP = 100

#: Espace insecable, separateur de milliers et separateur avant le symbole monetaire en typographie
#: francaise.
ESPACE_FINE = " "


class TauxIntrouvable(Exception):
    """Aucun taux n'est en vigueur a la date demandee.

    Ce n'est pas un cas limite theorique : si une compagnie n'a pas de bareme saisi pour l'annee en
    cours, le calcul doit s'arreter en le disant, et non appliquer zero en silence. Un commissionnement
    silencieusement nul serait decouvert des mois plus tard, a la lecture d'un bordereau vide.
    """


def applique_taux(montant_cents: int, taux_bp: int) -> int:
    """Applique un taux exprime en points de base a un montant en centimes.

    Arrondi au centime le plus proche, demie vers le haut.

    >>> applique_taux(10_000, 1_250)   # 100,00 euros a 12,5 pour cent
    1250
    >>> applique_taux(333, 1_250)      # 3,33 euros a 12,5 pour cent, soit 0,41625 euro
    42
    """
    if taux_bp < 0:
        raise ValueError(f"taux negatif : {taux_bp} points de base")
    produit = Decimal(montant_cents) * Decimal(taux_bp) / Decimal(TAUX_PLEIN_BP)
    return int(produit.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def pourcent_vers_bp(pourcentage: str | Decimal) -> int:
    """Convertit un pourcentage lisible en points de base. `"13,27"` ou `"13.27"` donnent 1327."""
    texte = str(pourcentage).replace(",", ".")
    return int((Decimal(texte) * UN_POURCENT_BP).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def bp_vers_pourcent(taux_bp: int) -> Decimal:
    """Convertit des points de base en pourcentage, pour l'affichage. 1327 donne `Decimal("13.27")`."""
    return (Decimal(taux_bp) / UN_POURCENT_BP).quantize(Decimal("0.01"))


def euros_vers_centimes(euros: str | Decimal | int) -> int:
    """Convertit un montant en euros vers des centimes. `"35,40"` donne 3540."""
    texte = str(euros).replace(",", ".").replace(" ", "").replace(" ", "")
    return int((Decimal(texte) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def centimes_vers_euros(montant_cents: int) -> Decimal:
    """Convertit des centimes vers un montant decimal en euros."""
    return (Decimal(montant_cents) / 100).quantize(Decimal("0.01"))


def formate_euros(montant_cents: int) -> str:
    """Formate un montant pour l'affichage francais : `3540` donne `"35,40 €"`.

    L'espace insecable separe les milliers, comme dans toute edition francaise de montant.
    """
    valeur = centimes_vers_euros(montant_cents)
    entier, _, decimales = f"{abs(valeur):.2f}".partition(".")
    groupes = []
    while len(entier) > 3:
        groupes.insert(0, entier[-3:])
        entier = entier[:-3]
    groupes.insert(0, entier)
    signe = "-" if montant_cents < 0 else ""
    return f"{signe}{ESPACE_FINE.join(groupes)},{decimales}{ESPACE_FINE}€"


def formate_pourcent(taux_bp: int) -> str:
    """Formate un taux pour l'affichage francais : `1327` donne `"13,27 %"`."""
    return f"{bp_vers_pourcent(taux_bp)}".replace(".", ",") + ESPACE_FINE + "%"


def _filtre_validite(requete: Select, modele, a_la_date: date) -> Select:
    """Restreint une requete aux lignes en vigueur a la date donnee."""
    return requete.where(
        modele.date_debut_validite <= a_la_date,
        or_(modele.date_fin_validite.is_(None), modele.date_fin_validite >= a_la_date),
    )


def taux_taxe_applicable(session: Session, branche_id: int, a_la_date: date) -> TauxTaxe:
    """Taux de taxe en vigueur pour une branche a une date donnee.

    En cas de plusieurs lignes valides, la plus recemment entree en vigueur l'emporte : c'est le
    comportement attendu quand un nouveau bareme est saisi sans clore formellement le precedent.
    """
    requete = _filtre_validite(select(TauxTaxe).where(TauxTaxe.branche_id == branche_id), TauxTaxe, a_la_date)
    taux = session.scalars(requete.order_by(TauxTaxe.date_debut_validite.desc())).first()
    if taux is None:
        raise TauxIntrouvable(f"aucun taux de taxe en vigueur le {a_la_date:%d/%m/%Y} pour la branche {branche_id}")
    return taux


def taux_commission_applicable(session: Session, produit_id: int, a_la_date: date) -> TauxCommission:
    """Taux de commission en vigueur pour un produit a une date donnee."""
    requete = _filtre_validite(
        select(TauxCommission).where(TauxCommission.produit_id == produit_id), TauxCommission, a_la_date
    )
    taux = session.scalars(requete.order_by(TauxCommission.date_debut_validite.desc())).first()
    if taux is None:
        raise TauxIntrouvable(
            f"aucun taux de commission en vigueur le {a_la_date:%d/%m/%Y} pour le produit {produit_id}"
        )
    return taux


def taux_retrocession_applicable(
    session: Session, apporteur_id: int, produit_id: int, a_la_date: date
) -> TauxRetrocession:
    """Taux de retrocession en vigueur pour un apporteur, pour un produit, a une date donnee.

    Un accord specifique a un produit l'emporte sur l'accord cadre, qui est la ligne dont `produit_id`
    est nul. C'est la pratique du metier : on signe un cadre avec un apporteur, puis on y deroge produit
    par produit quand la marge le justifie.
    """
    requete = _filtre_validite(
        select(TauxRetrocession).where(
            TauxRetrocession.apporteur_id == apporteur_id,
            or_(TauxRetrocession.produit_id == produit_id, TauxRetrocession.produit_id.is_(None)),
        ),
        TauxRetrocession,
        a_la_date,
    )
    candidats = list(
        session.scalars(
            requete.order_by(
                # Un taux porte par un produit precis passe devant l'accord cadre.
                TauxRetrocession.produit_id.is_(None),
                TauxRetrocession.date_debut_validite.desc(),
            )
        )
    )
    if not candidats:
        raise TauxIntrouvable(
            f"aucun taux de retrocession en vigueur le {a_la_date:%d/%m/%Y}"
            f" pour l'apporteur {apporteur_id} et le produit {produit_id}"
        )
    return candidats[0]
