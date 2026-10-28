"""Controles de qualite des fichiers recus : les sept bloquants et les deux non bloquants.

Deux principes gouvernent ce module, et ils expliquent sa forme.

**Tous les controles s'executent, puis la decision est prise.** S'arreter a la premiere erreur
bloquante serait plus rapide et serait une faute : le gestionnaire renvoie le rapport d'anomalies a
l'emetteur, et un rapport qui ne cite qu'une ligne sur douze provoque douze allers-retours. Les
controles accumulent donc, et `ResultatControles.bloquant` tranche a la fin.

**Un controle ne modifie rien.** Ces fonctions lisent le fichier et le referentiel, elles rendent des
anomalies. L'ecriture, ou le refus d'ecrire, est la responsabilite de `flux/ingestion.py`. C'est ce
decoupage qui permet d'affirmer qu'un lot rejete n'a rien ecrit : le code qui ecrit n'est jamais appele.

Severite, et ce que chaque severite veut dire pour le metier :

| Controle | Severite | Pourquoi |
| --- | --- | --- |
| schema et types | bloquant | une ligne illisible rend le bordereau non rapprochable |
| fichier deja charge | bloquant | charger deux fois le meme bordereau double les cotisations et les commissions |
| doublon de cle metier | bloquant | deux lignes pour une meme quittance : on ne sait pas laquelle est bonne |
| contrat inconnu | bloquant | la compagnie quittance un contrat que le cabinet ne gere pas : defaut de perimetre |
| adherent inconnu | bloquant | meme raison |
| somme differente du total | bloquant | le fichier est tronque ou altere, aucune ligne n'est fiable |
| periode incoherente | bloquant | quittancer hors garantie expose le cabinet et fausse le ratio sinistres sur primes |
| acte non delegue | bloquant | la convention est echue : le cabinet n'a plus le droit d'accomplir cet acte |
| montant atypique | avertissement | une cotisation inhabituelle peut etre legitime, une revalorisation par exemple |
| prestation proche du plafond | avertissement | information de gestion, pas une erreur de fichier |
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from courtage.flux.enums import (
    SEVERITE_PAR_CODE,
    CodeAnomalie,
    SeveriteAnomalie,
    TypeFlux,
)
from courtage.flux.fichiers import (
    CLE_METIER,
    SCHEMAS,
    ContenuFichier,
    ValeurIllisible,
    colonnes_totalisees,
    convertit,
    convertit_montant,
)
from courtage.flux.parametres import (
    MINIMUM_APPELS_POUR_HISTORIQUE,
    SEUIL_MONTANT_ATYPIQUE,
    SEUIL_PROCHE_PLAFOND,
    TOLERANCE_TOTAL_CENTS,
)
from courtage.flux.referentiel import ReferentielControles, mois_couverts
from courtage.modeles.enums import ActeGestion

#: Longueur maximale d'une reference metier, alignee sur la colonne la plus courte du schema.
#: Une reference plus longue serait tronquee ou refusee par PostgreSQL a l'ecriture ; mieux vaut la
#: refuser ici, avec un motif lisible.
LONGUEUR_MAXIMALE_REFERENCE = 40


@dataclass(slots=True)
class Anomalie:
    """Une anomalie constatee, prete a etre enregistree ou publiee dans un rapport."""

    code: CodeAnomalie
    ligne_numero: int | None
    reference_ligne: str | None
    message: str
    valeur_constatee: str | None = None
    valeur_attendue: str | None = None

    @property
    def severite(self) -> SeveriteAnomalie:
        return SEVERITE_PAR_CODE[self.code]

    @property
    def bloquante(self) -> bool:
        return self.severite is SeveriteAnomalie.BLOQUANT

    def en_dictionnaire(self) -> dict[str, object]:
        return {
            "ligne_numero": self.ligne_numero,
            "reference_ligne": self.reference_ligne,
            "code": self.code.value,
            "severite": self.severite.value,
            "message": self.message,
            "valeur_constatee": self.valeur_constatee,
            "valeur_attendue": self.valeur_attendue,
        }


@dataclass(slots=True)
class LigneConvertie:
    """Une ligne dont toutes les cellules se sont converties dans le type attendu."""

    numero: int
    valeurs: dict[str, object]

    def texte(self, nom: str) -> str | None:
        valeur = self.valeurs.get(nom)
        return None if valeur is None else str(valeur)

    def entier(self, nom: str) -> int:
        return int(self.valeurs[nom])  # type: ignore[arg-type]

    def jour(self, nom: str) -> date:
        return self.valeurs[nom]  # type: ignore[return-value]


@dataclass(slots=True)
class ResultatControles:
    """Verdict complet sur un lot : les anomalies, et les lignes retenues si le lot passe."""

    type_flux: TypeFlux
    anomalies: list[Anomalie] = field(default_factory=list)
    lignes: list[LigneConvertie] = field(default_factory=list)
    #: Sommes calculees sur les lignes lisibles, par colonne totalisee.
    sommes_calculees: dict[str, int] = field(default_factory=dict)
    #: Sommes annoncees par la ligne de total du fichier, quand elle est lisible.
    sommes_declarees: dict[str, int] = field(default_factory=dict)

    @property
    def bloquant(self) -> bool:
        return any(anomalie.bloquante for anomalie in self.anomalies)

    @property
    def anomalies_bloquantes(self) -> list[Anomalie]:
        return [anomalie for anomalie in self.anomalies if anomalie.bloquante]

    def motif_rejet(self) -> str:
        """Phrase de rejet, lisible par un gestionnaire, qui cite les codes et leur compte."""
        comptes: dict[str, int] = defaultdict(int)
        for anomalie in self.anomalies_bloquantes:
            comptes[anomalie.code.value] += 1
        detail = ", ".join(f"{code} ({nombre})" for code, nombre in sorted(comptes.items()))
        return f"lot rejeté en totalité sur {len(self.anomalies_bloquantes)} anomalie(s) bloquante(s) : {detail}"


# --- Controles de structure et de type -----------------------------------------------------------


def _controle_structure(contenu: ContenuFichier, resultat: ResultatControles) -> None:
    """Agrege les defauts de structure en une seule anomalie de schema.

    Une seule, et non une par defaut : les defauts de structure sont presque toujours la consequence
    d'une meme cause, un export mal configure, et l'unicite en base porte sur le couple lot, ligne,
    code et reference. Les detailler dans le message garde l'information sans multiplier les lignes.
    """
    if not contenu.defauts_structure:
        return
    resultat.anomalies.append(
        Anomalie(
            code=CodeAnomalie.SCHEMA_INVALIDE,
            ligne_numero=None,
            reference_ligne=None,
            message="structure du fichier non conforme : " + " ; ".join(contenu.defauts_structure),
            valeur_attendue=", ".join(colonne.nom for colonne in SCHEMAS[contenu.type_flux]),
        )
    )


def _controle_porteur_unique(contenu: ContenuFichier, resultat: ResultatControles) -> None:
    """Un appel porte un contrat ou une police, jamais les deux, jamais aucun.

    Rattache au controle de schema, parce que c'est bien la forme de la ligne qui est fautive : un
    bordereau qui ne designe pas son payeur n'est pas rapprochable, et un bordereau qui en designe deux
    ne dit pas lequel regle.
    """
    if contenu.type_flux is not TypeFlux.COTISATIONS:
        return
    for ligne in resultat.lignes:
        contrat = ligne.texte("numero_contrat")
        police = ligne.texte("numero_police")
        if bool(contrat) == bool(police):
            constat = "les deux renseignes" if contrat else "aucun des deux renseigne"
            resultat.anomalies.append(
                Anomalie(
                    code=CodeAnomalie.SCHEMA_INVALIDE,
                    ligne_numero=ligne.numero,
                    reference_ligne=ligne.texte("reference_appel"),
                    message=f"porteur de l'appel indetermine : {constat}",
                    valeur_constatee=f"numero_contrat={contrat!r}, numero_police={police!r}",
                    valeur_attendue="un numero de contrat, ou un numero de police",
                )
            )


def _convertit_lignes(contenu: ContenuFichier, resultat: ResultatControles) -> set[str]:
    """Convertit chaque ligne, et rend l'ensemble des colonnes totalisees devenues illisibles.

    Une ligne dont une cellule ne se convertit pas produit une anomalie de schema et n'entre pas dans
    les controles suivants : verifier la coherence d'une periode qu'on n'a pas su lire reviendrait a
    inventer un second defaut a partir du premier.
    """
    colonnes_abimees: set[str] = set()
    for ligne in contenu.lignes:
        valeurs: dict[str, object] = {}
        echecs: list[str] = []
        for colonne in SCHEMAS[contenu.type_flux]:
            try:
                valeurs[colonne.nom] = convertit(colonne, ligne.valeurs.get(colonne.nom))
            except ValeurIllisible as erreur:
                echecs.append(f"{colonne.nom} : {erreur}")
                if colonne.totalisee:
                    colonnes_abimees.add(colonne.nom)
        if echecs:
            resultat.anomalies.append(
                Anomalie(
                    code=CodeAnomalie.SCHEMA_INVALIDE,
                    ligne_numero=ligne.numero,
                    reference_ligne=ligne.cle(contenu.type_flux) or None,
                    message="ligne illisible : " + " ; ".join(echecs),
                    valeur_constatee=" | ".join(
                        str(ligne.valeurs.get(colonne.nom, "")) for colonne in SCHEMAS[contenu.type_flux]
                    )[:200],
                )
            )
            continue
        resultat.lignes.append(LigneConvertie(numero=ligne.numero, valeurs=valeurs))
    return colonnes_abimees


# --- Controles bloquants -------------------------------------------------------------------------


def _controle_contraintes_de_base(
    type_flux: TypeFlux, resultat: ResultatControles
) -> None:
    """Reprend, cote controle, les invariants que la base impose par contrainte de verification.

    Pourquoi les redire ici alors que la base les garantit deja. Parce qu'une contrainte violee se
    manifeste au moment de l'ecriture, par une exception, et une exception n'est pas un rejet : elle
    interrompt la boucle et empeche le traitement des lots suivants. Le defaut a ete trouve en
    relecture, et il contredisait la promesse « un lot, une transaction, le rejet d'un lot ne bloque pas
    les autres ».

    La base garde ses contraintes : elles restent le dernier filet. Ce controle-ci sert a transformer la
    violation en rejet propre, avec un motif qu'un gestionnaire peut renvoyer a l'emetteur.
    """
    nom_cle = CLE_METIER[type_flux]
    for ligne in resultat.lignes:
        reference = ligne.texte(nom_cle)
        defauts: list[str] = []

        if type_flux is TypeFlux.COTISATIONS:
            debut, fin = ligne.jour("periode_debut"), ligne.jour("periode_fin")
            if fin < debut:
                defauts.append(f"periode inversee : du {debut} au {fin}")
            affilies = ligne.valeurs.get("nombre_affilies")
            if ligne.texte("numero_police"):
                if affilies is None:
                    defauts.append("appel groupe sans nombre d'affilies")
                elif int(affilies) < 1:
                    defauts.append(f"nombre d'affilies invalide : {affilies}")
            for colonne in ("montant_ht", "montant_taxe", "montant_total"):
                if ligne.entier(colonne) < 0:
                    defauts.append(f"{colonne} negatif : {ligne.entier(colonne)}")
            attendu = ligne.entier("montant_ht") + ligne.entier("montant_taxe")
            if ligne.entier("montant_total") != attendu:
                defauts.append(
                    f"total incoherent avec ses parts : {ligne.entier('montant_total')}"
                    f" au lieu de {attendu}"
                )

        elif type_flux is TypeFlux.ENCAISSEMENTS:
            if ligne.entier("montant") == 0:
                defauts.append("mouvement bancaire d'un montant nul")

        else:
            for colonne in ("montant_demande", "montant_rembourse"):
                if ligne.entier(colonne) < 0:
                    defauts.append(f"{colonne} negatif : {ligne.entier(colonne)}")
            if ligne.entier("montant_rembourse") > ligne.entier("montant_demande"):
                defauts.append(
                    f"remboursement {ligne.entier('montant_rembourse')} superieur a la demande"
                    f" {ligne.entier('montant_demande')}"
                )

        if reference and len(reference) > LONGUEUR_MAXIMALE_REFERENCE:
            defauts.append(f"reference de {len(reference)} caracteres, maximum {LONGUEUR_MAXIMALE_REFERENCE}")

        if defauts:
            resultat.anomalies.append(
                Anomalie(
                    code=CodeAnomalie.SCHEMA_INVALIDE,
                    ligne_numero=ligne.numero,
                    reference_ligne=reference,
                    message="ligne contraire aux invariants du schema : " + " ; ".join(defauts),
                )
            )


def _controle_empreinte(
    sha256: str, nom_fichier: str, referentiel: ReferentielControles, resultat: ResultatControles
) -> None:
    """Refuse un contenu deja charge sous un autre nom."""
    deja = referentiel.empreintes_chargees.get(sha256)
    if deja is None or deja == nom_fichier:
        return
    resultat.anomalies.append(
        Anomalie(
            code=CodeAnomalie.FICHIER_DEJA_CHARGE,
            ligne_numero=None,
            reference_ligne=None,
            message=f"contenu identique au fichier deja charge {deja}, meme empreinte SHA-256",
            valeur_constatee=sha256,
        )
    )


def _controle_doublons(type_flux: TypeFlux, resultat: ResultatControles) -> None:
    """Signale chaque cle metier presente plus d'une fois, une anomalie par cle.

    L'anomalie est portee par la premiere occurrence et cite toutes les lignes concernees : le
    gestionnaire a besoin de voir les deux lignes pour decider laquelle est la bonne, et une anomalie
    par occurrence lui en ferait lire deux pour un seul probleme.
    """
    nom_cle = CLE_METIER[type_flux]
    positions: dict[str, list[int]] = defaultdict(list)
    for ligne in resultat.lignes:
        cle = ligne.texte(nom_cle)
        if cle:
            positions[cle].append(ligne.numero)
    for cle, numeros in positions.items():
        if len(numeros) > 1:
            resultat.anomalies.append(
                Anomalie(
                    code=CodeAnomalie.DOUBLON_CLE_METIER,
                    ligne_numero=numeros[0],
                    reference_ligne=cle,
                    message=f"cle metier {nom_cle} = {cle} presente sur les lignes {numeros}",
                    valeur_constatee=f"{len(numeros)} occurrences",
                    valeur_attendue="1 occurrence",
                )
            )


def _controle_references(
    type_flux: TypeFlux, referentiel: ReferentielControles, resultat: ResultatControles
) -> None:
    """Verifie que chaque contrat et chaque adherent cite existe au portefeuille."""
    nom_cle = CLE_METIER[type_flux]
    for ligne in resultat.lignes:
        reference = ligne.texte(nom_cle)
        numero_contrat = ligne.texte("numero_contrat")
        if numero_contrat and not referentiel.contrat_connu(numero_contrat):
            resultat.anomalies.append(
                Anomalie(
                    code=CodeAnomalie.CONTRAT_INCONNU,
                    ligne_numero=ligne.numero,
                    reference_ligne=reference,
                    message=f"contrat {numero_contrat} absent du portefeuille",
                    valeur_constatee=numero_contrat,
                )
            )
        numero_police = ligne.texte("numero_police")
        if numero_police and not referentiel.police_connue(numero_police):
            resultat.anomalies.append(
                Anomalie(
                    code=CodeAnomalie.CONTRAT_INCONNU,
                    ligne_numero=ligne.numero,
                    reference_ligne=reference,
                    message=f"police collective {numero_police} absente du portefeuille",
                    valeur_constatee=numero_police,
                )
            )
        numero_adherent = ligne.texte("numero_adherent")
        if numero_adherent and numero_adherent not in referentiel.adherents:
            resultat.anomalies.append(
                Anomalie(
                    code=CodeAnomalie.ADHERENT_INCONNU,
                    ligne_numero=ligne.numero,
                    reference_ligne=reference,
                    message=f"adherent {numero_adherent} absent du portefeuille",
                    valeur_constatee=numero_adherent,
                )
            )


def _controle_total(
    contenu: ContenuFichier, colonnes_abimees: set[str], resultat: ResultatControles
) -> None:
    """Compare la ligne de total aux sommes calculees, et le nombre de lignes annonce au nombre lu.

    Le controle des montants se tait des qu'une ligne du fichier n'a pas pu etre lue, quelle que soit la
    colonne fautive : une ligne ecartee manque a la somme, donc la comparaison signalerait un ecart qui
    ne vient pas du total mais de la ligne illisible. Ce serait une seconde anomalie fabriquee a partir
    de la premiere, et le rapport renvoye a l'emetteur l'enverrait sur une fausse piste.

    Le controle du nombre de lignes, lui, continue : il compte les lignes lues, lisibles ou non, donc il
    reste exact. C'est meme le seul controle qui detecte un fichier tronque a la transmission.
    """
    # Seules les lignes dont une cellule n'a pas pu etre **convertie** manquent a la somme. Une ligne
    # lisible mais contraire a un invariant, elle, a bien ete sommee : s'en servir pour taire le
    # controle du total ferait perdre une anomalie reelle dans le rapport renvoye a l'emetteur.
    lignes_illisibles = len(resultat.lignes) < len(contenu.lignes)
    type_flux = contenu.type_flux
    for nom in colonnes_totalisees(type_flux):
        resultat.sommes_calculees[nom] = sum(ligne.entier(nom) for ligne in resultat.lignes)
    if contenu.total_declare is None:
        return

    ecarts: list[str] = []
    for nom in colonnes_totalisees(type_flux):
        brut = contenu.total_declare.get(nom)
        try:
            declare = convertit_montant(brut)  # type: ignore[arg-type]
        except ValeurIllisible:
            ecarts.append(f"total de la colonne {nom} illisible : {brut!r}")
            continue
        resultat.sommes_declarees[nom] = declare
        if nom in colonnes_abimees or lignes_illisibles:
            continue
        calcule = resultat.sommes_calculees[nom]
        if abs(declare - calcule) > TOLERANCE_TOTAL_CENTS:
            ecarts.append(
                f"colonne {nom} : total declare {declare} centimes, somme des lignes {calcule} centimes,"
                f" ecart {declare - calcule}"
            )

    if contenu.nombre_lignes_declare is not None:
        try:
            annonce = int(str(contenu.nombre_lignes_declare).strip())
        except ValueError:
            ecarts.append(f"nombre de lignes annonce illisible : {contenu.nombre_lignes_declare!r}")
        else:
            lu = len(contenu.lignes)
            if annonce != lu:
                ecarts.append(f"nombre de lignes annonce {annonce}, lignes lues {lu}")

    if ecarts:
        resultat.anomalies.append(
            Anomalie(
                code=CodeAnomalie.TOTAL_INCOHERENT,
                ligne_numero=None,
                reference_ligne=None,
                message="ligne de total incoherente : " + " ; ".join(ecarts),
            )
        )


def _periode_de_la_ligne(type_flux: TypeFlux, ligne: LigneConvertie) -> tuple[date, date] | None:
    """Periode couverte par la ligne, selon le flux. Rend None quand la notion n'a pas de sens."""
    if type_flux is TypeFlux.COTISATIONS:
        return ligne.jour("periode_debut"), ligne.jour("periode_fin")
    if type_flux is TypeFlux.PRESTATIONS:
        soins = ligne.jour("date_soins")
        return soins, soins
    return None


