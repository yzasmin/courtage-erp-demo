"""Ce que les controles ont besoin de savoir du portefeuille, lu une fois par execution.

Un controle de qualite qui interroge la base ligne a ligne met plusieurs minutes sur quatorze mille
lignes et des dizaines de milliers d'appels. Tout ce dont les controles ont besoin est donc charge une
seule fois, en six requetes, et tient en memoire : les numeros connus, les dates d'effet et de
resiliation, le tarif mensuel median par contrat, les plafonds de garantie et la consommation annuelle
deja enregistree.

Ce module est aussi celui que le **generateur** de fichiers consulte. Le generateur et le controle
partagent ainsi la meme vue du portefeuille, ce qui est voulu : le manifeste de verite terrain doit
decrire ce qui a reellement ete injecte, dans les memes termes que le controle qui tentera de le
retrouver. Deux definitions du tarif median, une pour chaque, rendraient le rappel et la precision
inexploitables.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from courtage.flux.enums import StatutLot
from courtage.flux.modeles import LotFichier
from courtage.modeles.collectif import PoliceCollective
from courtage.modeles.delegation import ActeDelegue, ConventionDelegation, ConventionProduit
from courtage.modeles.enums import ActeGestion
from courtage.modeles.financier import AppelCotisation, Prestation
from courtage.modeles.portefeuille import Adherent, Contrat
from courtage.modeles.referentiel import Formule, FormuleGarantie, Garantie, Produit

#: Nombre moyen de jours par mois, pour ramener une periode de garantie a un nombre de mois.
JOURS_PAR_MOIS = 30.44


def mois_couverts(periode_debut: date, periode_fin: date) -> int:
    """Nombre de mois couverts par une periode d'appel, au plus proche, au minimum un."""
    return max(1, round(((periode_fin - periode_debut).days + 1) / JOURS_PAR_MOIS))


@dataclass(slots=True)
class ContextePorteur:
    """Ce qu'un controle doit savoir du porteur d'un appel : un contrat individuel ou une police.

    Le meme objet decrit les deux, parce que les controles leur posent les memes questions : existes-tu,
    depuis quand, jusqu'a quand, et quel est ton tarif habituel. Le tarif est ramene au mois **et a
    l'affilie**, de sorte qu'une police qui gagne dix salaries ne declenche pas un signalement de
    montant atypique : ce qui a change est son effectif, pas son tarif.

    `tarif_mensuel_median_cents` est la mediane, et non la moyenne, des tarifs mensuels par affilie
    observes sur les appels du porteur. La mediane resiste a une valeur aberrante deja presente dans
    l'historique, ce que la moyenne ne fait pas : une seule erreur passee deplacerait la reference et
    masquerait les suivantes.
    """

    numero: str
    date_effet: date | None
    date_resiliation: date | None
    tarif_mensuel_median_cents: int
    nombre_appels: int


