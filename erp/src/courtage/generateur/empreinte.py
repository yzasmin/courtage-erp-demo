"""Empreinte SHA-256 du jeu de donnees genere, qui prouve la reproductibilite.

Comment la preuve fonctionne. La generation est relancee avec la meme graine, sur une base vide, et
l'empreinte est recalculee. Si les deux empreintes coincident, alors chaque ligne de chaque table est
identique, a l'octet pres. C'est une affirmation beaucoup plus forte que « le meme nombre de lignes ».

Deux precautions rendent l'empreinte fiable.

**Les lignes sont triees avant d'etre hachees.** L'ordre dans lequel une base restitue des lignes n'est
pas garanti, et il differe entre PostgreSQL et SQLite. Sans tri, deux bases au contenu identique
donneraient deux empreintes differentes, et la preuve ne vaudrait rien.

**Les colonnes sont enumerees explicitement, table par table.** Les colonnes techniques d'horodatage sont
incluses, parce que le generateur les derive des dates simulees et non de l'horloge : elles sont donc
elles aussi reproductibles. Enumerer plutot que de prendre toutes les colonnes evite qu'un ajout de
colonne futur change silencieusement l'empreinte sans que personne le decide.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from courtage.modeles import (
    ActeDelegue,
    Adherent,
    AppelCotisation,
    Apporteur,
    Avenant,
    Branche,
    Compagnie,
    Contrat,
    ConventionDelegation,
    ConventionProduit,
    Encaissement,
    EntrepriseSouscriptrice,
    Formule,
    FormuleGarantie,
    Garantie,
    Prestation,
    Produit,
    TauxCommission,
    TauxRetrocession,
    TauxTaxe,
)

#: Colonnes retenues pour l'empreinte, table par table, dans un ordre fixe.
#: Les cles primaires techniques sont incluses : le generateur les attribue lui-meme, de facon
#: deterministe, donc elles font partie du resultat a reproduire.
COLONNES_EMPREINTE: tuple[tuple[type, tuple[str, ...]], ...] = (
    (Branche, ("id", "code", "libelle", "famille")),
    (Compagnie, ("id", "code", "raison_sociale", "siren", "actif")),
    (Apporteur, ("id", "code", "nom", "type_apporteur", "numero_orias", "departement", "date_entree",
                 "date_sortie", "actif")),
    (Produit, ("id", "code", "libelle", "branche_id", "compagnie_id", "date_ouverture", "date_fermeture")),
    (Garantie, ("id", "code", "libelle", "famille", "rang")),
    (Formule, ("id", "produit_id", "code", "libelle", "niveau", "tarif_base_mensuel_cents")),
    (FormuleGarantie, ("id", "formule_id", "garantie_id", "libelle_couverture", "taux_remboursement_bp",
                       "plafond_annuel_cents")),
    (TauxTaxe, ("id", "branche_id", "libelle", "taux_bp", "date_debut_validite", "date_fin_validite")),
    (TauxCommission, ("id", "produit_id", "taux_bp", "assiette", "date_debut_validite", "date_fin_validite")),
    (TauxRetrocession, ("id", "apporteur_id", "produit_id", "taux_bp", "date_debut_validite",
                        "date_fin_validite")),
    (ConventionDelegation, ("id", "reference", "compagnie_id", "portee", "date_debut_validite",
                            "date_fin_validite", "commentaire")),
    (ConventionProduit, ("id", "convention_id", "produit_id")),
    (ActeDelegue, ("id", "convention_id", "acte")),
    (EntrepriseSouscriptrice, ("id", "siren", "raison_sociale", "code_naf", "effectif", "departement",
                               "date_adhesion")),
    (Adherent, ("id", "numero_adherent", "nom", "prenom", "date_naissance", "sexe", "code_postal",
                "departement", "courriel", "telephone", "entreprise_id", "date_creation", "cree_le")),
    (Contrat, ("id", "numero_contrat", "adherent_id", "entreprise_id", "produit_id", "statut",
               "date_souscription", "date_effet", "date_resiliation", "motif_resiliation", "formule_id",
               "apporteur_id", "cotisation_mensuelle_cents", "fractionnement", "cree_le", "maj_le")),
    (Avenant, ("id", "contrat_id", "numero_ordre", "type_avenant", "date_effet", "date_saisie", "statut",
               "formule_id", "apporteur_id", "cotisation_mensuelle_cents", "fractionnement",
               "motif_resiliation", "commentaire")),
    (AppelCotisation, ("id", "reference_appel", "contrat_id", "periode_debut", "periode_fin",
                       "date_echeance", "montant_ht_cents", "montant_taxe_cents", "montant_total_cents",
                       "taux_taxe_bp", "statut", "bordereau_id")),
    (Encaissement, ("id", "reference_bancaire", "appel_id", "contrat_id", "date_encaissement",
                    "montant_cents", "moyen", "statut_rapprochement", "libelle_releve")),
    (Prestation, ("id", "reference_prestation", "contrat_id", "adherent_id", "garantie_id", "date_soins",
                  "date_reglement", "montant_demande_cents", "montant_rembourse_cents", "statut",
                  "motif_rejet", "bordereau_id")),
)

#: Nombre de lignes lues par aller-retour. L'empreinte porte sur des centaines de milliers de lignes, et
#: les charger toutes en memoire ne tiendrait pas sur ce poste.
TAILLE_PAGE = 5_000


def _canonise(valeur: object) -> object:
    """Rend une valeur comparable et serialisable, independamment du moteur.

    PostgreSQL et SQLite ne restituent pas les dates et les booleens dans le meme type Python. Sans
    cette normalisation, deux bases au contenu identique donneraient deux empreintes differentes.
    """
    if valeur is None:
        return None
    if isinstance(valeur, bool):
        return int(valeur)
    if isinstance(valeur, datetime):
        return valeur.replace(microsecond=0).isoformat(sep=" ")
    if isinstance(valeur, date):
        return valeur.isoformat()
    if isinstance(valeur, Decimal):
        return str(valeur)
    if isinstance(valeur, int | str):
        return valeur
    return str(valeur)


def empreinte_table(session: Session, modele: type, colonnes: tuple[str, ...]) -> tuple[int, str]:
    """Nombre de lignes et empreinte SHA-256 d'une table, lues par pages et triees par identifiant."""
    hacheur = hashlib.sha256()
    nombre = 0
    attributs = [getattr(modele, nom) for nom in colonnes]
    decalage = 0
    while True:
        lignes = session.execute(
            select(*attributs).order_by(modele.id).offset(decalage).limit(TAILLE_PAGE)
        ).all()
        if not lignes:
            break
        for ligne in lignes:
            # Serialisation canonique : separateurs fixes, pas d'espace, pas de reordonnancement.
            charge = json.dumps([_canonise(v) for v in ligne], ensure_ascii=True, separators=(",", ":"))
            hacheur.update(charge.encode("utf-8"))
            hacheur.update(b"\n")
            nombre += 1
        decalage += TAILLE_PAGE
    return nombre, hacheur.hexdigest()


def empreinte_complete(session: Session) -> dict:
    """Empreinte de tout le jeu de donnees, table par table, plus une empreinte globale.

    L'empreinte globale est le hachage de la liste ordonnee des empreintes de table. Deux jeux de
    donnees ont donc la meme empreinte globale si et seulement si toutes leurs tables coincident.
    """
    tables: dict[str, dict[str, object]] = {}
    hacheur_global = hashlib.sha256()
    total_lignes = 0

    for modele, colonnes in COLONNES_EMPREINTE:
        nom_table = modele.__tablename__
        nombre, empreinte = empreinte_table(session, modele, colonnes)
        tables[nom_table] = {"lignes": nombre, "sha256": empreinte}
        total_lignes += nombre
        hacheur_global.update(f"{nom_table}:{nombre}:{empreinte}\n".encode())

    return {
        "total_lignes": total_lignes,
        "sha256_global": hacheur_global.hexdigest(),
        "tables": tables,
    }
