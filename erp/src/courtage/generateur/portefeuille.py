"""Generation du portefeuille : entreprises souscriptrices, adherents, contrats et avenants.

C'est ici que vivent les trois signaux realistes exiges par la specification :

1. **Les hausses tarifaires a l'echeance du 1er janvier.** Chaque contrat en vigueur recoit un avenant de
   revalorisation au 1er janvier de chaque annee, d'une ampleur tiree par produit et par annee.
2. **Les resiliations, avec des causes.** La probabilite de resiliation n'est pas uniforme : elle monte
   apres une revalorisation forte, quand l'anciennete est faible, et chez deux apporteurs sur dix. Elle
   baisse chez les plus de 60 ans. La sinistralite individuelle, prevue a l'origine, n'est pas appliquee :
   les prestations sont generees apres les contrats et n'existent pas au moment du tirage. Ces
   facteurs sont documentes dans `docs/HYPOTHESES.md` et c'est ce que le module 5 devra retrouver.
3. **La saisonnalite des prestations**, qui est generee au module `financier`.

Les identifiants sont attribues par le generateur lui-meme, en partant de 1. Cela rend la generation
deterministe et permet d'inserer par lots sans relire la base. Sur PostgreSQL, les sequences sont ensuite
recalees, sans quoi la premiere souscription saisie dans l'interface reclamerait un identifiant deja pris.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from random import Random

from sqlalchemy.orm import Session

from courtage.generateur import parametres as reglages
from courtage.generateur.outils import (
    age_a_la_date,
    ajoute_mois,
    insere_par_lots,
    mois_entre,
    tire_pondere,
)
from courtage.generateur.parametres import (
    AGE_ELEVE,
    AGE_PIVOT_MAJORATION,
    ANCIENNETE_FAIBLE_MOIS,
    ANCIENNETE_MINIMALE_INFRA_ANNUELLE_MOIS,
    BORNES_COTISATION_CENTS,
    DEBUT_PORTEFEUILLE,
    FACTEUR_AGE_ELEVE,
    FACTEUR_APPORTEUR_VOLATIL,
    FACTEUR_FAIBLE_ANCIENNETE,
    FACTEUR_REVALORISATION_SENSIBLE,
    INDICES_APPORTEURS_VOLATILS,
    MAJORATION_AGE_PAR_TRANCHE_BP,
    PART_CHANGEMENT_APPORTEUR,
    PART_CHANGEMENT_FORMULE,
    PART_DEVIS_SANS_SUITE,
    PART_SECOND_CONTRAT,
    PART_SUSPENSION,
    POIDS_DEPARTEMENTS,
    POIDS_FRACTIONNEMENT,
    POIDS_MOTIF_RESILIATION,
    PREAVIS_ECHEANCE_MOIS,
    PROBA_RESILIATION_BASE,
    REVALORISATION_BP_PAR_ANNEE,
    SEUIL_REVALORISATION_SENSIBLE_BP,
)
from courtage.generateur.referentiel import PRODUITS as PRODUITS_REFERENTIEL
from courtage.generateur.referentiel import Referentiel
from courtage.modeles.enums import Fractionnement, MotifResiliation, StatutContrat, TypeAvenant
from courtage.modeles.portefeuille import Adherent, Avenant, Contrat, EntrepriseSouscriptrice

#: Borne de fin conventionnelle d'une suspension encore ouverte a la fin de l'historique simule.
#: Les periodes de suspension sont des intervalles (debut inclus, fin exclue) ; une suspension qui ne
#: se referme pas avant la date de reference porte donc une borne volontairement hors d'atteinte. Une
#: borne nulle aurait oblige a ecrire le test d'appartenance en deux cas a chaque usage, et c'est
#: exactement le genre d'oubli qui a laisse passer des prestations pendant une suspension en cours.
FIN_SUSPENSION_OUVERTE = date(9999, 12, 31)

NOMS = (
    "Martin", "Bernard", "Thomas", "Petit", "Robert", "Richard", "Durand", "Dubois", "Moreau", "Laurent",
    "Simon", "Michel", "Lefebvre", "Leroy", "Roux", "David", "Bertrand", "Morel", "Fournier", "Girard",
    "Bonnet", "Dupont", "Lambert", "Fontaine", "Rousseau", "Vincent", "Muller", "Lefevre", "Faure",
    "Andre", "Mercier", "Blanc", "Guerin", "Boyer", "Garnier", "Chevalier", "Francois", "Legrand",
    "Gauthier", "Garcia", "Perrin", "Robin", "Clement", "Morin", "Nicolas", "Henry", "Roussel",
    "Mathieu", "Gautier", "Masson", "Marchand", "Duval", "Denis", "Dumont", "Marie", "Lemaire", "Noel",
    "Meyer", "Dufour", "Meunier", "Brun", "Blanchard", "Giraud", "Joly", "Riviere", "Lucas", "Brunet",
    "Benali", "Saidi", "Ferreira", "Da Silva", "Lopez", "Martinez", "Sanchez", "Perez", "Nguyen",
)
PRENOMS_F = (
    "Marie", "Nathalie", "Isabelle", "Sylvie", "Catherine", "Martine", "Christine", "Francoise",
    "Monique", "Valerie", "Sandrine", "Stephanie", "Veronique", "Celine", "Patricia", "Chantal",
    "Julie", "Aurelie", "Laetitia", "Emilie", "Camille", "Sarah", "Laura", "Manon", "Lea", "Chloe",
    "Ines", "Yasmina", "Fatima", "Amelie", "Claire", "Helene", "Anne", "Dominique", "Nicole",
)
PRENOMS_M = (
    "Jean", "Philippe", "Michel", "Alain", "Patrick", "Nicolas", "Christophe", "Pierre", "Christian",
    "Eric", "Frederic", "Laurent", "David", "Daniel", "Stephane", "Pascal", "Thierry", "Olivier",
    "Bernard", "Julien", "Sebastien", "Vincent", "Thomas", "Maxime", "Antoine", "Alexandre", "Hugo",
    "Lucas", "Mohamed", "Karim", "Bruno", "Didier", "Gilles", "Yves", "Serge",
)
FORMES_SOCIALES = ("SARL", "SAS", "SA", "SASU", "SCOP", "EURL")
ACTIVITES = (
    "Transports", "Batiment", "Conseil", "Metallurgie", "Agroalimentaire", "Informatique", "Vignoble",
    "Logistique", "Imprimerie", "Nettoyage", "Securite", "Menuiserie", "Electricite", "Plomberie",
    "Restauration", "Hotellerie", "Pharmacie", "Optique", "Garage", "Paysagiste",
)
CODES_NAF = (
    "4941A", "4120A", "7022Z", "2550A", "1089Z", "6201Z", "0121Z", "5229A", "1812Z", "8121Z",
    "8010Z", "4332A", "4321A", "4322A", "5610A", "5510Z", "4773Z", "4778A", "4520A", "8130Z",
)


@dataclass
class ContratGenere:
    """Contrat genere, avec ce dont le module financier a besoin pour produire ses flux."""

    id: int
    numero_contrat: str
    adherent_id: int
    adherent_naissance: date
    produit_code: str
    produit_id: int
    entreprise_id: int | None
    police_id: int | None
    statut: StatutContrat
    date_souscription: date
    date_effet: date | None
    date_resiliation: date | None
    fractionnement: Fractionnement
    #: Etapes de cotisation : (date de debut d'application, cotisation mensuelle en centimes).
    #: Le module financier s'en sert pour quittancer au bon tarif sans relire les avenants.
    paliers_cotisation: list[tuple[date, int]]
    #: Apporteur en vigueur par periode : (date de debut, identifiant de l'apporteur).
    paliers_apporteur: list[tuple[date, int]]
    #: Periodes pendant lesquelles les garanties sont suspendues : (debut inclus, fin exclue).
    #: Aucune prestation ne peut etre servie pendant l'une d'elles : la garantie ne court pas.
    #: Une suspension encore ouverte a la fin de l'historique porte `FIN_SUSPENSION_OUVERTE` comme
    #: borne de fin, et non une absence de borne, pour que le test d'appartenance reste ecrit une
    #: seule fois cote prestations.
    suspensions: list[tuple[date, date]]
    #: Formule en vigueur par periode : (date de debut, identifiant de la formule). Le taux de
    #: remboursement d'une prestation doit etre celui de la formule en vigueur a la date des soins, et
    #: non celui de la formule actuelle.
    paliers_formule: list[tuple[date, int]]
    #: Etat courant, recopie du dernier avenant genere. C'est le cache que porte la table `contrat`.
    formule_id_courante: int
    apporteur_id_courant: int
    cotisation_courante: int
    motif_resiliation_courante: MotifResiliation | None


@dataclass
class PoliceGeneree:
    """Police collective, telle que le module financier en a besoin pour quittancer l'entreprise."""

    id: int
    numero_police: str
    entreprise_id: int
    code_produit: str
    date_effet: date
    fractionnement: Fractionnement


