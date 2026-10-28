"""Detection, archivage et chargement des fichiers recus. Les trois premieres taches du graphe.

Le decoupage en trois taches n'est pas decoratif : il correspond a trois responsabilites qui echouent
pour des raisons differentes, et qu'une reprise sur echec doit pouvoir reprendre separement.

1. **Detecter** inscrit au journal ce qui est arrive, avec son empreinte. A partir de la, le fichier est
   connu, meme si la suite echoue. Rejouer la detection sur un dossier inchange n'ajoute rien, parce que
   l'unicite du couple empreinte et nom de fichier l'interdit en base.
2. **Archiver** deplace le brut dans l'archive et verifie que l'empreinte n'a pas change pendant le
   deplacement. Un fichier archive n'est plus jamais modifie : c'est la piece d'audit.
3. **Charger** applique les controles, puis ecrit la couche brute **ou** prononce le rejet. Jamais les
   deux, et jamais une partie des lignes.

Sur ce dernier point, la garantie est structurelle et pas seulement intentionnelle. Les controles sont
executes en totalite **avant** la premiere ecriture : quand un lot est rejete, le code qui insere les
lignes n'est tout simplement pas atteint. Il n'y a donc pas d'ecriture a annuler. Chaque lot est en
outre traite dans sa propre transaction, de sorte que le rejet d'un lot ne touche pas les autres, et la
contrainte `ck_lot_fichier_rejet_sans_ecriture_partielle` refuse en base un lot rejete qui annoncerait
des lignes chargees.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from courtage.flux.controles import Anomalie, ResultatControles, applique_controles
from courtage.flux.enums import CodeAnomalie, FormatFichier, StatutLot, TypeFlux
from courtage.flux.fichiers import empreinte_fichier, format_depuis_extension, lit_fichier
from courtage.flux.modeles import (
    AnomalieLot,
    BrutCotisation,
    BrutEncaissement,
    BrutPrestation,
    LotFichier,
)
from courtage.flux.parametres import (
    NOM_DOSSIER_ARCHIVE,
    NOM_DOSSIER_ARRIVEE,
    NOM_DOSSIER_REJETS,
    racine_flux,
)
from courtage.flux.referentiel import ReferentielControles, charge_referentiel
from courtage.modeles.referentiel import Compagnie

#: Extensions traitees par la chaine. Tout autre fichier depose est ignore et signale.
EXTENSIONS_TRAITEES = (".csv", ".xlsx")

#: Emetteur du releve bancaire. Il n'est pas une compagnie, donc le lot n'a pas de compagnie.
CODE_EMETTEUR_BANQUE = "BANQUE"

#: Nombre de caracteres d'empreinte ajoutes a la reference du lot.
#:
#: La reference d'un lot est le nom du fichier suivi du debut de son empreinte. Le nom seul ne
#: suffit pas, et le defaut a ete constate en executant le graphe : une compagnie qui renvoie son
#: bordereau **corrige sous le meme nom** produit un contenu different, donc un nouveau lot, dont la
#: reference entrait en collision avec celle du premier. Suffixer par l'empreinte rend la reference
#: unique et surtout plus juste : elle designe le contenu recu, pas le nom du fichier. Le journal
#: montre alors les deux envois cote a cote, ce qui est exactement ce qu'un gestionnaire veut voir.
LONGUEUR_EMPREINTE_REFERENCE = 8


class DossierArriveeIntrouvable(FileNotFoundError):
    """Le dossier d'arrivee n'existe pas, donc la chaine ne peut rien detecter.

    Levee plutot que traitee comme un dossier vide : les deux se ressemblent dans un compte rendu, et
    seule la premiere situation demande une intervention.
    """


class NomFichierNonReconnu(ValueError):
    """Le nom du fichier ne respecte pas la convention de depot, donc son flux est indeterminable."""


@dataclass(slots=True)
class IdentiteFichier:
    """Ce que le nom du fichier annonce : son flux, son emetteur et la periode couverte."""

    type_flux: TypeFlux
    code_emetteur: str
    periode: date
    format_fichier: FormatFichier


def identifie(chemin: Path) -> IdentiteFichier:
    """Lit la convention de nom `FLUX_EMETTEUR_AAAAMM[suffixe].extension`.

    La convention est posee par le projet, pas devinee du contenu. Deviner le flux a partir des colonnes
    serait fragile : deux flux peuvent partager un en-tete apres une erreur d'export, et le fichier
    serait alors charge dans la mauvaise table sans que rien ne le signale.
    """
    format_fichier = format_depuis_extension(chemin)
    morceaux = chemin.stem.split("_")
    if len(morceaux) < 3:
        raise NomFichierNonReconnu(
            f"{chemin.name} : nom attendu FLUX_EMETTEUR_AAAAMM.extension, par exemple"
            " COTISATIONS_OCEANE_202607.csv"
        )
    try:
        type_flux = TypeFlux(morceaux[0].lower())
    except ValueError as erreur:
        flux_connus = ", ".join(flux.value.upper() for flux in TypeFlux)
        raise NomFichierNonReconnu(
            f"{chemin.name} : flux {morceaux[0]!r} inconnu, attendu l'un de {flux_connus}"
        ) from erreur
    periode_texte = morceaux[2][:6]
    try:
        periode = date(int(periode_texte[:4]), int(periode_texte[4:6]), 1)
    except ValueError as erreur:
        raise NomFichierNonReconnu(
            f"{chemin.name} : periode {periode_texte!r} illisible, attendu AAAAMM"
        ) from erreur
    return IdentiteFichier(
        type_flux=type_flux,
        code_emetteur=morceaux[1],
        periode=periode,
        format_fichier=format_fichier,
    )


def _nom_archive(lot: LotFichier) -> str:
    """Nom du fichier dans l'archive et dans les rejets : nom d'origine suffixe par l'empreinte.

    Le nom d'origine seul ne suffit pas, et le defaut est serieux. Une compagnie qui renvoie son
    bordereau **corrige sous le meme nom** produit un contenu different, donc un second lot. Avec un
    chemin d'archive tire du seul nom, le deplacement ecrasait le fichier archive du premier lot : son
    empreinte enregistree ne correspondait plus a rien, et la promesse « un fichier archive n'est plus
    jamais modifie » devenait fausse. Suffixer par l'empreinte rend chaque archive immuable.
    """
    chemin = Path(lot.nom_fichier)
    return f"{chemin.stem}-{lot.sha256[:LONGUEUR_EMPREINTE_REFERENCE]}{chemin.suffix}"


@dataclass(slots=True)
class CompteRenduEtape:
    """Ce qu'une etape a fait, pour le journal d'execution et pour la sortie de la commande."""

    etape: str
    traites: int = 0
    ignores: int = 0
    rejetes: int = 0
    lignes_chargees: int = 0
    details: list[dict[str, object]] = field(default_factory=list)

    def en_dictionnaire(self) -> dict[str, object]:
        return {
            "etape": self.etape,
            "lots_traites": self.traites,
            "lots_ignores": self.ignores,
            "lots_rejetes": self.rejetes,
            "lignes_chargees": self.lignes_chargees,
            "details": self.details,
        }


def dossier_arrivee() -> Path:
    return racine_flux() / NOM_DOSSIER_ARRIVEE


def dossier_archive() -> Path:
    return racine_flux() / NOM_DOSSIER_ARCHIVE


def dossier_rejets() -> Path:
    return racine_flux() / NOM_DOSSIER_REJETS


# --- Etape 1 : detection -------------------------------------------------------------------------


def detecte(session: Session, dossier: Path | None = None) -> CompteRenduEtape:
    """Inscrit au journal les fichiers presents dans le dossier d'arrivee.

    Les fichiers sont pris dans l'ordre alphabetique de leur nom, et cet ordre compte : un fichier
    redeposse sous un autre nom ne peut etre reconnu comme deja charge que si l'original a ete traite
    avant lui. La convention de nommage du generateur suffixe les copies et les corrections par un tiret
    bas, qui se trie apres le point de l'extension, donc l'original passe toujours en premier.
    """
    compte_rendu = CompteRenduEtape(etape="detection")
    source = dossier or dossier_arrivee()
    if not source.exists():
        # Une chaine qui tourne a vide tous les soirs sans rien dire est pire qu'une chaine en echec.
        # Un dossier d'arrivee absent est une erreur de configuration, le plus souvent une variable
        # `COURTAGE_FLUX_RACINE` oubliee dans le conteneur, et elle doit se voir.
        raise DossierArriveeIntrouvable(
            f"dossier d'arrivee introuvable : {source}."
            " Verifier la variable d'environnement COURTAGE_FLUX_RACINE."
        )

    codes_compagnies = {
        code: identifiant
        for identifiant, code in session.execute(select(Compagnie.id, Compagnie.code))
    }
    aujourdhui = date.today()

    for chemin in sorted(source.iterdir()):
        if not chemin.is_file():
            continue
        if chemin.suffix.lower() not in EXTENSIONS_TRAITEES:
            # Compte et nomme, et non ignore en silence : un fichier depose par une compagnie dans un
            # format inattendu doit apparaitre quelque part, sinon personne ne saura qu'il attend.
            compte_rendu.ignores += 1
            compte_rendu.details.append(
                {
                    "nom_fichier": chemin.name,
                    "statut": "extension_non_traitee",
                    "message": f"extensions traitees : {', '.join(EXTENSIONS_TRAITEES)}",
                }
            )
            continue
        try:
            identite = identifie(chemin)
        except (NomFichierNonReconnu, ValueError) as erreur:
            compte_rendu.ignores += 1
            compte_rendu.details.append(
                {"nom_fichier": chemin.name, "statut": "non_reconnu", "message": str(erreur)}
            )
            continue

        empreinte = empreinte_fichier(chemin)
        deja = session.scalars(
            select(LotFichier).where(
                LotFichier.sha256 == empreinte, LotFichier.nom_fichier == chemin.name
            )
        ).first()
        if deja is not None:
            compte_rendu.ignores += 1
            compte_rendu.details.append(
                {
                    "nom_fichier": chemin.name,
                    "reference_lot": deja.reference_lot,
                    "statut": "deja_connu",
                    "statut_lot": deja.statut.value,
                }
            )
            continue

        lot = LotFichier(
            reference_lot=f"{chemin.stem}-{empreinte[:LONGUEUR_EMPREINTE_REFERENCE]}",
            nom_fichier=chemin.name,
            type_flux=identite.type_flux,
            format_fichier=identite.format_fichier,
            sha256=empreinte,
            taille_octets=chemin.stat().st_size,
            compagnie_id=codes_compagnies.get(identite.code_emetteur),
            periode=identite.periode,
            date_depot=aujourdhui,
            statut=StatutLot.DETECTE,
        )
        session.add(lot)
        session.commit()
        compte_rendu.traites += 1
        compte_rendu.details.append(
            {
                "nom_fichier": chemin.name,
                "reference_lot": lot.reference_lot,
                "statut": "detecte",
                "type_flux": identite.type_flux.value,
                "periode": identite.periode.isoformat(),
                "sha256": empreinte,
            }
        )
    return compte_rendu


# --- Etape 2 : archivage -------------------------------------------------------------------------


def archive(session: Session) -> CompteRenduEtape:
    """Deplace chaque lot detecte dans l'archive, en verifiant que son empreinte n'a pas change.

    La verification apres deplacement n'est pas un exces de prudence : un fichier encore en cours
    d'ecriture par le depot au moment de la detection donnerait une empreinte differente apres, et
    charger un fichier tronque est precisement ce que la piste d'audit doit empecher.
    """
    compte_rendu = CompteRenduEtape(etape="archivage")
    lots = session.scalars(
        select(LotFichier).where(LotFichier.statut == StatutLot.DETECTE).order_by(LotFichier.id)
    ).all()
    for lot in lots:
        cible = dossier_archive() / f"{lot.periode:%Y%m}" / _nom_archive(lot)
        origine = dossier_arrivee() / lot.nom_fichier
        if cible.exists() and empreinte_fichier(cible) == lot.sha256:
            # Reprise apres un echec survenu entre le deplacement et la mise a jour du journal.
            # L'original n'est supprime que s'il est bien le fichier deja archive : un depot portant le
            # meme nom mais un autre contenu doit rester en arrivee pour etre detecte a son tour.
            if origine.exists() and empreinte_fichier(origine) == lot.sha256:
                origine.unlink()
        elif origine.exists():
            cible.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(origine), str(cible))
            empreinte_apres = empreinte_fichier(cible)
            if empreinte_apres != lot.sha256:
                lot.statut = StatutLot.REJETE
                lot.motif_rejet = (
                    f"empreinte differente apres archivage : {empreinte_apres} au lieu de {lot.sha256}."
                    " Le fichier a change entre la detection et l'archivage."
                )
                lot.chemin_archive = str(cible)
                lot.traite_le = datetime.now()
                session.commit()
                compte_rendu.rejetes += 1
                compte_rendu.details.append(
                    {"reference_lot": lot.reference_lot, "statut": "rejete", "message": lot.motif_rejet}
                )
                continue
        else:
            compte_rendu.ignores += 1
            compte_rendu.details.append(
                {
                    "reference_lot": lot.reference_lot,
                    "statut": "introuvable",
                    "message": f"ni dans {origine} ni dans {cible}",
                }
            )
            continue

        lot.chemin_archive = str(cible)
        lot.statut = StatutLot.ARCHIVE
        session.commit()
        compte_rendu.traites += 1
        compte_rendu.details.append(
            {
                "reference_lot": lot.reference_lot,
                "statut": "archive",
                "chemin_archive": str(cible),
                "sha256": lot.sha256,
            }
        )
    return compte_rendu


# --- Etape 3 : chargement ------------------------------------------------------------------------


def _enregistre_anomalies(session: Session, lot: LotFichier, anomalies: list[Anomalie]) -> None:
    """Enregistre les anomalies d'un lot, sans doublon de couple ligne, code et reference."""
    vues: set[tuple[int | None, str, str | None]] = set()
    for anomalie in anomalies:
        cle = (anomalie.ligne_numero, anomalie.code.value, anomalie.reference_ligne)
        if cle in vues:
            continue
        vues.add(cle)
        session.add(
            AnomalieLot(
                lot_id=lot.id,
                ligne_numero=anomalie.ligne_numero,
                reference_ligne=anomalie.reference_ligne,
                code=anomalie.code,
                severite=anomalie.severite,
                message=anomalie.message,
                valeur_constatee=anomalie.valeur_constatee,
                valeur_attendue=anomalie.valeur_attendue,
            )
        )


