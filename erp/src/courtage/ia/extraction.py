"""Extraction des champs d'un document recu, par un modele de langage en sortie structuree stricte.

Trois frontieres, et une protection a chacune :

1. **Le PDF.** Son texte est lu par pypdf ; un document sans texte (scan sans reconnaissance de
   caracteres) est refuse explicitement plutot que transmis vide au modele.
2. **Le modele.** La reponse est contrainte par un schema JSON (`output_config.format`), puis revalidee
   par Pydantic : le schema garantit la forme, Pydantic garantit les types Python (une date valide, un
   entier de centimes). Une reponse tronquee ou refusee leve une erreur au lieu de produire un dossier.
3. **Le mode hors ligne.** Chaque reponse reelle est enregistree avec l'empreinte de la requete qui l'a
   produite. Le mode `enregistre` rejoue ces reponses sans cle d'API, ce qui fait passer les tests et
   l'evaluation partout ; si le texte du document, les instructions ou le schema changent, l'empreinte
   ne correspond plus et le rejeu refuse, au lieu de mesurer un ancien comportement.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

MODELE = "claude-haiku-5-5"
#: Tarif public de Claude Haiku 5.5 pour les requetes de moins de 100 000 jetons, en dollars par million
#: de jetons, releve le 27/10/2026 dans la documentation d'Anthropic (table du 06/10/2026).
PRIX_ENTREE_PAR_MILLION = 0.10
PRIX_SORTIE_PAR_MILLION = 0.50

TYPES_DOCUMENT = ("demande_remboursement", "facture_optique", "facture_dentaire", "attestation_employeur")
CODES_GARANTIE = ("SOINS", "HOSPI", "OPTIQUE", "DENTAIRE", "AUDIO")

INSTRUCTIONS = """Tu lis des documents recus par un cabinet de courtage en assurance sante : demandes de \
remboursement, factures d'opticien, notes d'honoraires de dentiste, attestations d'employeur. Tu en \
extrais les champs demandes, sans rien inventer.

Regles :
- Recopie les noms, prenoms et numeros exactement comme ils sont ecrits, fautes comprises : ne corrige \
rien. Le nom de famille sans civilite, avec la casse d'origine si elle est mixte, en majuscules s'il est \
ecrit en majuscules.
- numero_adherent : le numero d'adherent ou d'affilie, de la forme A suivi de chiffres. null s'il n'est \
pas ecrit.
- numero_contrat : le numero de contrat ou de certificat d'adhesion, par exemple SI-2024-00012.
- date_document : la date des soins pour une demande, une facture ou une note d'honoraires ; la date de \
signature pour une attestation. Format AAAA-MM-JJ.
- emetteur : qui a etabli le document, sans l'adresse (le cabinet, l'opticien, le praticien, l'employeur).
- code_garantie : SOINS pour les consultations, la pharmacie, les analyses, la kinesitherapie et la \
radiologie ; OPTIQUE pour les lunettes et lentilles ; DENTAIRE pour les soins et protheses dentaires ; \
HOSPI pour une hospitalisation ; AUDIO pour les appareils auditifs ; null pour une attestation.
- montant_total_cents : le total tel qu'il est ecrit sur le document, en centimes, meme s'il ne fait pas \
la somme des lignes. null s'il n'y a pas de montant.
- lignes : chaque ligne de prestation avec son montant en centimes ; liste vide s'il n'y en a pas."""


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "type_document",
        "numero_adherent",
        "nom",
        "prenom",
        "numero_contrat",
        "date_document",
        "emetteur",
        "code_garantie",
        "montant_total_cents",
        "lignes",
    ],
    "properties": {
        "type_document": {"type": "string", "enum": list(TYPES_DOCUMENT)},
        "numero_adherent": _nullable({"type": "string"}),
        "nom": _nullable({"type": "string"}),
        "prenom": _nullable({"type": "string"}),
        "numero_contrat": _nullable({"type": "string"}),
        "date_document": _nullable({"type": "string", "format": "date"}),
        "emetteur": _nullable({"type": "string"}),
        "code_garantie": _nullable({"type": "string", "enum": list(CODES_GARANTIE)}),
        "montant_total_cents": _nullable({"type": "integer"}),
        "lignes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["libelle", "montant_cents"],
                "properties": {"libelle": {"type": "string"}, "montant_cents": {"type": "integer"}},
            },
        },
    },
}


class Ligne(BaseModel):
    model_config = ConfigDict(extra="forbid")
    libelle: str
    montant_cents: int


class ChampsExtraits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type_document: Literal["demande_remboursement", "facture_optique", "facture_dentaire", "attestation_employeur"]
    numero_adherent: str | None
    nom: str | None
    prenom: str | None
    numero_contrat: str | None
    date_document: date | None
    emetteur: str | None
    code_garantie: Literal["SOINS", "HOSPI", "OPTIQUE", "DENTAIRE", "AUDIO"] | None
    montant_total_cents: int | None
    lignes: list[Ligne]


class DocumentIllisible(Exception):
    """Le PDF ne contient pas de texte exploitable."""


class ExtractionImpossible(Exception):
    """Le modele n'a pas rendu de reponse exploitable, ou la reponse enregistree est absente ou perimee."""