def _controle_periodes(
    type_flux: TypeFlux, referentiel: ReferentielControles, resultat: ResultatControles
) -> None:
    """Verifie que la periode de la ligne tombe dans la periode de garantie du contrat.

    Un contrat reste au portefeuille apres sa resiliation, et le cabinet continue d'y lire des flux : le
    jour de la resiliation est donc une borne **exclusive**, comme au module 1. Un contrat encore a
    l'etat de devis n'a pas de date d'effet, et aucune ligne ne peut lui etre rattachee.
    """
    nom_cle = CLE_METIER[type_flux]
    for ligne in resultat.lignes:
        numero_contrat = ligne.texte("numero_contrat")
        numero_police = ligne.texte("numero_police")
        porteur = numero_contrat or numero_police
        if not porteur:
            continue
        contexte = referentiel.porteur(numero_contrat, numero_police)
        if contexte is None:
            # Porteur inconnu : deja signale par son propre controle. Ne pas le signaler deux fois.
            continue
        fenetre = _periode_de_la_ligne(type_flux, ligne)
        if fenetre is None:
            continue
        debut, fin = fenetre
        reference = ligne.texte(nom_cle)
        if contexte.date_effet is None:
            resultat.anomalies.append(
                Anomalie(
                    code=CodeAnomalie.PERIODE_INCOHERENTE,
                    ligne_numero=ligne.numero,
                    reference_ligne=reference,
                    message=f"porteur {porteur} sans date d'effet : aucun flux ne peut s'y rattacher",
                    valeur_constatee=f"{debut} au {fin}",
                )
            )
            continue
        if debut < contexte.date_effet:
            resultat.anomalies.append(
                Anomalie(
                    code=CodeAnomalie.PERIODE_INCOHERENTE,
                    ligne_numero=ligne.numero,
                    reference_ligne=reference,
                    message=(
                        f"periode debutant le {debut} anterieure a la date d'effet de"
                        f" {porteur}, fixee au {contexte.date_effet}"
                    ),
                    valeur_constatee=str(debut),
                    valeur_attendue=f"a partir du {contexte.date_effet}",
                )
            )
            continue
        if contexte.date_resiliation is not None and debut >= contexte.date_resiliation:
            resultat.anomalies.append(
                Anomalie(
                    code=CodeAnomalie.PERIODE_INCOHERENTE,
                    ligne_numero=ligne.numero,
                    reference_ligne=reference,
                    message=(
                        f"periode debutant le {debut} posterieure a la resiliation de"
                        f" {porteur}, prononcee au {contexte.date_resiliation}"
                    ),
                    valeur_constatee=str(debut),
                    valeur_attendue=f"avant le {contexte.date_resiliation}",
                )
            )