@dataclass
class Portefeuille:
    entreprises: int = 0
    adherents: int = 0
    contrats: list[ContratGenere] | None = None
    avenants: int = 0
    polices: list[PoliceGeneree] | None = None


def genere_portefeuille(
    session: Session, alea: Random, ref: Referentiel, date_reference: date
) -> Portefeuille:
    """Cree entreprises, adherents, contrats et avenants, et retourne de quoi generer les flux."""
    entreprises = _genere_entreprises(session, alea)
    _genere_polices_collectives(session, alea, ref, entreprises)
    adherents = _genere_adherents(session, alea, entreprises, date_reference)

    contrats, avenants, nb_avenants = _genere_contrats_et_avenants(alea, ref, adherents, date_reference)

    insere_par_lots(session, Contrat, [_ligne_contrat(c) for c in contrats])
    insere_par_lots(session, Avenant, avenants)

    return Portefeuille(
        entreprises=len(entreprises),
        adherents=len(adherents),
        contrats=contrats,
        avenants=nb_avenants,
        polices=[
            PoliceGeneree(
                id=e.police_id,
                numero_police=f"POL-{e.police_date_effet.year}-{e.police_id:04d}",
                entreprise_id=e.id,
                code_produit=e.police_code_produit,
                date_effet=e.police_date_effet,
                fractionnement=e.police_fractionnement,
            )
            for e in entreprises
        ],
    )


