"""Fabrication des 30 documents de demonstration et de leur verite terrain.

Les documents citent de vrais adherents et de vrais contrats du jeu de demonstration, pour que les
controles de l'agent aient quelque chose a verifier. Ils sont tous fictifs, comme le portefeuille.

Quelques documents portent une anomalie volontaire, notee dans la verite terrain : un nom mal
orthographie, un numero d'adherent inexistant, un total qui ne fait pas la somme des lignes, des soins
anterieurs a l'effet du contrat. La verite terrain decrit ce que le document **dit**, pas ce que la base
contient : c'est ce que l'extraction doit restituer, et c'est aux controles, ensuite, de relever l'ecart.
"""

from __future__ import annotations

import json
import unicodedata
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from random import Random

from fpdf import FPDF
from sqlalchemy import select
from sqlalchemy.orm import Session

from courtage.modeles import Adherent, Contrat, EntrepriseSouscriptrice, Garantie, Prestation
from courtage.modeles.enums import StatutContrat

GRAINE = 20261019
#: Date de creation inscrite dans les PDF : fixe, pour que deux generations donnent les memes octets.
DATE_CREATION_PDF = datetime(2026, 10, 1, tzinfo=UTC)
#: Ordre des types dans la serie : 10 demandes, 8 factures optiques, 7 notes dentaires, 5 attestations.
REPARTITION = (
    ["demande_remboursement"] * 10
    + ["facture_optique"] * 8
    + ["facture_dentaire"] * 7
    + ["attestation_employeur"] * 5
)
GARANTIE_PAR_TYPE = {"demande_remboursement": "SOINS", "facture_optique": "OPTIQUE", "facture_dentaire": "DENTAIRE"}
#: Anomalies volontaires, par rang du document dans la serie (0 a 29).
NOM_ALTERE = {2, 11, 19}
ADHERENT_INEXISTANT = {6, 22}
TOTAL_FAUX = {14, 23}
SOINS_AVANT_EFFET = {4}

OPTICIENS = [
    "Optique du Lez, 14 avenue de Palavas, 34000 Montpellier",
    "Lunetterie des Arceaux, 3 rue Marioge, 34000 Montpellier",
    "Vision Plus Beziers, 22 allees Paul Riquet, 34500 Beziers",
]
DENTISTES = [
    "Dr Claire Bousquet, chirurgien-dentiste, 8 rue Foch, 34000 Montpellier",
    "Dr Malik Haddad, chirurgien-dentiste, 41 avenue Saint-Saens, 34500 Beziers",
]
ACTES_SOINS = [
    ("Consultation medecin generaliste", 3000),
    ("Consultation specialiste", 6000),
    ("Analyses de biologie", 4250),
    ("Seance de kinesitherapie", 2150),
    ("Pharmacie", 1880),
    ("Radiographie", 3990),
]
ACTES_OPTIQUE = [
    ("Monture", (9000, 21000)),
    ("Verre unifocal droit", (6000, 14000)),
    ("Verre unifocal gauche", (6000, 14000)),
    ("Verre progressif droit", (18000, 32000)),
    ("Verre progressif gauche", (18000, 32000)),
]
ACTES_DENTAIRES = [
    ("Detartrage", (2892, 2892)),
    ("Couronne ceramo-metallique, dent 26", (50000, 75000)),
    ("Soin conservateur, dent 15", (4000, 8000)),
    ("Inlay-core, dent 36", (12000, 25000)),
]


def _euros(cents: int) -> str:
    return f"{cents / 100:,.2f}".replace(",", " ").replace(".", ",") + " EUR"


def _sans_accents(texte: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texte) if unicodedata.category(c) != "Mn")


def _altere(nom: str, alea: Random) -> str:
    """Faute de frappe credible : une lettre doublee ou deux lettres inversees, sans toucher l'initiale."""
    i = alea.randrange(1, len(nom) - 1)
    if alea.random() < 0.5:
        return nom[: i + 1] + nom[i] + nom[i + 1 :]
    return nom[:i] + nom[i + 1] + nom[i] + nom[i + 2 :]


def _candidats_sante(session: Session, code_garantie: str) -> list[tuple[Adherent, Contrat]]:
    """Contrats en cours qui ont deja eu une prestation de cette garantie : la garantie est donc couverte."""
    requete = (
        select(Adherent, Contrat)
        .join(Contrat, Contrat.adherent_id == Adherent.id)
        .join(Prestation, Prestation.contrat_id == Contrat.id)
        .join(Garantie, Garantie.id == Prestation.garantie_id)
        .where(Garantie.code == code_garantie, Contrat.statut == StatutContrat.EN_COURS)
        .distinct()
        .order_by(Contrat.id)
    )
    return list(session.execute(requete).tuples())


