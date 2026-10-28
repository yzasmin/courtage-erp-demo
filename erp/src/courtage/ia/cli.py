"""Commandes `courtage ia` : fabriquer les documents, les traiter, mesurer l'extraction.

`--mode api` appelle le modele et enregistre chaque reponse ; `--mode enregistre`, le defaut, rejoue les
reponses enregistrees, sans cle d'API. L'evaluation mesuree en mode `api` et celle rejouee en mode
`enregistre` donnent donc les memes chiffres, couts et latences compris, puisque ceux-ci sont
enregistres avec la reponse.
"""

from __future__ import annotations

import json
import os
import statistics
from datetime import date
from pathlib import Path

from sqlalchemy import select

from courtage.config import RACINE_PROJET
from courtage.db import session_portee
from courtage.flux.cli import _ecrit
from courtage.ia import extraction, traitement
from courtage.ia.documents import genere_documents
from courtage.modeles import DossierPreRempli

CHAMPS_EVALUES = [
    "type_document",
    "numero_adherent",
    "nom",
    "prenom",
    "numero_contrat",
    "date_document",
    "emetteur",
    "code_garantie",
    "montant_total_cents",
    "somme_lignes_cents",
]
#: Controle qui doit se declencher pour chaque anomalie volontaire, et le niveau minimal attendu.
CONTROLE_ATTENDU = {
    "nom_altere": ("nom", "alerte"),
    "adherent_inexistant": ("adherent", "bloquant"),
    "total_faux": ("montants", "bloquant"),
    "soins_avant_effet": ("garantie_a_date", "bloquant"),
}


def _racine_donnees() -> Path:
    return Path(os.environ.get("COURTAGE_IA_DONNEES", RACINE_PROJET / "data" / "ia"))


def _documents() -> Path:
    return _racine_donnees() / "documents"


def _reponses() -> Path:
    return _racine_donnees() / "reponses"


def commande_generer(_args) -> int:
    with session_portee() as session:
        verites = genere_documents(session, _documents())
    print(f"{len(verites)} documents ecrits dans {_documents()}")
    return 0


def commande_traiter(args) -> int:
    """Depose un dossier `a_valider` par document non encore traite. N'ecrit rien d'autre."""
    deposes = echecs = 0
    for chemin in sorted(_documents().glob("*.pdf")):
        with session_portee() as session:
            existe = session.scalars(
                select(DossierPreRempli.id).where(DossierPreRempli.nom_fichier == chemin.name)
            ).first()
            if existe:
                continue
            try:
                reponse = extraction.extrait(extraction.texte_du_pdf(chemin), chemin.name, args.mode, _reponses())
                traitement.cree_dossier(session, chemin.name, reponse)
                deposes += 1
            except (extraction.DocumentIllisible, extraction.ExtractionImpossible) as erreur:
                traitement.cree_dossier_en_echec(session, chemin.name, str(erreur))
                echecs += 1
    print(f"{deposes} dossiers deposes a valider, {echecs} documents non lus")
    return 0


def _egal(champ: str, attendu, obtenu) -> bool:
    if attendu is None or obtenu is None:
        return attendu is None and obtenu is None
    if isinstance(attendu, str):
        return traitement._normalise(attendu) == traitement._normalise(str(obtenu))
    return attendu == obtenu


def _valeurs_obtenues(champs: extraction.ChampsExtraits) -> dict:
    valeurs = champs.model_dump(mode="json")
    valeurs["somme_lignes_cents"] = sum(ligne.montant_cents for ligne in champs.lignes) if champs.lignes else None
    return valeurs


