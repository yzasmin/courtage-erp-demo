"""Format des fichiers recus : schema des colonnes, ecriture, lecture, empreinte.

Un seul module tient le format, et c'est deliberé : le generateur ecrit avec ces fonctions et la chaine
lit avec les memes. Un separateur ou un format de date modifie d'un cote se repercute de l'autre, donc
le generateur ne peut pas produire un fichier que le lecteur ne sait pas relire. L'erreur inverse,
deux definitions du format a deux endroits, est celle qui fait passer des tests verts sur des fichiers
que la production refuse.

Choix de format, et ils imitent ce qu'un cabinet recoit reellement :

- separateur point-virgule, parce que la virgule sert aux decimales en France ;
- dates au format jour, mois, annee, sur quatre chiffres pour l'annee ;
- montants en euros avec virgule decimale, et espaces de milliers tolerees a la lecture ;
- une ligne de total en fin de fichier, dont la premiere cellule porte le marqueur et la deuxieme le
  nombre de lignes annonce.

Le XLSX ecrit ses dates comme de vraies dates de tableur et ses montants comme du texte au format
francais. Ce n'est pas une facilite : c'est ce que produisent la plupart des exports de gestion, et
c'est le cas que le lecteur doit savoir traiter, car une date lue comme un `datetime` et un montant lu
comme une chaine arrivent bien par le meme fichier.
"""

from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from courtage.flux.enums import FormatFichier, TypeFlux
from courtage.flux.parametres import (
    ENCODAGE_CSV,
    FORMAT_DATE_FICHIER,
    MARQUEUR_TOTAL,
    SEPARATEUR_CSV,
)

#: Espaces susceptibles de separer les milliers dans un fichier recu : espace ordinaire, insecable,
#: insecable fine. Les trois existent dans les exports reels, et aucune ne doit faire echouer la lecture.
ESPACES_MILLIERS = (" ", " ", " ")

#: Forme acceptee d'un montant, une fois les espaces retires et la virgule ramenee au point.
#: Deux decimales au plus, signe facultatif, et rien d'autre : ni notation exponentielle, ni separateur
#: de lisibilite, ni `Infinity`, que `Decimal` accepterait pourtant volontiers.
MOTIF_MONTANT = re.compile(r"[+-]?\d+(\.\d{1,2})?")

#: Borne de vraisemblance d'un montant, en centimes : cent millions d'euros. Au-dela, la valeur ne
#: vient pas d'un bordereau de cotisations, elle vient d'une erreur d'export.
MONTANT_MAXIMAL_CENTS = 10_000_000_000

#: Taille des blocs de lecture pour le calcul d'empreinte. Un fichier recu ne tient pas forcement en
#: memoire, et rien n'oblige a l'y charger pour le hacher.
TAILLE_BLOC_EMPREINTE = 1 << 16


class ValeurIllisible(ValueError):
    """Une cellule ne se convertit pas dans le type attendu par le schema.

    Levee par les convertisseurs, rattrapee par les controles, qui en font une anomalie de schema au
    lieu de laisser remonter une exception technique.
    """


@dataclass(frozen=True, slots=True)
class Colonne:
    """Une colonne du schema d'un flux.

    `type_valeur` vaut `texte`, `date`, `montant` ou `entier`. `obligatoire` dit si une cellule vide est
    une erreur : une reference bancaire vide n'a pas de sens, une reference d'appel vide est au contraire
    le cas normal d'un virement saisi a la main, et un numero de police vide est le cas normal d'une
    cotisation individuelle.
    """

    nom: str
    type_valeur: str
    obligatoire: bool = True
    #: Vrai si la colonne entre dans la ligne de total.
    totalisee: bool = False