def _controle_delegation(
    type_flux: TypeFlux, referentiel: ReferentielControles, resultat: ResultatControles
) -> None:
    """Refuse une ligne dont l'acte n'est pas delegue au cabinet a la date de l'operation.

    Quel acte, selon le flux, et pourquoi le releve bancaire n'en fait pas partie :

    - un **bordereau de cotisations** atteste que le cabinet a quittance au nom de la compagnie. L'acte
      est donc le quittancement, et il doit etre delegue a la date d'echeance.
    - un **bordereau de prestations** atteste que le cabinet a regle des prestations. L'acte est la
      gestion des prestations, a la date des soins.
    - un **releve bancaire** n'atteste d'aucun acte du cabinet : l'argent est arrive, c'est un fait. Ce
      qui serait un acte, c'est de le rapprocher et de l'affecter, et c'est le rapprochement qui le
      refuse, ligne par ligne, sans rejeter le releve entier. Rejeter un releve de huit cents lignes
      parce qu'un adherent a paye le cabinet par erreur serait disproportionne.

    Le controle est bloquant sur les deux premiers flux : charger une quittance hors mandat
    reviendrait a enregistrer un acte que le cabinet n'a pas le droit d'accomplir, et c'est exactement
    ce qu'une convention echue interdit.
    """
    acte_attendu = {
        TypeFlux.COTISATIONS: ActeGestion.QUITTANCEMENT,
        TypeFlux.PRESTATIONS: ActeGestion.GESTION_PRESTATIONS,
    }.get(type_flux)
    if acte_attendu is None:
        return
    champ_date = "date_echeance" if type_flux is TypeFlux.COTISATIONS else "date_soins"
    nom_cle = CLE_METIER[type_flux]

    for ligne in resultat.lignes:
        porteur = ligne.texte("numero_contrat") or ligne.texte("numero_police")
        produit_id = referentiel.produit_du_porteur.get(porteur) if porteur else None
        if produit_id is None:
            code_produit = ligne.texte("code_produit")
            produit_id = referentiel.produit_par_code.get(code_produit) if code_produit else None
        if produit_id is None:
            # Porteur et produit inconnus : signale par le controle des references, pas ici.
            continue
        jour = ligne.jour(champ_date)
        actes = referentiel.actes_delegues(produit_id, jour)
        if acte_attendu in actes:
            continue
        delegues = ", ".join(sorted(acte.value for acte in actes)) or "aucun"
        resultat.anomalies.append(
            Anomalie(
                code=CodeAnomalie.ACTE_NON_DELEGUE,
                ligne_numero=ligne.numero,
                reference_ligne=ligne.texte(nom_cle),
                message=(
                    f"l'acte {acte_attendu.value} n'est pas delegue au cabinet le {jour} pour le"
                    f" produit {produit_id} du porteur {porteur}. Actes delegues a cette date :"
                    f" {delegues}"
                ),
                valeur_constatee=f"{acte_attendu.value} le {jour}",
                valeur_attendue=f"un acte parmi : {delegues}",
            )
        )