@dataclass
class _Entreprise:
    id: int
    departement: str
    effectif: int
    date_adhesion: date
    #: Police collective souscrite par l'entreprise. Toutes les affiliations de ses salaries s'y
    #: rattachent, et c'est a elle que l'appel de cotisation groupe est adresse.
    police_id: int = 0
    police_code_produit: str = ""
    police_formule_socle_id: int = 0
    police_apporteur_id: int = 0
    police_date_effet: date = DEBUT_PORTEFEUILLE
    police_fractionnement: Fractionnement = Fractionnement.TRIMESTRIEL


@dataclass
class _Adherent:
    id: int
    numero: str
    naissance: date
    sexe: str
    departement: str
    entreprise: _Entreprise | None
    date_creation: date


def _genere_entreprises(session: Session, alea: Random) -> list[_Entreprise]:
    lignes: list[dict] = []
    entreprises: list[_Entreprise] = []
    # Lues sur le module et non importees par leur nom : un test qui abaisse la volumetrie
    # doit pouvoir le faire reellement.
    for index in range(1, reglages.NB_ENTREPRISES + 1):
        departement = tire_pondere(alea, POIDS_DEPARTEMENTS)
        effectif = alea.randint(reglages.NB_ADHERENTS_COLLECTIFS_MIN, reglages.NB_ADHERENTS_COLLECTIFS_MAX)
        indice_activite = alea.randrange(len(ACTIVITES))
        # L'adhesion de l'entreprise precede celle de ses salaries : les contrats collectifs ne peuvent
        # pas prendre effet avant que l'entreprise ait souscrit.
        date_adhesion = DEBUT_PORTEFEUILLE + timedelta(days=alea.randint(0, 1_600))
        lignes.append(
            {
                "id": index,
                "siren": f"{300_000_000 + index * 7_919:09d}",
                "raison_sociale": f"{ACTIVITES[indice_activite]} {alea.choice(NOMS)} {alea.choice(FORMES_SOCIALES)}",
                "code_naf": CODES_NAF[indice_activite],
                "effectif": effectif,
                "departement": departement,
                "date_adhesion": date_adhesion,
            }
        )
        entreprises.append(
            _Entreprise(id=index, departement=departement, effectif=effectif, date_adhesion=date_adhesion)
        )
    insere_par_lots(session, EntrepriseSouscriptrice, lignes)
    return entreprises


#: Periodicite des appels groupes adresses aux entreprises. Le trimestriel domine : un service
#: comptable ne traite pas douze appels par an de bon coeur.
POIDS_FRACTIONNEMENT_POLICE = {"mensuel": 25, "trimestriel": 55, "semestriel": 12, "annuel": 8}


def _genere_polices_collectives(
    session: Session, alea: Random, ref: Referentiel, entreprises: list[_Entreprise]
) -> None:
    """Une police collective par entreprise souscriptrice.

    C'est le contrat cadre : l'entreprise souscrit, le cabinet place, et chaque salarie couvert y est
    affilie. L'appel de cotisation est ensuite adresse a l'entreprise, pas au salarie.
    """
    from courtage.modeles.collectif import PoliceCollective

    produits_collectifs = sorted(
        code for code, _libelle, branche, _cie in PRODUITS_REFERENTIEL if branche == "SC"
    )
    lignes: list[dict] = []
    for index, entreprise in enumerate(entreprises, start=1):
        code_produit = alea.choice(produits_collectifs)
        formules = ref.formules[code_produit]
        # Le socle obligatoire de la police est la formule 1 ou 2 : l'entreprise finance un niveau
        # de base, et le salarie qui veut davantage souscrit une formule superieure a sa charge.
        socle = formules[alea.choice((0, 0, 1))]
        apporteur = alea.choice(ref.apporteurs)
        fractionnement = Fractionnement(tire_pondere(alea, POIDS_FRACTIONNEMENT_POLICE))
        date_effet = entreprise.date_adhesion

        entreprise.police_id = index
        entreprise.police_code_produit = code_produit
        entreprise.police_formule_socle_id = socle.id
        entreprise.police_apporteur_id = apporteur.id
        entreprise.police_date_effet = date_effet
        entreprise.police_fractionnement = fractionnement

        lignes.append(
            {
                "id": index,
                "numero_police": f"POL-{date_effet.year}-{index:04d}",
                "entreprise_id": entreprise.id,
                "produit_id": ref.produits[code_produit].id,
                "formule_socle_id": socle.id,
                "apporteur_id": apporteur.id,
                "statut": StatutContrat.EN_COURS,
                "date_souscription": date_effet,
                "date_effet": date_effet,
                "date_resiliation": None,
                "motif_resiliation": None,
                "fractionnement": fractionnement,
            }
        )
    insere_par_lots(session, PoliceCollective, lignes)