def _lignes_brutes(lot: LotFichier, resultat: ResultatControles) -> list[object]:
    """Transforme les lignes controlees en entites de la couche brute."""
    entites: list[object] = []
    for ligne in resultat.lignes:
        if resultat.type_flux is TypeFlux.COTISATIONS:
            entites.append(
                BrutCotisation(
                    lot_id=lot.id,
                    ligne_numero=ligne.numero,
                    reference_appel=ligne.texte("reference_appel"),
                    numero_contrat=ligne.texte("numero_contrat"),
                    numero_adherent=ligne.texte("numero_adherent"),
                    numero_police=ligne.texte("numero_police"),
                    nombre_affilies=ligne.valeurs.get("nombre_affilies"),
                    code_produit=ligne.texte("code_produit"),
                    periode_debut=ligne.jour("periode_debut"),
                    periode_fin=ligne.jour("periode_fin"),
                    date_echeance=ligne.jour("date_echeance"),
                    montant_ht_cents=ligne.entier("montant_ht"),
                    montant_taxe_cents=ligne.entier("montant_taxe"),
                    montant_total_cents=ligne.entier("montant_total"),
                )
            )
        elif resultat.type_flux is TypeFlux.ENCAISSEMENTS:
            entites.append(
                BrutEncaissement(
                    lot_id=lot.id,
                    ligne_numero=ligne.numero,
                    reference_bancaire=ligne.texte("reference_bancaire"),
                    reference_appel=ligne.texte("reference_appel"),
                    numero_contrat=ligne.texte("numero_contrat"),
                    numero_police=ligne.texte("numero_police"),
                    date_encaissement=ligne.jour("date_operation"),
                    montant_cents=ligne.entier("montant"),
                    moyen=ligne.texte("moyen"),
                    libelle=ligne.texte("libelle"),
                )
            )
        else:
            entites.append(
                BrutPrestation(
                    lot_id=lot.id,
                    ligne_numero=ligne.numero,
                    reference_prestation=ligne.texte("reference_prestation"),
                    numero_contrat=ligne.texte("numero_contrat"),
                    numero_adherent=ligne.texte("numero_adherent"),
                    code_garantie=ligne.texte("code_garantie"),
                    date_soins=ligne.jour("date_soins"),
                    montant_demande_cents=ligne.entier("montant_demande"),
                    montant_rembourse_cents=ligne.entier("montant_rembourse"),
                )
            )
    return entites


