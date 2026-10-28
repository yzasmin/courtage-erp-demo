"""Rapport de detection : ce que les controles ont trouve, compare au manifeste de verite terrain.

Le but de ce module est de repondre honnetement a une question que personne ne pose a un projet de
demonstration : **les controles trouvent-ils vraiment les anomalies ?** Sans manifeste, un rapport
d'anomalies prouve seulement que du code s'est execute.

Le manifeste liste chaque anomalie injectee par le generateur. Le rapport compare les deux ensembles,
anomalie par anomalie, et publie le rappel et la precision reellement calcules.

| Mesure | Definition | Ce qu'elle dit au metier |
| --- | --- | --- |
| rappel | anomalies injectees retrouvees sur anomalies injectees | ce qui passe a travers les controles |
| precision | anomalies injectees retrouvees sur anomalies signalees | le bruit impose au gestionnaire |

La cle de rapprochement est le triplet nom de fichier, code d'anomalie, reference de la ligne. Le numero
de ligne n'est pas retenu : un controle de doublon porte legitimement son signalement sur une autre
occurrence que celle que le generateur a modifiee, alors que la cle metier dupliquee, elle, est la meme
des deux cotes. Pour les anomalies de niveau fichier, le total incoherent et l'empreinte deja connue, la
reference est nulle des deux cotes.

**Ce que le rapport ne cache pas.** Les deux controles non bloquants sont des jugements statistiques :
un montant inhabituel et une consommation proche d'un plafond existent aussi sans avoir ete injectes.
Ils apparaissent donc en faux positifs, et la precision en tient compte. Les masquer donnerait une
precision de un, et une mesure sans valeur.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from courtage.flux.enums import SEVERITE_PAR_CODE, CodeAnomalie, SeveriteAnomalie
from courtage.flux.modeles import AnomalieLot, LotFichier
from courtage.flux.parametres import NOM_MANIFESTE, racine_flux

#: Cle de rapprochement d'une anomalie : fichier, code, reference de ligne.
CleAnomalie = tuple[str, str, str | None]


class ManifesteIntrouvable(FileNotFoundError):
    """Le manifeste de verite terrain est absent, donc aucun rappel ni aucune precision n'est calculable.

    Levee plutot que contournee : publier un rapport de detection sans verite terrain reviendrait a
    publier un taux que rien ne soutient, ce que la specification interdit explicitement.
    """


@dataclass(slots=True)
class ComptesDetection:
    """Vrais positifs, faux positifs et faux negatifs, avec leur rappel et leur precision."""

    vrais_positifs: int = 0
    faux_positifs: int = 0
    faux_negatifs: int = 0

    @property
    def rappel(self) -> float | None:
        injectees = self.vrais_positifs + self.faux_negatifs
        return None if injectees == 0 else self.vrais_positifs / injectees

    @property
    def precision(self) -> float | None:
        signalees = self.vrais_positifs + self.faux_positifs
        return None if signalees == 0 else self.vrais_positifs / signalees

    def en_dictionnaire(self) -> dict[str, object]:
        return {
            "injectees": self.vrais_positifs + self.faux_negatifs,
            "signalees": self.vrais_positifs + self.faux_positifs,
            "vrais_positifs": self.vrais_positifs,
            "faux_positifs": self.faux_positifs,
            "faux_negatifs": self.faux_negatifs,
            "rappel": None if self.rappel is None else round(self.rappel, 4),
            "precision": None if self.precision is None else round(self.precision, 4),
        }


@dataclass(slots=True)
class RapportDetection:
    """Rapport complet, enregistre dans `results/`."""

    manifeste: str
    global_: ComptesDetection = field(default_factory=ComptesDetection)
    par_code: dict[str, ComptesDetection] = field(default_factory=dict)
    par_severite: dict[str, ComptesDetection] = field(default_factory=dict)
    exemples_faux_positifs: list[dict[str, object]] = field(default_factory=list)
    exemples_faux_negatifs: list[dict[str, object]] = field(default_factory=list)
    lots_rejetes: list[dict[str, object]] = field(default_factory=list)

    def en_dictionnaire(self) -> dict[str, object]:
        return {
            "manifeste": self.manifeste,
            "ensemble": self.global_.en_dictionnaire(),
            "par_code": {code: comptes.en_dictionnaire() for code, comptes in sorted(self.par_code.items())},
            "par_severite": {
                severite: comptes.en_dictionnaire()
                for severite, comptes in sorted(self.par_severite.items())
            },
            "exemples_faux_positifs": self.exemples_faux_positifs,
            "exemples_faux_negatifs": self.exemples_faux_negatifs,
            "lots_rejetes": self.lots_rejetes,
        }


def chemin_manifeste() -> Path:
    return racine_flux() / NOM_MANIFESTE


def _lit_manifeste(chemin: Path) -> dict[CleAnomalie, dict[str, object]]:
    if not chemin.exists():
        raise ManifesteIntrouvable(
            f"manifeste absent : {chemin}. Lancer `courtage flux generer` avant le rapport."
        )
    contenu = json.loads(chemin.read_text(encoding="utf-8"))
    injectees: dict[CleAnomalie, dict[str, object]] = {}
    for anomalie in contenu.get("anomalies", []):
        cle: CleAnomalie = (
            str(anomalie["nom_fichier"]),
            str(anomalie["code"]),
            None if anomalie.get("reference_ligne") in (None, "") else str(anomalie["reference_ligne"]),
        )
        injectees[cle] = anomalie
    return injectees


def _lit_detectees(session: Session) -> dict[CleAnomalie, dict[str, object]]:
    requete = select(
        LotFichier.nom_fichier,
        AnomalieLot.code,
        AnomalieLot.reference_ligne,
        AnomalieLot.ligne_numero,
        AnomalieLot.severite,
        AnomalieLot.message,
    ).join(LotFichier, LotFichier.id == AnomalieLot.lot_id)
    detectees: dict[CleAnomalie, dict[str, object]] = {}
    for nom_fichier, code, reference, ligne_numero, severite, message in session.execute(requete):
        cle: CleAnomalie = (nom_fichier, code.value, reference or None)
        detectees[cle] = {
            "nom_fichier": nom_fichier,
            "code": code.value,
            "reference_ligne": reference,
            "ligne_numero": ligne_numero,
            "severite": severite.value,
            "message": message,
        }
    return detectees


def _severite(code: str) -> str:
    try:
        return SEVERITE_PAR_CODE[CodeAnomalie(code)].value
    except ValueError:
        return SeveriteAnomalie.AVERTISSEMENT.value


def construit_rapport(session: Session, chemin: Path | None = None, exemples: int = 10) -> RapportDetection:
    """Compare les anomalies detectees au manifeste et calcule rappel et precision."""
    fichier = chemin or chemin_manifeste()
    injectees = _lit_manifeste(fichier)
    detectees = _lit_detectees(session)
    rapport = RapportDetection(manifeste=str(fichier))

    par_code: dict[str, ComptesDetection] = defaultdict(ComptesDetection)
    par_severite: dict[str, ComptesDetection] = defaultdict(ComptesDetection)

    for cle in injectees.keys() & detectees.keys():
        code = cle[1]
        rapport.global_.vrais_positifs += 1
        par_code[code].vrais_positifs += 1
        par_severite[_severite(code)].vrais_positifs += 1

    for cle in detectees.keys() - injectees.keys():
        code = cle[1]
        rapport.global_.faux_positifs += 1
        par_code[code].faux_positifs += 1
        par_severite[_severite(code)].faux_positifs += 1
        if len(rapport.exemples_faux_positifs) < exemples:
            rapport.exemples_faux_positifs.append(detectees[cle])

    for cle in injectees.keys() - detectees.keys():
        code = cle[1]
        rapport.global_.faux_negatifs += 1
        par_code[code].faux_negatifs += 1
        par_severite[_severite(code)].faux_negatifs += 1
        if len(rapport.exemples_faux_negatifs) < exemples:
            rapport.exemples_faux_negatifs.append(injectees[cle])

    rapport.par_code = dict(par_code)
    rapport.par_severite = dict(par_severite)

    rapport.lots_rejetes = [
        {
            "reference_lot": reference,
            "nom_fichier": nom,
            "motif": motif,
            "lignes_fichier": lignes,
            "lignes_chargees": chargees,
        }
        for reference, nom, motif, lignes, chargees in session.execute(
            select(
                LotFichier.reference_lot,
                LotFichier.nom_fichier,
                LotFichier.motif_rejet,
                LotFichier.nombre_lignes_fichier,
                LotFichier.nombre_lignes_chargees,
            )
            .where(LotFichier.motif_rejet.is_not(None))
            .order_by(LotFichier.nom_fichier)
        )
    ]
    return rapport