def _candidats_affilies(session: Session) -> list[tuple[Adherent, Contrat, EntrepriseSouscriptrice]]:
    requete = (
        select(Adherent, Contrat, EntrepriseSouscriptrice)
        .join(Contrat, Contrat.adherent_id == Adherent.id)
        .join(EntrepriseSouscriptrice, EntrepriseSouscriptrice.id == Adherent.entreprise_id)
        .where(Contrat.statut == StatutContrat.EN_COURS)
        .order_by(Contrat.id)
    )
    return list(session.execute(requete).tuples())


class _Page(FPDF):
    def __init__(self) -> None:
        super().__init__(format="A4")
        self.set_auto_page_break(True, 20)
        self.set_creation_date(DATE_CREATION_PDF)
        self.add_page()

    def ligne(self, texte: str, gras: bool = False, taille: int = 10) -> None:
        self.set_font("Helvetica", "B" if gras else "", taille)
        self.multi_cell(0, 6, texte, new_x="LMARGIN", new_y="NEXT")

    def tableau(self, lignes: list[tuple[str, int]], total_affiche: int) -> None:
        self.ln(2)
        self.set_font("Helvetica", "B", 10)
        self.cell(130, 7, "Designation", border=1)
        self.cell(50, 7, "Montant", border=1, align="R", new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", "", 10)
        for libelle, montant in lignes:
            self.cell(130, 7, libelle, border=1)
            self.cell(50, 7, _euros(montant), border=1, align="R", new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", "B", 10)
        self.cell(130, 7, "Total", border=1)
        self.cell(50, 7, _euros(total_affiche), border=1, align="R", new_x="LMARGIN", new_y="NEXT")


def _document(rang: int, type_document: str, alea: Random, reserves: dict) -> tuple[_Page, dict]:
    page = _Page()
    if type_document == "attestation_employeur":
        adherent, contrat, entreprise = reserves["affilies"].pop(alea.randrange(len(reserves["affilies"])))
    else:
        adherent, contrat = reserves[type_document].pop(alea.randrange(len(reserves[type_document])))
        entreprise = None

    nom = _sans_accents(adherent.nom)
    prenom = _sans_accents(adherent.prenom)
    if rang in NOM_ALTERE:
        nom = _altere(nom, alea)
    numero_adherent = f"A9{alea.randrange(10000, 99999)}" if rang in ADHERENT_INEXISTANT else adherent.numero_adherent
    if rang in SOINS_AVANT_EFFET:
        date_doc = contrat.date_effet - timedelta(days=alea.randint(20, 60))
    else:
        date_doc = date(2026, 6, 1) + timedelta(days=alea.randint(0, 115))

    verite: dict = {
        "type_document": type_document,
        "numero_adherent": numero_adherent,
        "nom": nom,
        "prenom": prenom,
        "numero_contrat": contrat.numero_contrat,
        "date_document": date_doc.isoformat(),
        "emetteur": None,
        "code_garantie": GARANTIE_PAR_TYPE.get(type_document),
        "montant_total_cents": None,
        "somme_lignes_cents": None,
        "anomalie_attendue": None,
    }
    lignes: list[tuple[str, int]] = []
    jour = date_doc.strftime("%d/%m/%Y")

    if type_document == "demande_remboursement":
        page.ligne("Cabinet Grossiste Delegataire - Service prestations", gras=True, taille=12)
        page.ligne("BP 40210, 34005 Montpellier Cedex 1")
        page.ln(4)
        page.ligne("DEMANDE DE REMBOURSEMENT DE FRAIS DE SANTE", gras=True, taille=14)
        page.ln(2)
        page.ligne(f"Adherent : {nom.upper()} {prenom}")
        page.ligne(f"Numero d'adherent : {numero_adherent}")
        page.ligne(f"Contrat : {contrat.numero_contrat}")
        page.ligne(f"Date des soins : {jour}")
        lignes = list(alea.sample(ACTES_SOINS, alea.randint(1, 3)))
        verite["emetteur"] = "Cabinet Grossiste Delegataire"
    elif type_document == "facture_optique":
        emetteur = alea.choice(OPTICIENS)
        raison, adresse = emetteur.split(", ", 1)
        page.ligne(raison.upper(), gras=True, taille=14)
        page.ligne(adresse)
        page.ln(4)
        page.ligne(f"FACTURE N° OPT-{date_doc.year}-{alea.randint(1000, 9999)}", gras=True, taille=12)
        page.ligne(f"Date : {jour}")
        page.ligne(f"Patient : {prenom} {nom.upper()}")
        page.ligne(f"Organisme complementaire : adherent {numero_adherent}, contrat {contrat.numero_contrat}")
        progressif = alea.random() < 0.4
        choix = [ACTES_OPTIQUE[0], ACTES_OPTIQUE[3], ACTES_OPTIQUE[4]] if progressif else ACTES_OPTIQUE[:3]
        lignes = [(libelle, alea.randrange(bas, haut, 100)) for libelle, (bas, haut) in choix]
        verite["emetteur"] = raison
    elif type_document == "facture_dentaire":
        emetteur = alea.choice(DENTISTES)
        praticien, adresse = emetteur.split(", chirurgien-dentiste, ")
        page.ligne(praticien, gras=True, taille=13)
        page.ligne(f"Chirurgien-dentiste, {adresse}")
        page.ln(4)
        page.ligne("NOTE D'HONORAIRES", gras=True, taille=12)
        page.ligne(f"Patient(e) : {nom.upper()} {prenom}, adherent n° {numero_adherent}")
        page.ligne(f"Contrat complementaire sante : {contrat.numero_contrat}")
        page.ligne(f"Soins realises le {jour}")
        for libelle, (bas, haut) in alea.sample(ACTES_DENTAIRES, alea.randint(1, 2)):
            lignes.append((libelle, bas if bas == haut else alea.randrange(bas, haut, 100)))
        verite["emetteur"] = praticien
    else:
        page.ligne(entreprise.raison_sociale.upper(), gras=True, taille=13)
        page.ligne(f"SIREN {entreprise.siren}")
        page.ln(6)
        page.ligne("ATTESTATION EMPLOYEUR", gras=True, taille=14)
        page.ln(2)
        civilite, pronom = ("Madame", "elle") if adherent.sexe == "F" else ("Monsieur", "il")
        page.ligne(
            f"Nous soussignes, {entreprise.raison_sociale}, attestons que {civilite} {prenom} {nom.upper()}, "
            f"ne(e) le {adherent.date_naissance.strftime('%d/%m/%Y')}, est salarie(e) de notre entreprise en "
            f"contrat a duree indeterminee depuis le {adherent.date_creation.strftime('%d/%m/%Y')}, et qu'a ce "
            f"titre {pronom} est affilie(e) au contrat collectif sante obligatoire, certificat d'adhesion "
            f"{contrat.numero_contrat}."
        )
        page.ligne(f"Fait a Montpellier, le {jour}, pour servir et valoir ce que de droit.")
        verite["emetteur"] = entreprise.raison_sociale
        verite["numero_adherent"] = None

    if lignes:
        somme = sum(m for _, m in lignes)
        total = somme + alea.choice([1000, -500, 2000]) if rang in TOTAL_FAUX else somme
        page.tableau(lignes, total)
        page.ln(4)
        page.ligne("Acquittee. Merci de votre confiance.")
        verite["montant_total_cents"] = total
        verite["somme_lignes_cents"] = somme

    for anomalie, rangs in (
        ("nom_altere", NOM_ALTERE),
        ("adherent_inexistant", ADHERENT_INEXISTANT),
        ("total_faux", TOTAL_FAUX),
        ("soins_avant_effet", SOINS_AVANT_EFFET),
    ):
        if rang in rangs:
            verite["anomalie_attendue"] = anomalie
    return page, verite


def genere_documents(session: Session, dossier: Path) -> list[dict]:
    """Ecrit les 30 PDF et `verite_terrain.json` dans `dossier`. Meme base, meme graine : memes fichiers."""
    alea = Random(GRAINE)
    reserves: dict = {t: _candidats_sante(session, g) for t, g in GARANTIE_PAR_TYPE.items()}
    reserves["affilies"] = _candidats_affilies(session)
    dossier.mkdir(parents=True, exist_ok=True)
    verites = []
    for rang, type_document in enumerate(REPARTITION):
        page, verite = _document(rang, type_document, alea, reserves)
        nom_fichier = f"doc-{rang + 1:02d}-{type_document}.pdf"
        page.output(str(dossier / nom_fichier))
        verites.append({"fichier": nom_fichier, **verite})
    (dossier / "verite_terrain.json").write_text(
        json.dumps(verites, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return verites