#: Schema de chaque flux. L'ordre des colonnes est l'ordre du fichier.
SCHEMAS: dict[TypeFlux, tuple[Colonne, ...]] = {
    TypeFlux.COTISATIONS: (
        Colonne("reference_appel", "texte"),
        # Un appel individuel porte le contrat et l'adherent, un appel groupe porte la police et le
        # nombre d'affilies couverts. Exactement l'un des deux porteurs est renseigne, et le controle
        # de schema le verifie : un bordereau qui porterait les deux ne designerait plus de payeur.
        Colonne("numero_contrat", "texte", obligatoire=False),
        Colonne("numero_adherent", "texte", obligatoire=False),
        Colonne("numero_police", "texte", obligatoire=False),
        Colonne("nombre_affilies", "entier", obligatoire=False),
        Colonne("code_produit", "texte"),
        Colonne("periode_debut", "date"),
        Colonne("periode_fin", "date"),
        Colonne("date_echeance", "date"),
        Colonne("montant_ht", "montant", totalisee=True),
        Colonne("montant_taxe", "montant", totalisee=True),
        Colonne("montant_total", "montant", totalisee=True),
    ),
    TypeFlux.ENCAISSEMENTS: (
        Colonne("reference_bancaire", "texte"),
        Colonne("date_operation", "date"),
        Colonne("libelle", "texte", obligatoire=False),
        Colonne("reference_appel", "texte", obligatoire=False),
        Colonne("numero_contrat", "texte", obligatoire=False),
        Colonne("numero_police", "texte", obligatoire=False),
        Colonne("moyen", "texte"),
        Colonne("montant", "montant", totalisee=True),
    ),
    TypeFlux.PRESTATIONS: (
        Colonne("reference_prestation", "texte"),
        Colonne("numero_contrat", "texte"),
        Colonne("numero_adherent", "texte"),
        Colonne("code_garantie", "texte"),
        Colonne("date_soins", "date"),
        Colonne("montant_demande", "montant", totalisee=True),
        Colonne("montant_rembourse", "montant", totalisee=True),
    ),
}

#: Cle metier de chaque flux, celle sur laquelle porte le controle de doublon.
CLE_METIER: dict[TypeFlux, str] = {
    TypeFlux.COTISATIONS: "reference_appel",
    TypeFlux.ENCAISSEMENTS: "reference_bancaire",
    TypeFlux.PRESTATIONS: "reference_prestation",
}


def noms_colonnes(type_flux: TypeFlux) -> list[str]:
    return [colonne.nom for colonne in SCHEMAS[type_flux]]


def colonnes_totalisees(type_flux: TypeFlux) -> list[str]:
    return [colonne.nom for colonne in SCHEMAS[type_flux] if colonne.totalisee]


# --- Mise en forme, pour l'ecriture --------------------------------------------------------------


def formate_date(valeur: date) -> str:
    return valeur.strftime(FORMAT_DATE_FICHIER)


def formate_montant(montant_cents: int) -> str:
    """Rend un montant en centimes au format francais d'un fichier recu : `123456` donne `1234,56`.

    Pas de separateur de milliers a l'ecriture : il est tolere a la lecture, mais en produire
    compliquerait le fichier sans rien demontrer.
    """
    signe = "-" if montant_cents < 0 else ""
    entier, centiemes = divmod(abs(montant_cents), 100)
    return f"{signe}{entier},{centiemes:02d}"


# --- Conversion, pour la lecture -----------------------------------------------------------------


def convertit_date(brut: str | date | datetime) -> date:
    """Convertit une cellule en date. Accepte le format francais du fichier et les dates du tableur."""
    if isinstance(brut, datetime):
        return brut.date()
    if isinstance(brut, date):
        return brut
    texte = str(brut).strip()
    if not texte:
        raise ValeurIllisible("date vide")
    try:
        return datetime.strptime(texte, FORMAT_DATE_FICHIER).date()
    except ValueError as erreur:
        raise ValeurIllisible(f"date illisible : {texte!r}, format attendu JJ/MM/AAAA") from erreur


def convertit_montant(brut: str | int | float | Decimal) -> int:
    """Convertit une cellule en centimes entiers.

    Le passage par `Decimal`, y compris quand la cellule arrive en nombre du tableur, est voulu : c'est
    le seul endroit du projet ou une valeur exterieure pourrait introduire un flottant, et elle est
    convertie ici, une fois, sur sa representation textuelle.

    La forme acceptee est volontairement etroite, et la premiere version ne l'etait pas assez. `Decimal`
    accepte en effet bien plus qu'un montant : `Infinity` et `sNaN` levaient une exception technique
    **hors** du bloc protege, donc un lot entier faisait planter la chaine au lieu d'etre rejete ;
    `1e400` donnait un entier de quatre cents chiffres, qui depasse l'entier de la base ; `1_000` et
    `1e3` passaient pour mille euros. Aucune de ces ecritures n'apparait dans un bordereau d'assurance.
    Le motif ci-dessous n'accepte donc qu'un nombre decimal a deux decimales au plus, signe compris.
    """
    texte = str(brut).strip()
    for espace in ESPACES_MILLIERS:
        texte = texte.replace(espace, "")
    texte = texte.replace(",", ".")
    if not texte:
        raise ValeurIllisible("montant vide")
    if not MOTIF_MONTANT.fullmatch(texte):
        raise ValeurIllisible(f"montant illisible : {brut!r}, attendu un nombre a deux decimales au plus")
    try:
        valeur = Decimal(texte)
        centimes = (valeur * 100).to_integral_value()
    except (InvalidOperation, OverflowError, ValueError) as erreur:
        raise ValeurIllisible(f"montant illisible : {brut!r}") from erreur
    if abs(centimes) > MONTANT_MAXIMAL_CENTS:
        raise ValeurIllisible(f"montant hors de toute vraisemblance : {brut!r}")
    return int(centimes)