def _somme_principale(resultat: ResultatControles, declaree: bool) -> int | None:
    """Somme de la derniere colonne totalisee : montant total, montant encaisse, montant rembourse."""
    sommes = resultat.sommes_declarees if declaree else resultat.sommes_calculees
    if not sommes:
        return None
    from courtage.flux.fichiers import colonnes_totalisees

    colonnes = colonnes_totalisees(resultat.type_flux)
    return sommes.get(colonnes[-1])


def charge(session: Session, referentiel: ReferentielControles | None = None) -> CompteRenduEtape:
    """Applique les controles a chaque lot archive, puis charge la couche brute ou rejette le lot.

    Un lot, une transaction. Le rejet d'un bordereau n'empeche donc pas le chargement des autres, ce qui
    est le comportement attendu : un cabinet ne bloque pas son mois parce qu'une compagnie a envoye un
    fichier fautif.
    """
    compte_rendu = CompteRenduEtape(etape="chargement")
    vue = referentiel or charge_referentiel(session)
    # Par identifiant, donc dans l'ordre de detection, et non par nom de fichier. Le tri SQL depend de
    # la collation du moteur : sous une collation linguistique, PostgreSQL ignore la ponctuation au
    # premier niveau, et une copie suffixee passerait avant son original. C'est alors l'original qui
    # serait rejete comme « deja charge ». L'ordre d'insertion, lui, est celui du dossier d'arrivee.
    lots = session.scalars(
        select(LotFichier).where(LotFichier.statut == StatutLot.ARCHIVE).order_by(LotFichier.id)
    ).all()

    for lot in lots:
        chemin = Path(lot.chemin_archive) if lot.chemin_archive else None
        if chemin is None or not chemin.exists():
            compte_rendu.ignores += 1
            compte_rendu.details.append(
                {
                    "reference_lot": lot.reference_lot,
                    "statut": "introuvable",
                    "message": f"archive absente : {lot.chemin_archive}",
                }
            )
            continue

        contenu = lit_fichier(chemin, lot.type_flux)
        resultat = applique_controles(contenu, lot.sha256, lot.nom_fichier, vue)

        lot.nombre_lignes_fichier = len(contenu.lignes)
        lot.montant_total_declare_cents = _somme_principale(resultat, declaree=True)
        lot.montant_total_calcule_cents = _somme_principale(resultat, declaree=False)
        lot.traite_le = datetime.now()
        _enregistre_anomalies(session, lot, resultat.anomalies)

        if not resultat.bloquant and not resultat.lignes:
            # Un fichier reduit a son en-tete et a sa ligne de total passe tous les controles. Le
            # charger comme un succes ferait compter le mois comme servi : un releve bancaire vide
            # marquerait la periode comme traitee, et tous les appels du mois seraient ensuite ecartes
            # faute d'encaissement, sans que personne ne sache pourquoi. Le lot est donc rejete.
            resultat.anomalies.append(
                Anomalie(
                    code=CodeAnomalie.SCHEMA_INVALIDE,
                    ligne_numero=None,
                    reference_ligne=None,
                    message="fichier sans aucune ligne de donnee : en-tete et total seulement",
                )
            )
            _enregistre_anomalies(session, lot, resultat.anomalies[-1:])

        if resultat.bloquant:
            # Aucune ligne n'est inseree : le code qui insere n'est pas atteint.
            lot.statut = StatutLot.REJETE
            lot.motif_rejet = resultat.motif_rejet()
            lot.nombre_lignes_chargees = 0
            session.commit()
            copie = dossier_rejets() / _nom_archive(lot)
            copie.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(chemin, copie)
            compte_rendu.rejetes += 1
            compte_rendu.details.append(
                {
                    "reference_lot": lot.reference_lot,
                    "statut": "rejete",
                    "lignes_fichier": lot.nombre_lignes_fichier,
                    "motif": lot.motif_rejet,
                    "anomalies": [anomalie.code.value for anomalie in resultat.anomalies_bloquantes],
                }
            )
            continue

        try:
            for entite in _lignes_brutes(lot, resultat):
                session.add(entite)
            lot.statut = StatutLot.CHARGE
            lot.nombre_lignes_chargees = len(resultat.lignes)
            session.commit()
        except SQLAlchemyError as erreur:
            # Dernier filet. Les controles reprennent les invariants du schema, mais une contrainte
            # ajoutee a la base et pas encore refletee ici ferait echouer l'ecriture. Sans ce
            # confinement, l'exception interrompt la boucle et **aucun lot suivant** n'est traite, ce
            # qui contredit la promesse « un lot, une transaction ». Le lot est donc rejete avec un
            # motif technique, et la chaine continue.
            session.rollback()
            lot_recharge = session.get(LotFichier, lot.id)
            if lot_recharge is not None:
                lot_recharge.statut = StatutLot.REJETE
                lot_recharge.nombre_lignes_chargees = 0
                lot_recharge.motif_rejet = (
                    "lot rejete sur refus de la base a l'ecriture : "
                    f"{type(erreur).__name__} : {str(erreur)[:300]}"
                )
                lot_recharge.traite_le = datetime.now()
                session.commit()
            compte_rendu.rejetes += 1
            compte_rendu.details.append(
                {
                    "reference_lot": lot.reference_lot,
                    "statut": "rejete",
                    "motif": "refus de la base a l'ecriture",
                    "erreur": type(erreur).__name__,
                }
            )
            continue
        vue.empreintes_chargees[lot.sha256] = lot.nom_fichier
        compte_rendu.traites += 1
        compte_rendu.lignes_chargees += lot.nombre_lignes_chargees
        compte_rendu.details.append(
            {
                "reference_lot": lot.reference_lot,
                "statut": "charge",
                "lignes_chargees": lot.nombre_lignes_chargees,
                "avertissements": len(
                    [anomalie for anomalie in resultat.anomalies if not anomalie.bloquante]
                ),
            }
        )
    return compte_rendu