def commande_evaluer(args) -> int:
    verites = json.loads((_documents() / "verite_terrain.json").read_text(encoding="utf-8"))
    justes = {champ: 0 for champ in CHAMPS_EVALUES}
    documents_exacts = 0
    erreurs: list[dict] = []
    anomalies: list[dict] = []
    faux_bloquants: list[str] = []
    latences: list[int] = []
    couts: list[int] = []
    jetons: list[tuple[int, int]] = []
    echecs: list[str] = []

    with session_portee() as session:
        for verite in verites:
            chemin = _documents() / verite["fichier"]
            try:
                reponse = extraction.extrait(extraction.texte_du_pdf(chemin), chemin.name, args.mode, _reponses())
            except (extraction.DocumentIllisible, extraction.ExtractionImpossible) as erreur:
                echecs.append(f"{chemin.name} : {erreur}")
                continue
            latences.append(reponse.latence_ms)
            couts.append(reponse.cout_microdollars)
            jetons.append((reponse.jetons_entree, reponse.jetons_sortie))
            obtenues = _valeurs_obtenues(reponse.champs)
            exact = True
            for champ in CHAMPS_EVALUES:
                if _egal(champ, verite[champ], obtenues[champ]):
                    justes[champ] += 1
                else:
                    exact = False
                    erreurs.append(
                        {"fichier": chemin.name, "champ": champ, "attendu": verite[champ], "obtenu": obtenues[champ]}
                    )
            documents_exacts += exact

            controles, *_ = traitement.controle(session, reponse.champs)
            if verite["anomalie_attendue"]:
                code, niveau = CONTROLE_ATTENDU[verite["anomalie_attendue"]]
                vus = [c for c in controles if c.code == code and c.niveau in ({niveau, "bloquant"})]
                anomalies.append(
                    {
                        "fichier": chemin.name,
                        "anomalie": verite["anomalie_attendue"],
                        "detectee": bool(vus),
                        "message": vus[0].message if vus else None,
                    }
                )
            elif any(c.niveau == "bloquant" for c in controles):
                bloquant = next(c for c in controles if c.niveau == "bloquant")
                faux_bloquants.append(f"{chemin.name} : {bloquant.message}")
        session.rollback()

    lus = len(latences)
    total_champs = lus * len(CHAMPS_EVALUES)
    resultat = {
        "date": date.today().isoformat(),
        "mode": args.mode,
        "modele": extraction.MODELE,
        "documents": len(verites),
        "documents_lus": lus,
        "echecs_de_lecture": echecs,
        "precision_globale": round(sum(justes.values()) / total_champs, 4) if total_champs else None,
        "precision_par_champ": {champ: round(n / lus, 4) if lus else None for champ, n in justes.items()},
        "documents_entierement_exacts": documents_exacts,
        "erreurs": erreurs,
        "anomalies_detectees": sum(a["detectee"] for a in anomalies),
        "anomalies_attendues": len(anomalies),
        "detail_anomalies": anomalies,
        "documents_sains_bloques_a_tort": faux_bloquants,
        "latence_moyenne_ms": round(statistics.mean(latences)) if latences else None,
        "latence_max_ms": max(latences) if latences else None,
        "cout_moyen_dollars": round(statistics.mean(couts) / 1e6, 6) if couts else None,
        "cout_total_dollars": round(sum(couts) / 1e6, 6),
        "jetons_entree_moyens": round(statistics.mean(e for e, _ in jetons)) if jetons else None,
        "jetons_sortie_moyens": round(statistics.mean(s for _, s in jetons)) if jetons else None,
    }
    _ecrit("ia_evaluation.json", resultat)
    (RACINE_PROJET / "docs" / "eval-extraction.md").write_text(_rapport(resultat), encoding="utf-8")
    print(
        f"precision globale {resultat['precision_globale']} sur {lus} documents ; anomalies "
        f"{resultat['anomalies_detectees']}/{resultat['anomalies_attendues']} ; cout moyen "
        f"{resultat['cout_moyen_dollars']} $ ; latence moyenne {resultat['latence_moyenne_ms']} ms"
    )
    return 0 if not echecs else 1


def _pourcent(valeur: float | None) -> str:
    return "non mesure" if valeur is None else f"{100 * valeur:.1f} %".replace(".", ",")