def convertit_entier(brut: str | int) -> int:
    """Convertit une cellule en entier. Sert au nombre d'affilies d'un appel groupe."""
    texte = str(brut).strip()
    for espace in ESPACES_MILLIERS:
        texte = texte.replace(espace, "")
    try:
        return int(texte)
    except ValueError as erreur:
        raise ValeurIllisible(f"entier illisible : {brut!r}") from erreur


def convertit(colonne: Colonne, brut: object) -> str | date | int | None:
    """Convertit une cellule selon le type declare par le schema. Rend `None` pour une cellule vide."""
    if brut is None:
        texte_vide = True
    else:
        texte_vide = str(brut).strip() == ""
    if texte_vide:
        if colonne.obligatoire:
            raise ValeurIllisible(f"colonne obligatoire vide : {colonne.nom}")
        return None
    if colonne.type_valeur == "date":
        return convertit_date(brut)  # type: ignore[arg-type]
    if colonne.type_valeur == "montant":
        return convertit_montant(brut)  # type: ignore[arg-type]
    if colonne.type_valeur == "entier":
        return convertit_entier(brut)  # type: ignore[arg-type]
    return str(brut).strip()


# --- Contenu lu ----------------------------------------------------------------------------------


@dataclass(slots=True)
class LigneLue:
    """Une ligne de donnees, telle qu'elle a ete lue, avant toute conversion.

    Les valeurs restent des chaines, ou des dates quand le tableur en a fourni. La conversion est faite
    par les controles, parce qu'une conversion qui echoue **est** un resultat de controle : la decrire
    comme une exception a ce niveau ferait perdre le numero de ligne et la reference.
    """

    numero: int
    valeurs: dict[str, object]

    def cle(self, type_flux: TypeFlux) -> str:
        valeur = self.valeurs.get(CLE_METIER[type_flux])
        return "" if valeur is None else str(valeur).strip()


@dataclass(slots=True)
class ContenuFichier:
    """Resultat de la lecture d'un fichier recu, sans aucun jugement sur sa validite."""

    type_flux: TypeFlux
    format_fichier: FormatFichier
    colonnes: list[str]
    lignes: list[LigneLue] = field(default_factory=list)
    #: Cellules de la ligne de total, par nom de colonne. Absente si le fichier n'en porte pas.
    total_declare: dict[str, object] | None = None
    #: Nombre de lignes annonce par la deuxieme cellule de la ligne de total.
    nombre_lignes_declare: str | None = None
    #: Defauts de structure constates a la lecture : en-tete different du schema, ligne de total absente,
    #: nombre de cellules different du nombre de colonnes. Ils donnent des anomalies de schema.
    defauts_structure: list[str] = field(default_factory=list)


def empreinte_fichier(chemin: Path) -> str:
    """Empreinte SHA-256 du fichier, calculee par blocs."""
    condensat = hashlib.sha256()
    with chemin.open("rb") as flux:
        for bloc in iter(lambda: flux.read(TAILLE_BLOC_EMPREINTE), b""):
            condensat.update(bloc)
    return condensat.hexdigest()


def format_depuis_extension(chemin: Path) -> FormatFichier:
    suffixe = chemin.suffix.lower()
    if suffixe == ".csv":
        return FormatFichier.CSV
    if suffixe == ".xlsx":
        return FormatFichier.XLSX
    raise ValueError(f"extension de fichier non prise en charge : {chemin.name}")


# --- Ecriture ------------------------------------------------------------------------------------