@dataclass(slots=True)
class ReferentielControles:
    """Vue du portefeuille utilisee par les controles et par le generateur de fichiers."""

    contrats: dict[str, ContextePorteur] = field(default_factory=dict)
    #: Polices collectives, indexees par numero de police.
    polices: dict[str, ContextePorteur] = field(default_factory=dict)
    adherents: set[str] = field(default_factory=set)
    #: Plafond annuel par couple numero de contrat et code de garantie, quand il est saisi.
    plafonds: dict[tuple[str, str], int] = field(default_factory=dict)
    #: Remboursements enregistres, par contrat, garantie et annee civile, sous forme de liste datee.
    #: Une liste et non une somme, parce que la consommation doit etre cumulee **a une date** : sommer
    #: l'annee entiere ferait entrer dans le calcul des prestations posterieures a celle que l'on
    #: controle, et le controle regarderait alors l'avenir. C'est la meme exigence que celle du module 5
    #: sur l'absence de fuite temporelle, et elle vaut aussi pour un controle de qualite.
    consommation: dict[tuple[str, str, int], list[tuple[date, str, int]]] = field(default_factory=dict)
    #: Empreintes des lots deja charges, pour le controle « fichier deja charge ».
    empreintes_chargees: dict[str, str] = field(default_factory=dict)
    #: Identifiant de produit par code de produit.
    produit_par_code: dict[str, int] = field(default_factory=dict)
    #: Produit du porteur, indexe par numero de contrat et par numero de police.
    produit_du_porteur: dict[str, int] = field(default_factory=dict)
    #: Perimetres delegues par produit : periode de validite et actes reellement delegues.
    #: Charges en memoire une fois, parce que les controles les interrogent ligne a ligne.
    conventions: dict[int, list[tuple[date, date | None, frozenset[ActeGestion]]]] = field(
        default_factory=dict
    )

    def contrat_connu(self, numero: str) -> bool:
        return numero in self.contrats

    def police_connue(self, numero: str) -> bool:
        return numero in self.polices

    def porteur(self, numero_contrat: str | None, numero_police: str | None) -> ContextePorteur | None:
        """Porteur de la ligne : son contrat en individuel, sa police en collectif."""
        if numero_contrat:
            return self.contrats.get(numero_contrat)
        if numero_police:
            return self.polices.get(numero_police)
        return None

    def actes_delegues(self, produit_id: int, jour: date) -> frozenset[ActeGestion]:
        """Actes reellement delegues sur un produit a une date donnee.

        Aucune autorisation par defaut : un produit sans convention en vigueur ne permet aucun acte.
        C'est la regle posee au module 1, et la repeter ici n'est pas une duplication de logique mais
        la meme lecture faite en memoire, pour ne pas emettre une requete par ligne de fichier.
        """
        for debut, fin, actes in self.conventions.get(produit_id, []):
            if debut <= jour and (fin is None or fin >= jour):
                return actes
        return frozenset()

    def plafond(self, numero_contrat: str, code_garantie: str) -> int | None:
        return self.plafonds.get((numero_contrat, code_garantie))

    def cumul_avant(
        self, numero_contrat: str, code_garantie: str, jour: date, reference: str
    ) -> int:
        """Remboursements de l'annee anterieurs a cette date, hors la prestation citee.

        La prestation citee est exclue parce que le bordereau recu la reprend : la compter deux fois
        gonflerait la consommation et signalerait un depassement qui n'existe pas.
        """
        lignes = self.consommation.get((numero_contrat, code_garantie, jour.year), [])
        return sum(montant for date_soins, ref, montant in lignes if date_soins < jour and ref != reference)


def _contexte(
    numero: str, date_effet: date | None, date_resiliation: date | None, tarifs: dict[str, list[int]]
) -> ContextePorteur:
    observations = tarifs.get(numero, [])
    return ContextePorteur(
        numero=numero,
        date_effet=date_effet,
        date_resiliation=date_resiliation,
        tarif_mensuel_median_cents=int(median(observations)) if observations else 0,
        nombre_appels=len(observations),
    )


