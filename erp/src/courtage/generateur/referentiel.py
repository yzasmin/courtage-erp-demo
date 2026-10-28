"""Generation du referentiel : branches, compagnies, produits, garanties, formules, apporteurs,
taux dates et conventions de delegation de gestion.

Les compagnies, les produits et les apporteurs sont **fictifs**. Les noms sont construits pour sonner
juste dans le bassin d'activite simule, en Occitanie, sans correspondre a aucune entite reelle.

Le petit volume de ce referentiel justifie de passer par l'ORM : quelques dizaines de lignes, dont les
identifiants servent ensuite a tout le reste de la generation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from random import Random

from sqlalchemy.orm import Session

from courtage.domaine.montants import TAUX_PLEIN_BP
from courtage.generateur.parametres import (
    BORNES_COMMISSION_BP,
    BORNES_RETROCESSION_BP,
    NB_APPORTEURS,
    TAUX_TAXE_BP,
)
from courtage.modeles.delegation import ActeDelegue, ConventionDelegation, ConventionProduit
from courtage.modeles.enums import ActeGestion, PorteeDelegation, TypeApporteur
from courtage.modeles.referentiel import (
    Apporteur,
    Branche,
    Compagnie,
    Formule,
    FormuleGarantie,
    Garantie,
    Produit,
    TauxCommission,
    TauxRetrocession,
    TauxTaxe,
)

#: Le referentiel est ouvert avant le debut de l'historique : un produit se commercialise avant d'avoir
#: son premier contrat.
OUVERTURE_REFERENTIEL = date(2020, 1, 1)

BRANCHES = (
    ("SI", "Santé individuelle", "personnes"),
    ("SC", "Santé collective", "personnes"),
    ("PV", "Prévoyance", "personnes"),
)

COMPAGNIES = (
    ("OCE", "Oceane Assurances", "412587963"),
    ("PYR", "Pyrene Mutuelle", "389654127"),
    ("LIT", "Littoral Prevoyance", "445123789"),
)

#: (code, libelle, code branche, code compagnie)
PRODUITS = (
    ("SI-ESSENTIEL", "Santé Essentiel, particuliers", "SI", "OCE"),
    ("SI-EQUILIBRE", "Santé Équilibre, particuliers", "SI", "PYR"),
    ("SI-SENIOR", "Santé Senior, plus de 55 ans", "SI", "OCE"),
    ("SC-TPE", "Santé collective, très petites entreprises", "SC", "PYR"),
    ("SC-CADRES", "Santé collective, cadres", "SC", "OCE"),
    ("SC-ARTISANS", "Santé collective, artisans et indépendants", "SC", "PYR"),
    ("PV-DECES", "Prévoyance décès et rente éducation", "PV", "LIT"),
    ("PV-ARRET", "Prévoyance incapacité et invalidité", "PV", "LIT"),
)

#: (code, libelle, famille, rang). Catalogue global : c'est la formule qui dira lesquelles s'appliquent.
GARANTIES = (
    ("SOINS", "Soins courants", "sante", 10),
    ("HOSPI", "Hospitalisation", "sante", 20),
    ("OPTIQUE", "Optique", "sante", 30),
    ("DENTAIRE", "Dentaire", "sante", 40),
    ("AUDIO", "Audiologie", "sante", 50),
    ("DECES", "Capital deces", "prevoyance", 60),
    ("RENTE-EDU", "Rente education", "prevoyance", 70),
    ("INCAP", "Incapacite temporaire de travail", "prevoyance", 80),
    ("INVAL", "Invalidite permanente", "prevoyance", 90),
)

#: Garanties couvertes par branche, dans l'ordre d'affichage.
GARANTIES_PAR_BRANCHE = {
    "SI": ("SOINS", "HOSPI", "OPTIQUE", "DENTAIRE", "AUDIO"),
    "SC": ("SOINS", "HOSPI", "OPTIQUE", "DENTAIRE", "AUDIO"),
    "PV": ("DECES", "RENTE-EDU", "INCAP", "INVAL"),
}

#: (code, libelle, niveau). Trois niveaux de couverture par produit.
NIVEAUX_FORMULE = (
    ("F1", "Formule 1, socle", 1),
    ("F2", "Formule 2, renforcée", 2),
    ("F3", "Formule 3, confort", 3),
)

#: Taux de remboursement et plafond annuel par garantie et par niveau. Hypotheses de simulation.
#: Le plafond vaut None quand la garantie n'est pas plafonnee, par exemple l'hospitalisation.
COUVERTURES = {
    "SOINS": ((10_000, None), (12_500, None), (15_000, None)),
    "HOSPI": ((10_000, None), (15_000, None), (20_000, None)),
    "OPTIQUE": ((10_000, 15_000), (15_000, 30_000), (20_000, 50_000)),
    "DENTAIRE": ((10_000, 40_000), (15_000, 90_000), (22_500, 200_000)),
    "AUDIO": ((10_000, 25_000), (12_500, 60_000), (17_500, 100_000)),
    "DECES": ((10_000, 2_000_000), (10_000, 5_000_000), (10_000, 10_000_000)),
    "RENTE-EDU": ((10_000, 300_000), (10_000, 600_000), (10_000, 1_200_000)),
    "INCAP": ((5_000, None), (7_000, None), (8_000, None)),
    "INVAL": ((5_000, None), (7_500, None), (10_000, None)),
}

#: Apporteurs du reseau. (code, nom, type, departement). Tous fictifs.
APPORTEURS = (
    ("AP01", "Cabinet Vidal Courtage", "mandataire", "34"),
    ("AP02", "Occitane Patrimoine", "mandataire", "31"),
    ("AP03", "Reseau Garrigue Assurances", "sous_courtier", "30"),
    ("AP04", "Comparassur en ligne", "comparateur", "34"),
    ("AP05", "Mireille Fabre, agent general", "mandataire", "11"),
    ("AP06", "Littoral Conseil Entreprises", "sous_courtier", "66"),
    ("AP07", "Cevennes Prevoyance Conseil", "mandataire", "48"),
    ("AP08", "Devis-Sante.fr", "comparateur", "31"),
    ("AP09", "Production directe, equipe commerciale", "direct", "34"),
    ("AP10", "Production directe, guichet en ligne", "direct", "34"),
)


@dataclass
class Referentiel:
    """Identifiants du referentiel, passes au reste de la generation."""

    branches: dict[str, Branche] = field(default_factory=dict)
    compagnies: dict[str, Compagnie] = field(default_factory=dict)
    produits: dict[str, Produit] = field(default_factory=dict)
    garanties: dict[str, Garantie] = field(default_factory=dict)
    #: Formules par code de produit, dans l'ordre des niveaux.
    formules: dict[str, list[Formule]] = field(default_factory=dict)
    apporteurs: list[Apporteur] = field(default_factory=list)
    #: Codes des produits dont la gestion des prestations n'est pas deleguee au cabinet.
    produits_sans_delegation_prestations: tuple[str, ...] = ()

    def produits_de_branche(self, code_branche: str) -> list[Produit]:
        return [p for code, p in self.produits.items() if self.code_branche_de(code) == code_branche]

    def code_branche_de(self, code_produit: str) -> str:
        for code, _libelle, code_branche, _compagnie in PRODUITS:
            if code == code_produit:
                return code_branche
        raise KeyError(code_produit)


def genere_referentiel(session: Session, alea: Random, date_reference: date) -> Referentiel:
    """Cree le referentiel complet et retourne les identifiants utiles a la suite."""
    ref = Referentiel()

    for code, libelle, famille in BRANCHES:
        branche = Branche(code=code, libelle=libelle, famille=famille)
        session.add(branche)
        ref.branches[code] = branche

    for code, raison_sociale, siren in COMPAGNIES:
        compagnie = Compagnie(code=code, raison_sociale=raison_sociale, siren=siren, actif=True)
        session.add(compagnie)
        ref.compagnies[code] = compagnie

    for code, libelle, famille, rang in GARANTIES:
        garantie = Garantie(code=code, libelle=libelle, famille=famille, rang=rang)
        session.add(garantie)
        ref.garanties[code] = garantie

    session.flush()

    for code, libelle, code_branche, code_compagnie in PRODUITS:
        produit = Produit(
            code=code,
            libelle=libelle,
            branche_id=ref.branches[code_branche].id,
            compagnie_id=ref.compagnies[code_compagnie].id,
            date_ouverture=OUVERTURE_REFERENTIEL,
            date_fermeture=None,
        )
        session.add(produit)
        ref.produits[code] = produit
    session.flush()

    _genere_formules(session, alea, ref)
    _genere_taux_taxe(session, ref)
    _genere_apporteurs(session, ref)
    session.flush()
    _genere_taux_commission(session, alea, ref)
    _genere_taux_retrocession(session, alea, ref)
    _genere_conventions_delegation(session, ref)
    session.flush()
    return ref


def _genere_formules(session: Session, alea: Random, ref: Referentiel) -> None:
    """Trois formules par produit, avec leur tableau de garanties."""
    for code_produit in sorted(ref.produits):
        produit = ref.produits[code_produit]
        code_branche = ref.code_branche_de(code_produit)
        formules: list[Formule] = []
        # Tarif de base croissant avec le niveau. Le pas depend du produit, tire une fois.
        tarif_socle = alea.randrange(2_600, 7_400, 50)
        pas = alea.randrange(900, 2_600, 50)
        for index, (code, libelle, niveau) in enumerate(NIVEAUX_FORMULE):
            formule = Formule(
                produit_id=produit.id,
                code=code,
                libelle=libelle,
                niveau=niveau,
                tarif_base_mensuel_cents=tarif_socle + index * pas,
            )
            session.add(formule)
            formules.append(formule)
        session.flush()

        for index, formule in enumerate(formules):
            for code_garantie in GARANTIES_PAR_BRANCHE[code_branche]:
                taux_bp, plafond = COUVERTURES[code_garantie][index]
                session.add(
                    FormuleGarantie(
                        formule_id=formule.id,
                        garantie_id=ref.garanties[code_garantie].id,
                        libelle_couverture=_libelle_couverture(code_garantie, taux_bp, plafond),
                        taux_remboursement_bp=taux_bp,
                        plafond_annuel_cents=plafond,
                    )
                )
        ref.formules[code_produit] = formules


#: Assiette de calcul de chaque garantie, qui determine la facon dont le niveau de couverture s'ecrit.
#: Ce n'est pas un detail de redaction : ecrire « 125 % des frais reels » annonce un remboursement
#: superieur a la depense, ce qui n'existe pas. En sante, le pourcentage porte sur la **base de
#: remboursement** de l'assurance maladie, et il peut donc depasser 100 %. En prevoyance, il porte sur
#: le **salaire de reference**, et la garantie deces s'exprime en capital, pas en pourcentage.
ASSIETTE_GARANTIE = {
    "SOINS": "base_remboursement",
    "HOSPI": "base_remboursement",
    "OPTIQUE": "base_remboursement",
    "DENTAIRE": "base_remboursement",
    "AUDIO": "base_remboursement",
    "DECES": "capital",
    "RENTE-EDU": "rente",
    "INCAP": "salaire",
    "INVAL": "salaire",
}


def _libelle_couverture(code_garantie: str, taux_bp: int, plafond_cents: int | None) -> str:
    """Libelle lisible du niveau de couverture, tel qu'un tableau de garanties l'affiche.

    Un gestionnaire relit ce libelle tous les jours, et un adherent le retrouve sur son tableau de
    garanties. Il doit dire exactement sur quoi porte le pourcentage.
    """
    assiette = ASSIETTE_GARANTIE[code_garantie]
    pourcentage = taux_bp // 100

    if assiette == "capital":
        return f"capital garanti {plafond_cents // 100} EUR" if plafond_cents else "capital garanti"
    if assiette == "rente":
        return (
            f"rente annuelle par enfant, {plafond_cents // 100} EUR"
            if plafond_cents
            else "rente education"
        )
    if assiette == "salaire":
        complement = "en indemnites journalieres" if code_garantie == "INCAP" else "en rente"
        return f"{pourcentage} % du salaire de reference, {complement}"

    # Sante : le pourcentage porte sur la base de remboursement de l'assurance maladie.
    if plafond_cents is None:
        return f"{pourcentage} % de la base de remboursement"
    return f"{pourcentage} % de la base de remboursement, plafond annuel {plafond_cents // 100} EUR"


def _genere_taux_taxe(session: Session, ref: Referentiel) -> None:
    """Un taux de taxe par branche, en vigueur sur toute la periode simulee.

    Une seule periode de validite ici : ce qui doit etre demontre est que le calcul va chercher le taux
    a la date d'echeance. Le module 2 ajoutera un changement de taux au 1er janvier pour l'eprouver.
    """
    for code_branche, taux_bp in sorted(TAUX_TAXE_BP.items()):
        session.add(
            TauxTaxe(
                branche_id=ref.branches[code_branche].id,
                libelle=f"Taxe sur les cotisations, branche {code_branche}, hypothèse de simulation",
                taux_bp=taux_bp,
                date_debut_validite=OUVERTURE_REFERENTIEL,
                date_fin_validite=None,
            )
        )


def _genere_apporteurs(session: Session, ref: Referentiel) -> None:
    """Les dix apporteurs du reseau. Deux d'entre eux portent un portefeuille plus volatil."""
    for index, (code, nom, type_apporteur, departement) in enumerate(APPORTEURS[:NB_APPORTEURS]):
        apporteur = Apporteur(
            code=code,
            nom=nom,
            type_apporteur=TypeApporteur(type_apporteur),
            # Numero d'immatriculation fictif. La production directe n'en a pas : l'affaire arrive sans
            # intermediaire, donc sous le mandat
            # du cabinet, pas sous le sien.
            numero_orias=None if type_apporteur == "direct" else f"1{index:07d}",
            departement=departement,
            date_entree=OUVERTURE_REFERENTIEL,
            date_sortie=None,
            actif=True,
        )
        session.add(apporteur)
        ref.apporteurs.append(apporteur)