# --- Controles non bloquants ---------------------------------------------------------------------


def _controle_montant_atypique(referentiel: ReferentielControles, resultat: ResultatControles) -> None:
    """Signale une cotisation dont le tarif mensuel s'ecarte fortement de l'historique du contrat.

    Le tarif mensuel, et non le montant de l'appel : un contrat trimestriel appelle trois mois a la
    fois, et sa derniere periode est souvent tronquee par une resiliation. Comparer les montants bruts
    signalerait ces deux cas tout a fait normaux.

    Le controle se tait sous le minimum d'appels connus. Parler d'historique sur une ou deux echeances
    serait une affirmation sans support.
    """
    for ligne in resultat.lignes:
        numero_contrat = ligne.texte("numero_contrat")
        numero_police = ligne.texte("numero_police")
        porteur = numero_contrat or numero_police
        if not porteur:
            continue
        contexte = referentiel.porteur(numero_contrat, numero_police)
        if contexte is None or contexte.nombre_appels < MINIMUM_APPELS_POUR_HISTORIQUE:
            continue
        reference_mensuelle = contexte.tarif_mensuel_median_cents
        if reference_mensuelle <= 0:
            continue
        # Ramene au mois et a l'affilie : une police qui gagne dix salaries voit son appel grossir sans
        # que son tarif ait bouge, et ce n'est pas une anomalie.
        affilies = ligne.valeurs.get("nombre_affilies")
        diviseur = mois_couverts(ligne.jour("periode_debut"), ligne.jour("periode_fin")) * max(
            1, int(affilies or 1)
        )
        tarif_observe = round(ligne.entier("montant_ht") / diviseur)
        ecart_relatif = abs(tarif_observe - reference_mensuelle) / reference_mensuelle
        if ecart_relatif > SEUIL_MONTANT_ATYPIQUE:
            resultat.anomalies.append(
                Anomalie(
                    code=CodeAnomalie.MONTANT_ATYPIQUE,
                    ligne_numero=ligne.numero,
                    reference_ligne=ligne.texte("reference_appel"),
                    message=(
                        f"tarif mensuel par affilie de {tarif_observe} centimes contre"
                        f" {reference_mensuelle} centimes en mediane sur les"
                        f" {contexte.nombre_appels} echeances connues de {porteur},"
                        f" ecart de {ecart_relatif:.0%}"
                    ),
                    valeur_constatee=f"{tarif_observe} centimes par mois",
                    valeur_attendue=f"{reference_mensuelle} centimes par mois, a {SEUIL_MONTANT_ATYPIQUE:.0%} pres",
                )
            )