def charge_referentiel(session: Session) -> ReferentielControles:
    """Charge en memoire tout ce dont les controles ont besoin."""
    referentiel = ReferentielControles()

    referentiel.adherents = set(session.scalars(select(Adherent.numero_adherent)))

    tarifs: dict[str, list[int]] = defaultdict(list)
    requete_appels_individuels = select(
        Contrat.numero_contrat,
        AppelCotisation.montant_ht_cents,
        AppelCotisation.periode_debut,
        AppelCotisation.periode_fin,
        AppelCotisation.nombre_affilies,
    ).join(AppelCotisation, AppelCotisation.contrat_id == Contrat.id)
    requete_appels_groupes = select(
        PoliceCollective.numero_police,
        AppelCotisation.montant_ht_cents,
        AppelCotisation.periode_debut,
        AppelCotisation.periode_fin,
        AppelCotisation.nombre_affilies,
    ).join(AppelCotisation, AppelCotisation.police_id == PoliceCollective.id)
    for requete in (requete_appels_individuels, requete_appels_groupes):
        for numero, montant_ht, debut, fin, affilies in session.execute(requete):
            diviseur = mois_couverts(debut, fin) * max(1, int(affilies or 1))
            tarifs[numero].append(round(int(montant_ht) / diviseur))

    requete_contrats = select(Contrat.numero_contrat, Contrat.date_effet, Contrat.date_resiliation)
    for numero, date_effet, date_resiliation in session.execute(requete_contrats):
        referentiel.contrats[numero] = _contexte(numero, date_effet, date_resiliation, tarifs)

    requete_polices = select(
        PoliceCollective.numero_police,
        PoliceCollective.date_effet,
        PoliceCollective.date_resiliation,
    )
    for numero, date_effet, date_resiliation in session.execute(requete_polices):
        referentiel.polices[numero] = _contexte(numero, date_effet, date_resiliation, tarifs)

    requete_plafonds = (
        select(Contrat.numero_contrat, Garantie.code, FormuleGarantie.plafond_annuel_cents)
        .join(Formule, Formule.id == Contrat.formule_id)
        .join(FormuleGarantie, FormuleGarantie.formule_id == Formule.id)
        .join(Garantie, Garantie.id == FormuleGarantie.garantie_id)
        .where(FormuleGarantie.plafond_annuel_cents.is_not(None))
    )
    for numero, code_garantie, plafond in session.execute(requete_plafonds):
        referentiel.plafonds[(numero, code_garantie)] = int(plafond)

    requete_prestations = (
        select(
            Contrat.numero_contrat,
            Garantie.code,
            Prestation.date_soins,
            Prestation.reference_prestation,
            Prestation.montant_rembourse_cents,
        )
        .join(Contrat, Contrat.id == Prestation.contrat_id)
        .join(Garantie, Garantie.id == Prestation.garantie_id)
    )
    consommation: dict[tuple[str, str, int], list[tuple[date, str, int]]] = defaultdict(list)
    for numero, code_garantie, date_soins, reference, rembourse in session.execute(requete_prestations):
        consommation[(numero, code_garantie, date_soins.year)].append(
            (date_soins, reference, int(rembourse))
        )
    for lignes in consommation.values():
        lignes.sort()
    referentiel.consommation = dict(consommation)

    referentiel.produit_par_code = {
        code: identifiant for identifiant, code in session.execute(select(Produit.id, Produit.code))
    }
    requete_porteurs_contrats = select(Contrat.numero_contrat, Contrat.produit_id)
    requete_porteurs_polices = select(PoliceCollective.numero_police, PoliceCollective.produit_id)
    for requete in (requete_porteurs_contrats, requete_porteurs_polices):
        for numero, produit_id in session.execute(requete):
            referentiel.produit_du_porteur[numero] = produit_id

    actes_par_convention: dict[int, set[ActeGestion]] = defaultdict(set)
    for convention_id, acte in session.execute(select(ActeDelegue.convention_id, ActeDelegue.acte)):
        actes_par_convention[convention_id].add(acte)
    perimetres: dict[int, list[tuple[date, date | None, frozenset[ActeGestion]]]] = defaultdict(list)
    requete_conventions = (
        select(
            ConventionProduit.produit_id,
            ConventionDelegation.id,
            ConventionDelegation.date_debut_validite,
            ConventionDelegation.date_fin_validite,
        )
        .join(ConventionDelegation, ConventionDelegation.id == ConventionProduit.convention_id)
        .order_by(ConventionDelegation.date_debut_validite.desc())
    )
    for produit_id, convention_id, debut, fin in session.execute(requete_conventions):
        perimetres[produit_id].append((debut, fin, frozenset(actes_par_convention.get(convention_id, set()))))
    referentiel.conventions = dict(perimetres)

    requete_lots = select(LotFichier.sha256, LotFichier.nom_fichier).where(
        LotFichier.statut == StatutLot.CHARGE
    )
    referentiel.empreintes_chargees = {
        empreinte: nom for empreinte, nom in session.execute(requete_lots)
    }
    return referentiel