@dataclass(frozen=True)
class Reponse:
    champs: ChampsExtraits
    modele: str
    jetons_entree: int
    jetons_sortie: int
    latence_ms: int

    @property
    def cout_microdollars(self) -> int:
        return round(self.jetons_entree * PRIX_ENTREE_PAR_MILLION + self.jetons_sortie * PRIX_SORTIE_PAR_MILLION)


def texte_du_pdf(chemin: Path) -> str:
    # Import local : l'ecran de validation de l'ERP importe ce module sans avoir besoin de lire un PDF,
    # et l'image de l'application n'embarque pas l'option `ia`.
    from pypdf import PdfReader

    texte ="\n".join(page.extract_text() or "" for page in PdfReader(chemin).pages).strip()
    if not texte:
        raise DocumentIllisible(f"{chemin.name} : aucun texte lisible, document a ressaisir")
    return texte


def empreinte_requete(texte: str) -> str:
    """Empreinte de tout ce qui determine la reponse : modele, instructions, schema et texte du document."""
    contenu = json.dumps([MODELE, INSTRUCTIONS, SCHEMA, texte], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(contenu.encode("utf-8")).hexdigest()


def _valide(texte_reponse: str) -> ChampsExtraits:
    try:
        return ChampsExtraits.model_validate_json(texte_reponse)
    except ValidationError as erreur:
        raise ExtractionImpossible(f"reponse hors schema : {erreur.error_count()} erreur(s)") from erreur


def _appel_api(texte: str) -> tuple[str, int, int, int]:
    import anthropic

    # Une cle personnelle (`sk-ant-usr`) n'est rattachee a aucun espace de travail : l'API exige alors
    # l'identifiant de l'espace dans un en-tete. Une cle d'espace de travail s'en passe.
    espace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    client = anthropic.Anthropic(default_headers={"anthropic-workspace-id": espace} if espace else None)
    debut = time.perf_counter()
    reponse = client.messages.create(
        model=MODELE,
        max_tokens=2000,
        system=INSTRUCTIONS,
        messages=[{"role": "user", "content": f"Document recu :\n\n{texte}"}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
    )
    latence_ms = round((time.perf_counter() - debut) * 1000)
    if reponse.stop_reason != "end_turn":
        raise ExtractionImpossible(f"reponse interrompue : {reponse.stop_reason}")
    texte_reponse = next(bloc.text for bloc in reponse.content if bloc.type == "text")
    return texte_reponse, reponse.usage.input_tokens, reponse.usage.output_tokens, latence_ms


def extrait(texte: str, nom_document: str, mode: str, dossier_reponses: Path) -> Reponse:
    """Extrait les champs du texte, en appelant le modele (`api`) ou en rejouant (`enregistre`)."""
    empreinte = empreinte_requete(texte)
    chemin = dossier_reponses / f"{Path(nom_document).stem}.json"
    if mode == "api":
        texte_reponse, entree, sortie, latence = _appel_api(texte)
        dossier_reponses.mkdir(parents=True, exist_ok=True)
        enregistrement = {
            "modele": MODELE,
            "empreinte_requete": empreinte,
            "reponse_texte": texte_reponse,
            "jetons_entree": entree,
            "jetons_sortie": sortie,
            "latence_ms": latence,
        }
        chemin.write_text(json.dumps(enregistrement, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    elif mode == "enregistre":
        if not chemin.exists():
            raise ExtractionImpossible(f"aucune reponse enregistree pour {nom_document} : relancer en mode api")
        enregistrement = json.loads(chemin.read_text(encoding="utf-8"))
        if enregistrement["empreinte_requete"] != empreinte:
            raise ExtractionImpossible(
                f"reponse enregistree perimee pour {nom_document} : le document, les instructions ou le schema "
                "ont change depuis l'enregistrement, relancer en mode api"
            )
    else:
        raise ValueError(f"mode inconnu : {mode}")
    return Reponse(
        champs=_valide(enregistrement["reponse_texte"]),
        modele=enregistrement["modele"],
        jetons_entree=enregistrement["jetons_entree"],
        jetons_sortie=enregistrement["jetons_sortie"],
        latence_ms=enregistrement["latence_ms"],
    )