def _genere_adherents(
    session: Session, alea: Random, entreprises: list[_Entreprise], date_reference: date
) -> list[_Adherent]:
    """Les adherents collectifs d'abord, puis les individuels. Environ 5 000 au total."""
    lignes: list[dict] = []
    adherents: list[_Adherent] = []
    identifiant = 0

    def ajoute(entreprise: _Entreprise | None) -> None:
        nonlocal identifiant
        identifiant += 1
        sexe = "F" if alea.random() < 0.51 else "M"
        prenom = alea.choice(PRENOMS_F if sexe == "F" else PRENOMS_M)
        nom = alea.choice(NOMS)
        # Population d'age actif pour le collectif, plus large pour l'individuel, qui comprend des
        # retraites : c'est ce qui donne au produit Senior une clientele.
        age = alea.randint(19, 62) if entreprise else alea.randint(18, 84)
        if entreprise is not None:
            departement = entreprise.departement
            # Un salarie est affilie a partir de l'adhesion de son entreprise, jamais avant.
            plancher = entreprise.date_adhesion
        else:
            departement = tire_pondere(alea, POIDS_DEPARTEMENTS)
            plancher = DEBUT_PORTEFEUILLE
        # Annee de naissance deduite de la **date de reference**, jamais de l'horloge. Avec
        # `date.today()`, la meme graine aurait produit des ages differents au 1er janvier suivant,
        # donc des cotisations et des probabilites de resiliation differentes, donc une autre
        # empreinte. La reproductibilite annoncee serait devenue fausse sans que rien ne le signale,
        # puisque les deux executions d'une meme preuve ont toujours lieu le meme jour.
        naissance = date(date_reference.year - age, alea.randint(1, 12), alea.randint(1, 28))
        # Les entrees s'etalent sur toute la profondeur du portefeuille, et non sur la seule fenetre
        # de donnees : le cabinet avait deja des clients avant de conserver ses flux en ligne.
        marge = max(1, (date_reference - plancher).days - 60)
        date_creation = plancher + timedelta(days=alea.randint(0, marge))
        lignes.append(
            {
                "id": identifiant,
                "numero_adherent": f"A{identifiant:06d}",
                "nom": nom,
                "prenom": prenom,
                "date_naissance": naissance,
                "sexe": sexe,
                "code_postal": f"{departement.zfill(2)}{alea.randint(0, 999):03d}"[:5],
                "departement": departement,
                "courriel": f"{prenom.lower()}.{nom.lower().replace(' ', '')}{identifiant}@exemple.invalid",
                "telephone": f"06{alea.randint(10_000_000, 99_999_999)}",
                "entreprise_id": entreprise.id if entreprise else None,
                "date_creation": date_creation,
                # Instant de creation deduit des dates simulees, et non de l'horloge : sinon deux
                # executions a graine egale ne donneraient pas la meme empreinte.
                "cree_le": datetime.combine(date_creation, time(9, 0)),
            }
        )
        adherents.append(
            _Adherent(
                id=identifiant,
                numero=f"A{identifiant:06d}",
                naissance=naissance,
                sexe=sexe,
                departement=departement,
                entreprise=entreprise,
                date_creation=date_creation,
            )
        )

    for entreprise in entreprises:
        for _ in range(entreprise.effectif):
            ajoute(entreprise)
    for _ in range(reglages.NB_ADHERENTS_INDIVIDUELS):
        ajoute(None)

    insere_par_lots(session, Adherent, lignes)
    return adherents