def _ligne_total(
    type_flux: TypeFlux, lignes: list[dict[str, object]], totaux_forces: dict[str, int] | None
) -> list[str]:
    """Construit la ligne de total : marqueur, nombre de lignes, puis les sommes dans leurs colonnes."""
    colonnes = SCHEMAS[type_flux]
    sommes: dict[str, int] = {}
    for nom in colonnes_totalisees(type_flux):
        if totaux_forces and nom in totaux_forces:
            sommes[nom] = totaux_forces[nom]
        else:
            # Une cellule volontairement abimee n'est pas sommable. L'emetteur dont une formule a
            # echoue produit exactement cela : un total qui ne compte que les lignes lisibles. La ligne
            # illisible sera signalee par le controle de schema, pas par celui du total.
            sommes[nom] = sum(
                int(ligne[nom]) for ligne in lignes if isinstance(ligne.get(nom), int)
            )
    cellules: list[str] = []
    for rang, colonne in enumerate(colonnes):
        if rang == 0:
            cellules.append(MARQUEUR_TOTAL)
        elif rang == 1:
            cellules.append(str(len(lignes)))
        elif colonne.nom in sommes:
            cellules.append(formate_montant(sommes[colonne.nom]))
        else:
            cellules.append("")
    return cellules


def _cellules_de_ligne(type_flux: TypeFlux, ligne: dict[str, object]) -> list[str]:
    """Rend une ligne de donnees sous forme de cellules textuelles, selon le type de chaque colonne."""
    cellules: list[str] = []
    for colonne in SCHEMAS[type_flux]:
        valeur = ligne.get(colonne.nom)
        if valeur is None:
            cellules.append("")
        elif isinstance(valeur, str):
            # Valeur deja mise en forme, ou valeur abimee volontairement par l'injection d'anomalie.
            cellules.append(valeur)
        elif colonne.type_valeur == "date":
            cellules.append(formate_date(valeur))  # type: ignore[arg-type]
        elif colonne.type_valeur == "montant":
            cellules.append(formate_montant(int(valeur)))
        else:
            cellules.append(str(valeur))
    return cellules


def ecrit_csv(
    chemin: Path,
    type_flux: TypeFlux,
    lignes: list[dict[str, object]],
    totaux_forces: dict[str, int] | None = None,
) -> None:
    """Ecrit un fichier CSV : en-tete, lignes de donnees, ligne de total."""
    chemin.parent.mkdir(parents=True, exist_ok=True)
    # `newline=""` est obligatoire avec le module csv, sinon Windows double les fins de ligne.
    with chemin.open("w", encoding=ENCODAGE_CSV, newline="") as flux:
        graveur = csv.writer(flux, delimiter=SEPARATEUR_CSV, lineterminator="\r\n")
        graveur.writerow(noms_colonnes(type_flux))
        for ligne in lignes:
            graveur.writerow(_cellules_de_ligne(type_flux, ligne))
        graveur.writerow(_ligne_total(type_flux, lignes, totaux_forces))


def ecrit_xlsx(
    chemin: Path,
    type_flux: TypeFlux,
    lignes: list[dict[str, object]],
    totaux_forces: dict[str, int] | None = None,
) -> None:
    """Ecrit un fichier XLSX : dates en vraies dates de tableur, montants en texte au format francais."""
    from openpyxl import Workbook

    chemin.parent.mkdir(parents=True, exist_ok=True)
    classeur = Workbook()
    feuille = classeur.active
    feuille.title = type_flux.value[:31]
    feuille.append(noms_colonnes(type_flux))
    for ligne in lignes:
        textes = _cellules_de_ligne(type_flux, ligne)
        cellules: list[object] = []
        for rang, colonne in enumerate(SCHEMAS[type_flux]):
            valeur = ligne.get(colonne.nom)
            # Les dates partent en vraies dates de tableur, tout le reste en texte. Une valeur de date
            # abimee volontairement reste une chaine, et doit donc rester du texte : sinon l'anomalie de
            # schema disparaitrait a l'ecriture.
            if colonne.type_valeur == "date" and isinstance(valeur, date):
                cellules.append(valeur)
            else:
                cellules.append(textes[rang])
        feuille.append(cellules)
    feuille.append(_ligne_total(type_flux, lignes, totaux_forces))
    classeur.save(chemin)


# --- Lecture -------------------------------------------------------------------------------------