def _genere_taux_commission(session: Session, alea: Random, ref: Referentiel) -> None:
    """Un taux de commission par produit, avec un changement de bareme au 1er janvier 2025.

    Le changement de bareme est volontaire : il prouve que le calcul retient le taux en vigueur a la date
    d'echeance, et non le taux courant. Un bordereau de 2024 relu aujourd'hui doit retrouver l'ancien
    taux.
    """
    bascule = date(2025, 1, 1)
    for code_produit in sorted(ref.produits):
        produit = ref.produits[code_produit]
        borne_basse, borne_haute = BORNES_COMMISSION_BP[ref.code_branche_de(code_produit)]
        taux_initial = alea.randrange(borne_basse, borne_haute + 1, 25)
        # Le nouveau bareme s'ecarte de l'ancien de quelques dixiemes de point, dans un sens ou l'autre.
        taux_revise = min(borne_haute, max(borne_basse, taux_initial + alea.choice((-75, -50, 25, 50, 75))))
        session.add(
            TauxCommission(
                produit_id=produit.id,
                taux_bp=taux_initial,
                assiette="cotisation_encaissee",
                date_debut_validite=OUVERTURE_REFERENTIEL,
                date_fin_validite=bascule - timedelta(days=1),
            )
        )
        session.add(
            TauxCommission(
                produit_id=produit.id,
                taux_bp=taux_revise,
                assiette="cotisation_encaissee",
                date_debut_validite=bascule,
                date_fin_validite=None,
            )
        )