def _cotisation_initiale(alea: Random, ref: Referentiel, code_produit: str, niveau: int, age: int) -> int:
    """Cotisation mensuelle a la souscription : tarif de la formule, puis majoration liee a l'age."""
    formule = ref.formules[code_produit][niveau - 1]
    base = formule.tarif_base_mensuel_cents
    tranches = max(0, (age - AGE_PIVOT_MAJORATION) // 5)
    majoree = base + base * tranches * MAJORATION_AGE_PAR_TRANCHE_BP // 10_000
    borne_basse, borne_haute = BORNES_COTISATION_CENTS[ref.code_branche_de(code_produit)]
    # Dispersion individuelle de quelques pour cent, pour que deux adherents de meme age et meme
    # formule n'aient pas exactement le meme tarif, comme dans un portefeuille reel.
    dispersion = alea.randint(-400, 400)
    return max(borne_basse, min(borne_haute, majoree + majoree * dispersion // 10_000))


def _probabilite_resiliation(
    revalorisation_bp: int, anciennete_mois: int, age: int, indice_apporteur: int
) -> float:
    """Probabilite annuelle de resiliation, apres application des facteurs documentes.

    Les facteurs sont multiplicatifs et volontairement simples : le module 5 doit pouvoir les retrouver,
    et une loi trop entortillee ne prouverait rien de plus.
    """
    probabilite = PROBA_RESILIATION_BASE
    if revalorisation_bp >= SEUIL_REVALORISATION_SENSIBLE_BP:
        probabilite *= FACTEUR_REVALORISATION_SENSIBLE
    if anciennete_mois < ANCIENNETE_FAIBLE_MOIS:
        probabilite *= FACTEUR_FAIBLE_ANCIENNETE
    if age >= AGE_ELEVE:
        probabilite *= FACTEUR_AGE_ELEVE
    if indice_apporteur in INDICES_APPORTEURS_VOLATILS:
        probabilite *= FACTEUR_APPORTEUR_VOLATIL
    return min(0.9, probabilite)


def _produits_eligibles(ref: Referentiel, adherent: _Adherent, a_la_date: date) -> list[str]:
    """Produits qu'un adherent peut souscrire, selon qu'il releve du collectif ou de l'individuel.

    En collectif, il n'y a aucun choix : le salarie est affilie a la police que **son entreprise** a
    souscrite, donc au produit de cette police. Tirer un produit au hasard, comme le faisait la
    version precedente, revenait a laisser chaque salarie d'une meme entreprise relever d'un contrat
    different, ce qui n'existe pas.
    """
    age = age_a_la_date(adherent.naissance, a_la_date)
    if adherent.entreprise is not None:
        return [adherent.entreprise.police_code_produit]
    produits = ["SI-ESSENTIEL", "SI-EQUILIBRE", "PV-DECES", "PV-ARRET"]
    if age >= 55:
        produits.append("SI-SENIOR")
    return produits


def _genere_contrats_et_avenants(
    alea: Random, ref: Referentiel, adherents: list[_Adherent], date_reference: date
) -> tuple[list[ContratGenere], list[dict], int]:
    """Construit les contrats et toute leur histoire d'avenants."""
    contrats: list[ContratGenere] = []
    avenants: list[dict] = []
    identifiant_contrat = 0
    identifiant_avenant = 0
    compteur_par_annee: dict[tuple[str, int], int] = {}

    for adherent in adherents:
        nb_contrats = 2 if (adherent.entreprise is None and alea.random() < PART_SECOND_CONTRAT) else 1
        produits_pris: list[str] = []
        for _ in range(nb_contrats):
            eligibles = _produits_eligibles(ref, adherent, adherent.date_creation)
            candidats = [p for p in eligibles if p not in produits_pris]
            if not candidats:
                break
            code_produit = alea.choice(candidats)
            produits_pris.append(code_produit)

            identifiant_contrat += 1
            identifiant_avenant, nouveaux = _genere_un_contrat(
                alea=alea,
                ref=ref,
                adherent=adherent,
                code_produit=code_produit,
                identifiant_contrat=identifiant_contrat,
                identifiant_avenant_depart=identifiant_avenant,
                compteur_par_annee=compteur_par_annee,
                date_reference=date_reference,
                contrats=contrats,
            )
            avenants.extend(nouveaux)

    return contrats, avenants, len(avenants)


def _genere_un_contrat(
    *,
    alea: Random,
    ref: Referentiel,
    adherent: _Adherent,
    code_produit: str,
    identifiant_contrat: int,
    identifiant_avenant_depart: int,
    compteur_par_annee: dict[tuple[str, int], int],
    date_reference: date,
    contrats: list[ContratGenere],
) -> tuple[int, list[dict]]:
    """Genere un contrat et la suite de ses avenants, de la souscription a son etat au jour de reference."""
    identifiant_avenant = identifiant_avenant_depart
    avenants: list[dict] = []
    # Date d'effet du dernier avenant emis, qui ne peut jamais reculer.
    derniere_date_effet = date.min
    code_branche = ref.code_branche_de(code_produit)

    date_souscription = adherent.date_creation
    police_id = None
    if adherent.entreprise is not None:
        police_id = adherent.entreprise.police_id
        # Une affiliation ne precede jamais la police a laquelle elle se rattache.
        date_souscription = max(date_souscription, adherent.entreprise.police_date_effet)
    annee = date_souscription.year
    cle = (code_branche, annee)
    compteur_par_annee[cle] = compteur_par_annee.get(cle, 0) + 1
    numero_contrat = f"{code_branche}-{annee}-{compteur_par_annee[cle]:05d}"

    niveau = alea.choice((1, 1, 2, 2, 2, 3))
    formule = ref.formules[code_produit][niveau - 1]
    indice_apporteur = alea.randrange(len(ref.apporteurs))
    apporteur = ref.apporteurs[indice_apporteur]
    fractionnement = Fractionnement(tire_pondere(alea, POIDS_FRACTIONNEMENT))
    age_souscription = age_a_la_date(adherent.naissance, date_souscription)
    cotisation = _cotisation_initiale(alea, ref, code_produit, niveau, age_souscription)

    def ajoute_avenant(
        *,
        ordre: int,
        type_avenant: TypeAvenant,
        date_effet: date,
        statut: StatutContrat,
        formule_id: int,
        apporteur_id: int,
        cotisation_cents: int,
        motif: MotifResiliation | None = None,
        commentaire: str | None = None,
    ) -> None:
        nonlocal identifiant_avenant, derniere_date_effet
        # Invariant tenu ici plutot qu'au cas par cas : les dates d'effet croissent avec le numero
        # d'ordre. C'est la regle que `services.contrats.ajoute_avenant` applique aux saisies, et un
        # historique qui ne la respecte pas rend la reconstitution a date ambigue. Un generateur qui
        # produit des donnees invalides est pire qu'un generateur qui s'arrete.
        if date_effet < derniere_date_effet:
            raise ValueError(
                f"avenant hors ordre sur le contrat {numero_contrat} : rang {ordre} a effet"
                f" {date_effet} alors que le precedent prend effet {derniere_date_effet}"
            )
        derniere_date_effet = date_effet
        identifiant_avenant += 1
        avenants.append(
            {
                "id": identifiant_avenant,
                "contrat_id": identifiant_contrat,
                "numero_ordre": ordre,
                "type_avenant": type_avenant,
                "date_effet": date_effet,
                "date_saisie": date_effet,
                "auteur": "generateur",
                "statut": statut,
                "formule_id": formule_id,
                "apporteur_id": apporteur_id,
                "cotisation_mensuelle_cents": cotisation_cents,
                "fractionnement": fractionnement,
                "motif_resiliation": motif,
                "commentaire": commentaire,
            }
        )

    # Avenant d'ordre zero : la souscription. Le contrat est un devis, il n'a pas encore pris effet.
    ajoute_avenant(
        ordre=0,
        type_avenant=TypeAvenant.SOUSCRIPTION,
        date_effet=date_souscription,
        statut=StatutContrat.DEVIS,
        formule_id=formule.id,
        apporteur_id=apporteur.id,
        cotisation_cents=cotisation,
        commentaire="Souscription, donnée de simulation",
    )

    # Une partie des devis ne se transforme jamais. Ils restent au statut devis : un devis sans suite
    # n'est pas une resiliation, puisqu'il n'a jamais pris effet.
    if alea.random() < PART_DEVIS_SANS_SUITE:
        contrats.append(
            ContratGenere(
                id=identifiant_contrat,
                numero_contrat=numero_contrat,
                adherent_id=adherent.id,
                adherent_naissance=adherent.naissance,
                produit_code=code_produit,
                produit_id=ref.produits[code_produit].id,
                entreprise_id=adherent.entreprise.id if adherent.entreprise else None,
                police_id=police_id,
                statut=StatutContrat.DEVIS,
                date_souscription=date_souscription,
                date_effet=None,
                date_resiliation=None,
                fractionnement=fractionnement,
                paliers_cotisation=[],
                paliers_apporteur=[],
                suspensions=[],
                paliers_formule=[],
                formule_id_courante=formule.id,
                apporteur_id_courant=apporteur.id,
                cotisation_courante=cotisation,
                motif_resiliation_courante=None,
            )
        )
        return identifiant_avenant, avenants

    # Mise en vigueur au premier jour du mois suivant la souscription : c'est la pratique courante,
    # la garantie et le quittancement commencent sur un mois entier.
    date_effet = ajoute_mois(date_souscription.replace(day=1), 1)
    ordre = 1
    ajoute_avenant(
        ordre=ordre,
        type_avenant=TypeAvenant.MISE_EN_VIGUEUR,
        date_effet=date_effet,
        statut=StatutContrat.EN_COURS,
        formule_id=formule.id,
        apporteur_id=apporteur.id,
        cotisation_cents=cotisation,
    )

    paliers_cotisation: list[tuple[date, int]] = [(date_effet, cotisation)]
    paliers_apporteur: list[tuple[date, int]] = [(date_effet, apporteur.id)]
    paliers_formule: list[tuple[date, int]] = [(date_effet, formule.id)]
    suspensions: list[tuple[date, date]] = []
    statut = StatutContrat.EN_COURS
    formule_courante = formule
    apporteur_courant = apporteur
    date_resiliation: date | None = None
    motif_final: MotifResiliation | None = None
    derniere_revalorisation_bp = 0

    # Deroulement annee par annee, a chaque echeance du 1er janvier.
    for annee_echeance in sorted(REVALORISATION_BP_PAR_ANNEE):
        echeance = date(annee_echeance, 1, 1)
        if echeance <= date_effet or echeance > date_reference:
            continue
        if statut is StatutContrat.RESILIE:
            break

        borne_basse, borne_haute = REVALORISATION_BP_PAR_ANNEE[annee_echeance]
        revalorisation_bp = alea.randrange(borne_basse, borne_haute + 1, 25)
        cotisation = cotisation + cotisation * revalorisation_bp // 10_000
        derniere_revalorisation_bp = revalorisation_bp
        ordre += 1
        ajoute_avenant(
            ordre=ordre,
            type_avenant=TypeAvenant.REVALORISATION_TARIFAIRE,
            date_effet=echeance,
            statut=statut,
            formule_id=formule_courante.id,
            apporteur_id=apporteur_courant.id,
            cotisation_cents=cotisation,
            commentaire=f"Revalorisation à l'échéance, {revalorisation_bp / 100:.2f} pour cent",
        )
        paliers_cotisation.append((echeance, cotisation))

        # Changement de formule demande par l'adherent, en cours d'annee.
        # La date est arretee **avant** toute mutation : si l'avenant ne peut pas etre emis, l'etat du
        # contrat ne doit pas bouger. Autrement, les avenants suivants porteraient une formule ou un
        # tarif que plus aucun avenant n'explique.
        if alea.random() < PART_CHANGEMENT_FORMULE:
            niveaux_possibles = [f for f in ref.formules[code_produit] if f.id != formule_courante.id]
            nouvelle_formule = alea.choice(niveaux_possibles)
            nouvelle_cotisation = _cotisation_initiale(
                alea, ref, code_produit, nouvelle_formule.niveau,
                age_a_la_date(adherent.naissance, echeance),
            )
            date_changement = ajoute_mois(echeance, alea.randint(1, 10))
            if date_changement <= date_reference and date_changement >= derniere_date_effet:
                formule_courante = nouvelle_formule
                cotisation = nouvelle_cotisation
                ordre += 1
                ajoute_avenant(
                    ordre=ordre,
                    type_avenant=TypeAvenant.CHANGEMENT_FORMULE,
                    date_effet=date_changement,
                    statut=statut,
                    formule_id=formule_courante.id,
                    apporteur_id=apporteur_courant.id,
                    cotisation_cents=cotisation,
                    commentaire=f"Passage en {formule_courante.libelle}",
                )
                paliers_cotisation.append((date_changement, cotisation))
                paliers_formule.append((date_changement, formule_courante.id))

        # Reprise du portefeuille par un autre apporteur.
        if alea.random() < PART_CHANGEMENT_APPORTEUR:
            autres = [a for a in ref.apporteurs if a.id != apporteur_courant.id]
            nouvel_apporteur = alea.choice(autres)
            date_changement = ajoute_mois(echeance, alea.randint(1, 11))
            if date_changement <= date_reference and date_changement >= derniere_date_effet:
                apporteur_courant = nouvel_apporteur
                indice_apporteur = ref.apporteurs.index(apporteur_courant)
                ordre += 1
                ajoute_avenant(
                    ordre=ordre,
                    type_avenant=TypeAvenant.CHANGEMENT_APPORTEUR,
                    date_effet=date_changement,
                    statut=statut,
                    formule_id=formule_courante.id,
                    apporteur_id=apporteur_courant.id,
                    cotisation_cents=cotisation,
                    commentaire=f"Reprise du portefeuille par {apporteur_courant.nom}",
                )
                paliers_apporteur.append((date_changement, apporteur_courant.id))

        # Suspension de garanties pour non-paiement, puis remise en vigueur apres regularisation.
        if statut is StatutContrat.EN_COURS and alea.random() < PART_SUSPENSION:
            debut_suspension = ajoute_mois(echeance, alea.randint(2, 8))
            if debut_suspension <= date_reference and debut_suspension >= derniere_date_effet:
                ordre += 1
                statut = StatutContrat.SUSPENDU
                ajoute_avenant(
                    ordre=ordre,
                    type_avenant=TypeAvenant.SUSPENSION,
                    date_effet=debut_suspension,
                    statut=statut,
                    formule_id=formule_courante.id,
                    apporteur_id=apporteur_courant.id,
                    cotisation_cents=cotisation,
                    commentaire="Suspension des garanties après mise en demeure, cotisations impayées",
                )
                # Trois suspensions sur quatre se regularisent, la quatrieme finit en resiliation.
                #
                # Dans les deux branches, la sortie de suspension peut tomber **apres** la fin de
                # l'historique simule. Le contrat reste alors suspendu au 30/09/2026, et la suspension
                # reste ouverte. Ce cas etait le defaut : la periode n'etait pas enregistree, le
                # generateur de prestations n'avait donc rien a ecarter, et des remboursements etaient
                # servis pendant une suspension en cours. Six prestations, dont une reglee, sur les
                # contrats concernes. La borne `FIN_SUSPENSION_OUVERTE` ferme ce trou.
                if alea.random() < 0.75:
                    fin_suspension = ajoute_mois(debut_suspension, alea.randint(1, 3))
                    if fin_suspension <= date_reference and fin_suspension >= derniere_date_effet:
                        ordre += 1
                        statut = StatutContrat.EN_COURS
                        ajoute_avenant(
                            ordre=ordre,
                            type_avenant=TypeAvenant.REMISE_EN_VIGUEUR,
                            date_effet=fin_suspension,
                            statut=statut,
                            formule_id=formule_courante.id,
                            apporteur_id=apporteur_courant.id,
                            cotisation_cents=cotisation,
                            commentaire="Remise en vigueur après encaissement de l'arriéré",
                        )
                        suspensions.append((debut_suspension, fin_suspension))
                    else:
                        suspensions.append((debut_suspension, FIN_SUSPENSION_OUVERTE))
                else:
                    date_resiliation = ajoute_mois(debut_suspension, 2)
                    if date_resiliation <= date_reference and date_resiliation >= derniere_date_effet:
                        ordre += 1
                        statut = StatutContrat.RESILIE
                        motif_final = MotifResiliation.NON_PAIEMENT
                        ajoute_avenant(
                            ordre=ordre,
                            type_avenant=TypeAvenant.RESILIATION,
                            date_effet=date_resiliation,
                            statut=statut,
                            formule_id=formule_courante.id,
                            apporteur_id=apporteur_courant.id,
                            cotisation_cents=cotisation,
                            motif=motif_final,
                            commentaire="Résiliation pour non-paiement, après suspension",
                        )
                        suspensions.append((debut_suspension, date_resiliation))
                        break
                    date_resiliation = None
                    suspensions.append((debut_suspension, FIN_SUSPENSION_OUVERTE))

        # Resiliation a l'echeance suivante, selon les facteurs documentes.
        if statut is not StatutContrat.RESILIE:
            anciennete = mois_entre(date_effet, echeance)
            age = age_a_la_date(adherent.naissance, echeance)
            probabilite = _probabilite_resiliation(
                derniere_revalorisation_bp, anciennete, age, indice_apporteur
            )
            if alea.random() < probabilite:
                motif = MotifResiliation(tire_pondere(alea, POIDS_MOTIF_RESILIATION))
                # La resiliation infra-annuelle est la faculte de sortir **apres** la premiere annee.
                # L'employer avant douze mois d'anciennete serait une contradiction dans les termes.
                # Avant ce seuil, la sortie prend le motif d'un changement de situation.
                if (
                    motif is MotifResiliation.INFRA_ANNUELLE
                    and anciennete < ANCIENNETE_MINIMALE_INFRA_ANNUELLE_MOIS
                ):
                    motif = MotifResiliation.CHANGEMENT_SITUATION
                # Une resiliation a l'echeance prend effet a l'echeance suivante, apres preavis. Les
                # autres motifs prennent effet en cours d'annee.
                if motif is MotifResiliation.ECHEANCE:
                    candidate = ajoute_mois(echeance, 12)
                else:
                    candidate = ajoute_mois(echeance, alea.randint(PREAVIS_ECHEANCE_MOIS, 11))
                if candidate <= date_reference and candidate > date_effet and candidate >= derniere_date_effet:
                    ordre += 1
                    statut = StatutContrat.RESILIE
                    date_resiliation = candidate
                    motif_final = motif
                    ajoute_avenant(
                        ordre=ordre,
                        type_avenant=TypeAvenant.RESILIATION,
                        date_effet=candidate,
                        statut=statut,
                        formule_id=formule_courante.id,
                        apporteur_id=apporteur_courant.id,
                        cotisation_cents=cotisation,
                        motif=motif_final,
                        commentaire=f"Résiliation, motif {motif.value.replace('_', ' ')}",
                    )
                    break

    # Le dernier avenant genere porte l'etat courant du contrat : c'est lui qui alimente le cache de la
    # table `contrat`. Le lire ici, plutot que de recopier les variables de boucle, evite qu'un ajout
    # d'avenant futur oublie de mettre le cache a jour.
    dernier = avenants[-1]
    contrats.append(
        ContratGenere(
            id=identifiant_contrat,
            numero_contrat=numero_contrat,
            adherent_id=adherent.id,
            adherent_naissance=adherent.naissance,
            produit_code=code_produit,
            produit_id=ref.produits[code_produit].id,
            entreprise_id=adherent.entreprise.id if adherent.entreprise else None,
            police_id=police_id,
            statut=dernier["statut"],
            date_souscription=date_souscription,
            date_effet=date_effet,
            date_resiliation=date_resiliation,
            fractionnement=fractionnement,
            paliers_cotisation=paliers_cotisation,
            paliers_apporteur=paliers_apporteur,
            suspensions=suspensions,
            paliers_formule=paliers_formule,
            formule_id_courante=dernier["formule_id"],
            apporteur_id_courant=dernier["apporteur_id"],
            cotisation_courante=dernier["cotisation_mensuelle_cents"],
            motif_resiliation_courante=dernier["motif_resiliation"],
        )
    )
    return identifiant_avenant, avenants


def _ligne_contrat(contrat: ContratGenere) -> dict:
    """Ligne d'insertion du contrat, avec son cache d'etat courant.

    Le cache est deduit du dernier avenant, par construction. Le test
    `test_cache_contrat_egale_reconstitution` le verifie ensuite sur l'ensemble du portefeuille, en
    passant par la reconstitution, c'est-a-dire par un chemin different de celui qui l'a produit.
    """
    return {
        "id": contrat.id,
        "numero_contrat": contrat.numero_contrat,
        "adherent_id": contrat.adherent_id,
        "entreprise_id": contrat.entreprise_id,
        "police_id": contrat.police_id,
        "produit_id": contrat.produit_id,
        "statut": contrat.statut,
        "date_souscription": contrat.date_souscription,
        "date_effet": contrat.date_effet,
        "date_resiliation": contrat.date_resiliation,
        "motif_resiliation": contrat.motif_resiliation_courante,
        "formule_id": contrat.formule_id_courante,
        "apporteur_id": contrat.apporteur_id_courant,
        "cotisation_mensuelle_cents": contrat.cotisation_courante,
        "fractionnement": contrat.fractionnement,
        "cree_le": datetime.combine(contrat.date_souscription, time(9, 0)),
        "maj_le": datetime.combine(contrat.date_souscription, time(9, 0)),
    }