def _assemble(type_flux: TypeFlux, format_fichier: FormatFichier, rangees: list[list[object]]) -> ContenuFichier:
    """Transforme des rangees brutes en contenu structure, en notant les defauts de structure.

    Aucun jugement metier ici. Le role de cette fonction est de dire ce que le fichier contient, y
    compris quand il est mal forme, pour que les controles disposent de tout le fichier et pas seulement
    de sa premiere erreur.
    """
    attendues = noms_colonnes(type_flux)
    contenu = ContenuFichier(
        type_flux=type_flux, format_fichier=format_fichier, colonnes=list(attendues)
    )
    if not rangees:
        contenu.defauts_structure.append("fichier vide : aucune ligne, pas meme l'en-tete")
        return contenu

    entete = [str(cellule).strip() if cellule is not None else "" for cellule in rangees[0]]
    if entete != attendues:
        contenu.defauts_structure.append(
            f"en-tete different du schema attendu. Attendu {attendues}, lu {entete}"
        )

    corps = rangees[1:]
    # Les lignes entierement vides de la fin sont retirees avant de chercher le total. Un CSV termine
    # par deux fins de ligne, ou une feuille de tableur dont les dernieres lignes sont formatees mais
    # vides, produisent une derniere rangee vide : sans ce nettoyage, le fichier serait rejete pour
    # « ligne de total absente » alors qu'elle est la, juste au-dessus.
    while corps and all(cellule is None or str(cellule).strip() == "" for cellule in corps[-1]):
        corps.pop()
    # La ligne de total est la derniere ligne dont la premiere cellule porte le marqueur.
    if corps and str(corps[-1][0] if corps[-1] else "").strip().upper() == MARQUEUR_TOTAL:
        rangee_total = corps.pop()
        contenu.total_declare = {
            nom: (rangee_total[rang] if rang < len(rangee_total) else None)
            for rang, nom in enumerate(attendues)
        }
        deuxieme = rangee_total[1] if len(rangee_total) > 1 else None
        contenu.nombre_lignes_declare = None if deuxieme is None else str(deuxieme).strip()
    else:
        contenu.defauts_structure.append("ligne de total absente du fichier")

    for rang, rangee in enumerate(corps, start=1):
        if all(cellule is None or str(cellule).strip() == "" for cellule in rangee):
            continue
        if len(rangee) != len(attendues):
            contenu.defauts_structure.append(
                f"ligne {rang} : {len(rangee)} cellules au lieu de {len(attendues)}"
            )
        valeurs: dict[str, object] = {}
        for position, nom in enumerate(attendues):
            valeurs[nom] = rangee[position] if position < len(rangee) else None
        contenu.lignes.append(LigneLue(numero=rang, valeurs=valeurs))
    return contenu


def lit_csv(chemin: Path, type_flux: TypeFlux) -> ContenuFichier:
    with chemin.open("r", encoding=ENCODAGE_CSV, newline="") as flux:
        rangees = [list(rangee) for rangee in csv.reader(flux, delimiter=SEPARATEUR_CSV)]
    return _assemble(type_flux, FormatFichier.CSV, rangees)  # type: ignore[arg-type]


def lit_xlsx(chemin: Path, type_flux: TypeFlux) -> ContenuFichier:
    from openpyxl import load_workbook

    classeur = load_workbook(chemin, read_only=True, data_only=True)
    try:
        noms_feuilles = list(classeur.sheetnames)
        feuille = classeur[noms_feuilles[0]]
        rangees = [list(rangee) for rangee in feuille.iter_rows(values_only=True)]
    finally:
        classeur.close()
    contenu = _assemble(type_flux, FormatFichier.XLSX, rangees)
    if len(noms_feuilles) > 1:
        # Seule la premiere feuille est lue. Le signaler plutot que de perdre les autres en silence :
        # un classeur a plusieurs onglets est soit un export mal configure, soit un bordereau dont une
        # partie des lignes n'arriverait jamais en base.
        contenu.defauts_structure.append(
            f"classeur a {len(noms_feuilles)} feuilles, seule la premiere est lue : {noms_feuilles}"
        )
    return contenu


def lit_fichier(chemin: Path, type_flux: TypeFlux) -> ContenuFichier:
    """Lit un fichier recu, quel que soit son format, sans jamais laisser passer d'exception technique.

    Un CSV encode en Latin-1, un XLSX tronque a la transmission, un octet nul au milieu d'une ligne :
    ces fichiers existent, et ils arrivent. La premiere version laissait remonter l'exception du
    lecteur, ce qui interrompait le traitement **de tous les lots suivants** au lieu de rejeter celui-ci.
    L'echec de lecture devient donc un defaut de structure, que les controles transforment en anomalie
    de schema bloquante, et le lot est rejete proprement.
    """
    format_fichier = format_depuis_extension(chemin)
    try:
        if format_fichier is FormatFichier.CSV:
            return lit_csv(chemin, type_flux)
        return lit_xlsx(chemin, type_flux)
    except (UnicodeDecodeError, csv.Error, OSError, ValueError, KeyError) as erreur:
        contenu = ContenuFichier(
            type_flux=type_flux, format_fichier=format_fichier, colonnes=noms_colonnes(type_flux)
        )
        contenu.defauts_structure.append(
            f"fichier illisible : {type(erreur).__name__} : {erreur}"
        )
        return contenu