def _genere_taux_retrocession(session: Session, alea: Random, ref: Referentiel) -> None:
    """Un accord cadre par apporteur, et une derogation par produit pour deux d'entre eux.

    La derogation existe pour que la regle de priorite du domaine, le produit precis devant l'accord
    cadre, soit exercee par les donnees et pas seulement par un test unitaire.
    """
    taux_cadre_par_apporteur: dict[str, int] = {}
    for apporteur in ref.apporteurs:
        borne_basse, borne_haute = BORNES_RETROCESSION_BP[apporteur.type_apporteur.value]
        taux_cadre = borne_basse if borne_basse == borne_haute else alea.randrange(borne_basse, borne_haute + 1, 50)
        taux_cadre_par_apporteur[apporteur.code] = taux_cadre
        session.add(
            TauxRetrocession(
                apporteur_id=apporteur.id,
                produit_id=None,
                taux_bp=taux_cadre,
                date_debut_validite=OUVERTURE_REFERENTIEL,
                date_fin_validite=None,
            )
        )

    # La derogation est calculee **a partir du taux cadre tire**, et non des bornes du type d'apporteur.
    # Autrement, un tirage haut du cadre pouvait donner une derogation moins avantageuse que lui, ce qui
    # n'a aucun sens : on ne deroge a un accord que pour l'ameliorer. Le defaut etait invisible avec la
    # graine des tests et apparaissait avec celle de la demonstration.
    for code_apporteur, code_produit in (("AP03", "PV-DECES"), ("AP06", "SC-CADRES")):
        apporteur = next(a for a in ref.apporteurs if a.code == code_apporteur)
        session.add(
            TauxRetrocession(
                apporteur_id=apporteur.id,
                produit_id=ref.produits[code_produit].id,
                taux_bp=min(TAUX_PLEIN_BP, taux_cadre_par_apporteur[apporteur.code] + 500),
                date_debut_validite=OUVERTURE_REFERENTIEL,
                date_fin_validite=None,
            )
        )