def _controle_proche_plafond(referentiel: ReferentielControles, resultat: ResultatControles) -> None:
    """Signale une prestation qui porte la consommation annuelle pres du plafond de la garantie.

    La consommation enregistree par le cabinet **avant la date des soins** est reprise, la prestation du
    bordereau est ajoutee, et le total est compare au plafond annuel de la formule. Deux precautions
    comptent ici. La consommation posterieure est exclue, sinon le controle regarderait l'avenir et
    signalerait en juillet un depassement qui n'arrivera qu'en decembre. Et si le bordereau reprend une
    prestation deja connue, son montant n'est pas compte deux fois.

    **Le signalement a lieu au franchissement du seuil, une fois.** La premiere version de ce controle
    signalait toute prestation d'un adherent deja au-dela du seuil, et elle a ete mesuree : sept cent
    quarante-cinq signalements, dont soixante-et-onze correspondaient a une anomalie reellement
    injectee. Un gestionnaire qui recoit dix alertes pour un meme adherent n'en lit aucune. Le controle
    ne se declenche donc que sur la prestation qui fait passer la consommation annuelle au-dessus du
    seuil : une alerte par adherent, par garantie et par annee, au moment ou elle est actionnable,
    c'est-a-dire quand il reste encore du plafond a consommer.
    """
    for ligne in resultat.lignes:
        numero_contrat = ligne.texte("numero_contrat")
        code_garantie = ligne.texte("code_garantie")
        reference = ligne.texte("reference_prestation")
        if not (numero_contrat and code_garantie and reference):
            continue
        plafond = referentiel.plafond(numero_contrat, code_garantie)
        if plafond is None or plafond <= 0:
            continue
        jour = ligne.jour("date_soins")
        annee = jour.year
        deja = referentiel.cumul_avant(numero_contrat, code_garantie, jour, reference)
        cumule = deja + ligne.entier("montant_rembourse")
        seuil = plafond * SEUIL_PROCHE_PLAFOND
        if deja < seuil <= cumule:
            resultat.anomalies.append(
                Anomalie(
                    code=CodeAnomalie.PRESTATION_PROCHE_PLAFOND,
                    ligne_numero=ligne.numero,
                    reference_ligne=reference,
                    message=(
                        f"cette prestation porte la consommation {annee} de {deja} a {cumule} centimes"
                        f" sur la garantie {code_garantie} du contrat {numero_contrat}, pour un plafond"
                        f" annuel de {plafond} centimes : le seuil d'alerte est franchi"
                    ),
                    valeur_constatee=f"{cumule} centimes cumules",
                    valeur_attendue=f"moins de {round(seuil)} centimes",
                )
            )