def _rapport(r: dict) -> str:
    libelles = {
        "type_document": "type de document",
        "numero_adherent": "numéro d'adhérent",
        "nom": "nom",
        "prenom": "prénom",
        "numero_contrat": "numéro de contrat",
        "date_document": "date des soins ou de l'attestation",
        "emetteur": "émetteur",
        "code_garantie": "garantie invoquée",
        "montant_total_cents": "total écrit",
        "somme_lignes_cents": "somme des lignes lues",
    }
    lignes_champs = "\n".join(
        f"| {libelles[c]} | {_pourcent(v)} |" for c, v in r["precision_par_champ"].items()
    )
    lignes_anomalies = "\n".join(
        f"| {a['fichier']} | {a['anomalie'].replace('_', ' ')} | {'oui' if a['detectee'] else '**non**'} | "
        f"{a['message'] or '-'} |"
        for a in r["detail_anomalies"]
    )
    lignes_erreurs = (
        "\n".join(f"| {e['fichier']} | {libelles[e['champ']]} | {e['attendu']} | {e['obtenu']} |" for e in r["erreurs"])
        or "| aucune | | | |"
    )
    faux = "\n".join(f"- {f}" for f in r["documents_sains_bloques_a_tort"]) or "Aucun."
    cout = "non mesuré" if r["cout_moyen_dollars"] is None else f"{r['cout_moyen_dollars']:.6f} $".replace(".", ",")
    return f"""Évaluation de l'agent de pré-remplissage (module 4a), écrite par `courtage ia evaluer` le \
{r['date']} à partir de `results/ia_evaluation.json`. Ne pas modifier à la main.

# Évaluation de l'extraction

Modèle `{r['modele']}`, mode `{r['mode']}`. Les 30 documents sont fictifs ; leur vérité terrain est dans
`data/ia/documents/verite_terrain.json`. En mode `enregistre`, les réponses rejouées sont celles d'un
appel réel au modèle, enregistrées avec leur nombre de jetons et leur latence.

## Résultat

| Mesure | Valeur |
| --- | --- |
| Documents lus | {r['documents_lus']} sur {r['documents']} |
| Précision globale, champ par champ | {_pourcent(r['precision_globale'])} |
| Documents entièrement exacts | {r['documents_entierement_exacts']} sur {r['documents_lus']} |
| Anomalies volontaires relevées par les contrôles | {r['anomalies_detectees']} sur {r['anomalies_attendues']} |
| Documents sains bloqués à tort | {len(r['documents_sains_bloques_a_tort'])} |
| Coût moyen par document | {cout} |
| Latence moyenne par document | {r['latence_moyenne_ms']} ms (maximum {r['latence_max_ms']} ms) |
| Jetons moyens par document | {r['jetons_entree_moyens']} en entrée, {r['jetons_sortie_moyens']} en sortie |

## Précision par champ

| Champ | Précision |
| --- | --- |
{lignes_champs}

## Anomalies volontaires

| Document | Anomalie | Relevée | Message affiché au gestionnaire |
| --- | --- | --- | --- |
{lignes_anomalies}

## Documents sains bloqués à tort

{faux}

## Erreurs d'extraction

| Document | Champ | Attendu | Lu |
| --- | --- | --- | --- |
{lignes_erreurs}

## Rejouer

```bash
python -m uv run --extra ia courtage ia evaluer                 # rejoue les réponses enregistrées
python -m uv run --extra ia courtage ia evaluer --mode api      # appelle le modèle (clé d'API requise)
```
"""


def ajoute_commandes(sous_analyseurs) -> None:
    """Greffe le groupe de commandes `ia` sur l'analyseur principal du client."""
    ia = sous_analyseurs.add_parser("ia", help="agent de pre-remplissage : documents, dossiers, evaluation")
    actions = ia.add_subparsers(dest="action", required=True)
    actions.add_parser("generer", help="ecrit les 30 PDF de demonstration et leur verite terrain").set_defaults(
        fonction=commande_generer
    )
    for nom, aide, fonction in (
        ("traiter", "depose un dossier a valider par document recu", commande_traiter),
        ("evaluer", "mesure l'extraction contre la verite terrain, ecrit docs/eval-extraction.md", commande_evaluer),
    ):
        commande = actions.add_parser(nom, help=aide)
        commande.add_argument("--mode", choices=["enregistre", "api"], default="enregistre")
        commande.set_defaults(fonction=fonction)