def _genere_conventions_delegation(session: Session, ref: Referentiel) -> None:
    """Une convention de delegation par compagnie, avec des perimetres volontairement differents.

    C'est la partie du referentiel qui distingue un courtier **delegataire** d'un simple apporteur
    d'affaires, et elle est construite pour que le refus soit visible et non theorique :

    - **Oceane delegue tout** : affiliation, avenants, radiation, quittancement, encaissement et gestion
      des prestations. C'est le cas d'une delegation totale.
    - **Pyrene delegue tout sauf l'encaissement.** Le cabinet affilie, quittance et regle les
      prestations, mais c'est la compagnie qui preleve les cotisations. Cas courant d'une gestion
      partagee : aucun encaissement n'existe dans l'ERP pour ces trois produits.
    - **Littoral, en prevoyance, ne delegue ni les prestations ni les sinistres.** Un sinistre de
      prevoyance suppose une expertise medicale et une appreciation du risque, que l'assureur garde
      presque toujours. Les contrats de prevoyance n'ont donc aucune prestation dans cet ERP, et toute
      tentative d'en enregistrer une est refusee par `domaine.delegation`.

    - **Un quatrieme cas, volontairement visible dans les donnees** : la convention du produit
      PV-ARRET est arrivee a echeance le 30 juin 2026 et n'a pas ete renouvelee. Depuis cette date,
      aucun acte n'y est delegue : le quittancement s'arrete, et toute saisie sur un contrat de ce
      produit est refusee. C'est frequent dans la vraie vie, un renouvellement se signe souvent en
      retard, et c'est ce qui rend le controle constatable plutot que seulement codé.

    Un relecteur du metier peut donc verifier, sur la fiche d'un contrat de prevoyance, que
    l'enregistrement d'une prestation est indisponible, et sur un contrat PV-ARRET que plus aucun acte
    n'est possible depuis juillet 2026, avec le motif affiche.
    """
    perimetres = {
        "OCE": (
            PorteeDelegation.TOTALE,
            (
                ActeGestion.AFFILIATION,
                ActeGestion.AVENANT,
                ActeGestion.RADIATION,
                ActeGestion.QUITTANCEMENT,
                ActeGestion.ENCAISSEMENT,
                ActeGestion.GESTION_PRESTATIONS,
            ),
        ),
        "PYR": (
            PorteeDelegation.PARTIELLE,
            (
                ActeGestion.AFFILIATION,
                ActeGestion.AVENANT,
                ActeGestion.RADIATION,
                ActeGestion.QUITTANCEMENT,
                ActeGestion.GESTION_PRESTATIONS,
            ),
        ),
        "LIT": (
            PorteeDelegation.PARTIELLE,
            (
                ActeGestion.AFFILIATION,
                ActeGestion.AVENANT,
                ActeGestion.RADIATION,
                ActeGestion.QUITTANCEMENT,
                ActeGestion.ENCAISSEMENT,
            ),
        ),
    }

    #: Convention arrivee a echeance et non renouvelee, sur un seul produit.
    #: C'est un cas de demonstration **volontaire**, et il est frequent dans la vraie vie : une
    #: convention se renouvelle par avenant annuel, et le renouvellement prend parfois du retard. Tant
    #: qu'il n'est pas signe, le cabinet n'a plus mandat, et l'ERP doit refuser. Sans ce cas, le
    #: controle de delegation existerait dans le code sans jamais se declencher sur les donnees, et un
    #: lecteur ne pourrait pas le constater.
    FIN_CONVENTION_NON_RENOUVELEE = date(2026, 6, 30)
    PRODUIT_CONVENTION_ECHUE = "PV-ARRET"

    sans_prestations: list[str] = []
    for code_compagnie in sorted(perimetres):
        portee, actes = perimetres[code_compagnie]
        convention = ConventionDelegation(
            reference=f"CONV-{code_compagnie}-2020",
            compagnie_id=ref.compagnies[code_compagnie].id,
            portee=portee,
            date_debut_validite=OUVERTURE_REFERENTIEL,
            date_fin_validite=None,
            commentaire=(
                "Convention de delegation de gestion, donnee de simulation."
                f" Perimetre : {', '.join(a.value for a in actes)}."
            ),
        )
        session.add(convention)
        session.flush()

        for acte in actes:
            session.add(ActeDelegue(convention_id=convention.id, acte=acte))

        for code_produit, _libelle, _branche, code_cie in PRODUITS:
            if code_cie != code_compagnie or code_produit == PRODUIT_CONVENTION_ECHUE:
                continue
            session.add(
                ConventionProduit(convention_id=convention.id, produit_id=ref.produits[code_produit].id)
            )
            if ActeGestion.GESTION_PRESTATIONS not in actes:
                sans_prestations.append(code_produit)

        # Le produit a convention echue recoit sa propre convention, avec une date de fin.
        if any(code == PRODUIT_CONVENTION_ECHUE for code, _l, _b, cie in PRODUITS if cie == code_compagnie):
            echue = ConventionDelegation(
                reference=f"CONV-{code_compagnie}-{PRODUIT_CONVENTION_ECHUE}-2020",
                compagnie_id=ref.compagnies[code_compagnie].id,
                portee=portee,
                date_debut_validite=OUVERTURE_REFERENTIEL,
                date_fin_validite=FIN_CONVENTION_NON_RENOUVELEE,
                commentaire=(
                    "Convention arrivee a echeance le"
                    f" {FIN_CONVENTION_NON_RENOUVELEE:%d/%m/%Y} et non renouvelee a ce jour."
                    " Aucun acte de gestion n'est delegue au cabinet sur ce produit depuis cette date."
                ),
            )
            session.add(echue)
            session.flush()
            for acte in actes:
                session.add(ActeDelegue(convention_id=echue.id, acte=acte))
            session.add(
                ConventionProduit(
                    convention_id=echue.id, produit_id=ref.produits[PRODUIT_CONVENTION_ECHUE].id
                )
            )
            if ActeGestion.GESTION_PRESTATIONS not in actes:
                sans_prestations.append(PRODUIT_CONVENTION_ECHUE)

    ref.produits_sans_delegation_prestations = tuple(sorted(sans_prestations))