# --- Enchainement ---------------------------------------------------------------------------------


def applique_controles(
    contenu: ContenuFichier,
    sha256: str,
    nom_fichier: str,
    referentiel: ReferentielControles,
) -> ResultatControles:
    """Applique tous les controles a un lot et rend le verdict complet.

    Ne touche pas a la base. C'est l'appelant qui decide d'ecrire ou de rejeter, et c'est ce qui rend
    verifiable l'exigence « aucune ecriture partielle ».
    """
    resultat = ResultatControles(type_flux=contenu.type_flux)
    _controle_structure(contenu, resultat)
    colonnes_abimees = _convertit_lignes(contenu, resultat)
    _controle_porteur_unique(contenu, resultat)
    _controle_contraintes_de_base(contenu.type_flux, resultat)
    _controle_empreinte(sha256, nom_fichier, referentiel, resultat)
    _controle_doublons(contenu.type_flux, resultat)
    _controle_references(contenu.type_flux, referentiel, resultat)
    _controle_total(contenu, colonnes_abimees, resultat)
    _controle_periodes(contenu.type_flux, referentiel, resultat)
    _controle_delegation(contenu.type_flux, referentiel, resultat)
    if contenu.type_flux is TypeFlux.COTISATIONS:
        _controle_montant_atypique(referentiel, resultat)
    if contenu.type_flux is TypeFlux.PRESTATIONS:
        _controle_proche_plafond(referentiel, resultat)
    return resultat
