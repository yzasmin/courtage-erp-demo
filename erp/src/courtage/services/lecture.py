"""Requetes de lecture partagees par l'API et par l'interface.

Elles sont ici, et non dupliquees des deux cotes, pour une raison simple : une liste d'adherents affichee
dans l'interface et la meme liste servie par l'API doivent compter les contrats de la meme facon. Deux
implementations divergeraient, et c'est l'utilisateur qui le decouvrirait.

Les requetes evitent le probleme des N plus une requete en agregeant cote base plutot qu'en parcourant
les relations ORM. Sur une fiche adherent la difference est invisible ; sur une liste de 5 000 adherents
elle fait la difference entre un ecran instantane et un ecran inutilisable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.orm import Session

from courtage.modeles.collectif import LigneAppelCotisation
from courtage.modeles.enums import StatutAppel, StatutContrat, StatutPrestation
from courtage.modeles.financier import AppelCotisation, Encaissement, Prestation
from courtage.modeles.portefeuille import Adherent, Avenant, Contrat, EntrepriseSouscriptrice
from courtage.modeles.referentiel import (
    Apporteur,
    Branche,
    Compagnie,
    Formule,
    FormuleGarantie,
    Garantie,
    Produit,
    TauxRetrocession,
)

#: Statuts qui comptent dans le portefeuille. Un devis n'en fait pas partie : il n'a jamais pris effet.
STATUTS_EN_PORTEFEUILLE = (StatutContrat.EN_COURS, StatutContrat.SUSPENDU)

#: Nombre de mois d'une annee, pour annualiser une cotisation mensuelle.
MOIS_PAR_AN = 12


@dataclass(frozen=True, slots=True)
class Page:
    """Une page de resultats et de quoi afficher sa pagination."""

    lignes: list
    total: int
    page: int
    par_page: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.par_page))


def _applique_pagination(requete: Select, page: int, par_page: int) -> Select:
    return requete.offset((page - 1) * par_page).limit(par_page)


def liste_adherents(
    session: Session,
    *,
    recherche: str | None = None,
    departement: str | None = None,
    page: int = 1,
    par_page: int = 25,
) -> Page:
    """Liste paginee des adherents, avec le nombre de contrats de chacun.

    La recherche porte sur le nom, le prenom et le numero d'adherent : ce sont les trois entrees dont
    dispose un gestionnaire qui a quelqu'un au telephone.
    """
    nb_contrats = (
        select(Contrat.adherent_id, func.count(Contrat.id).label("nombre"))
        .group_by(Contrat.adherent_id)
        .subquery()
    )

    requete = (
        select(Adherent, EntrepriseSouscriptrice.raison_sociale, func.coalesce(nb_contrats.c.nombre, 0))
        .outerjoin(EntrepriseSouscriptrice, EntrepriseSouscriptrice.id == Adherent.entreprise_id)
        .outerjoin(nb_contrats, nb_contrats.c.adherent_id == Adherent.id)
    )
    comptage = select(func.count(Adherent.id))

    if recherche:
        motif = f"%{recherche.strip().lower()}%"
        condition = or_(
            func.lower(Adherent.nom).like(motif),
            func.lower(Adherent.prenom).like(motif),
            func.lower(Adherent.numero_adherent).like(motif),
        )
        requete = requete.where(condition)
        comptage = comptage.where(condition)
    if departement:
        requete = requete.where(Adherent.departement == departement)
        comptage = comptage.where(Adherent.departement == departement)

    total = session.scalar(comptage) or 0
    requete = requete.order_by(Adherent.nom, Adherent.prenom, Adherent.id)
    lignes = session.execute(_applique_pagination(requete, page, par_page)).all()
    return Page(lignes=list(lignes), total=total, page=page, par_page=par_page)


def adherent_par_numero(session: Session, numero_adherent: str):
    """Adherent, avec la raison sociale de son entreprise quand il releve d'un contrat collectif."""
    return session.execute(
        select(Adherent, EntrepriseSouscriptrice.raison_sociale)
        .outerjoin(EntrepriseSouscriptrice, EntrepriseSouscriptrice.id == Adherent.entreprise_id)
        .where(Adherent.numero_adherent == numero_adherent)
    ).first()


def _requete_contrats_enrichie() -> Select:
    """Contrat, avec son produit, sa branche, sa formule et son apporteur en une seule requete."""
    return (
        select(Contrat, Produit, Branche, Formule, Apporteur)
        .join(Produit, Produit.id == Contrat.produit_id)
        .join(Branche, Branche.id == Produit.branche_id)
        .join(Formule, Formule.id == Contrat.formule_id)
        .join(Apporteur, Apporteur.id == Contrat.apporteur_id)
    )


def contrats_de_adherent(session: Session, adherent_id: int) -> list:
    requete = _requete_contrats_enrichie().where(Contrat.adherent_id == adherent_id)
    return list(session.execute(requete.order_by(Contrat.date_souscription.desc())).all())


def liste_contrats(
    session: Session,
    *,
    statut: StatutContrat | None = None,
    code_produit: str | None = None,
    code_apporteur: str | None = None,
    recherche: str | None = None,
    page: int = 1,
    par_page: int = 25,
) -> Page:
    """Liste paginee du portefeuille, filtrable comme un gestionnaire en a besoin."""
    requete = _requete_contrats_enrichie()
    comptage = (
        select(func.count(Contrat.id))
        .join(Produit, Produit.id == Contrat.produit_id)
        .join(Apporteur, Apporteur.id == Contrat.apporteur_id)
    )

    conditions = []
    if statut is not None:
        conditions.append(Contrat.statut == statut)
    if code_produit:
        conditions.append(Produit.code == code_produit)
    if code_apporteur:
        conditions.append(Apporteur.code == code_apporteur)
    if recherche:
        conditions.append(func.lower(Contrat.numero_contrat).like(f"%{recherche.strip().lower()}%"))
    for condition in conditions:
        requete = requete.where(condition)
        comptage = comptage.where(condition)

    total = session.scalar(comptage) or 0
    requete = requete.order_by(Contrat.date_souscription.desc(), Contrat.id)
    lignes = session.execute(_applique_pagination(requete, page, par_page)).all()
    return Page(lignes=list(lignes), total=total, page=page, par_page=par_page)


def contrat_enrichi(session: Session, numero_contrat: str):
    """Contrat et son environnement : produit, branche, formule, apporteur, compagnie, adherent."""
    return session.execute(
        _requete_contrats_enrichie()
        .add_columns(Compagnie, Adherent, EntrepriseSouscriptrice.raison_sociale)
        .join(Compagnie, Compagnie.id == Produit.compagnie_id)
        .join(Adherent, Adherent.id == Contrat.adherent_id)
        .outerjoin(EntrepriseSouscriptrice, EntrepriseSouscriptrice.id == Contrat.entreprise_id)
        .where(Contrat.numero_contrat == numero_contrat)
    ).first()


def garanties_de_formule(session: Session, formule_id: int) -> list:
    """Tableau de garanties d'une formule, dans l'ordre d'affichage du referentiel."""
    return list(
        session.execute(
            select(Garantie, FormuleGarantie)
            .join(FormuleGarantie, FormuleGarantie.garantie_id == Garantie.id)
            .where(FormuleGarantie.formule_id == formule_id)
            .order_by(Garantie.rang)
        ).all()
    )


def avenants_de_contrat(session: Session, contrat_id: int) -> list:
    """Historique complet des avenants d'un contrat, du plus ancien au plus recent."""
    return list(
        session.execute(
            select(Avenant, Formule.code, Apporteur.code)
            .join(Formule, Formule.id == Avenant.formule_id)
            .join(Apporteur, Apporteur.id == Avenant.apporteur_id)
            .where(Avenant.contrat_id == contrat_id)
            .order_by(Avenant.numero_ordre)
        ).all()
    )


@dataclass(frozen=True, slots=True)
class SoldesContrat:
    """Cumuls financiers d'un contrat, tels que la fiche les affiche."""

    cotisations_appelees_cents: int
    cotisations_encaissees_cents: int
    prestations_reglees_cents: int
    nombre_appels: int
    nombre_appels_impayes: int
    nombre_prestations: int


def soldes_de_contrat(session: Session, contrat_id: int) -> SoldesContrat:
    """Cumuls financiers d'un contrat, calcules en base plutot qu'en memoire.

    Trois agregats separes, et non une seule requete jointe : joindre appels, encaissements et
    prestations dans une meme requete multiplierait les lignes entre elles et gonflerait les sommes.
    C'est l'erreur classique du produit cartesien, et elle passe inapercue parce que le resultat reste
    plausible.
    """
    # Un contrat individuel est quittance pour lui-meme ; une affiliation collective l'est par sa
    # police, et sa part se lit dans la ligne de detail. Ne sommer que `contrat_id` affichait un
    # total de zero euro sur une fiche collective, juste au-dessus d'un tableau d'appels bien
    # remplis. C'est le genre d'ecart qu'un gestionnaire remarque en premier.
    police_id = session.scalar(select(Contrat.police_id).where(Contrat.id == contrat_id))

    if police_id is None:
        appels = session.execute(
            select(
                func.coalesce(func.sum(AppelCotisation.montant_total_cents), 0),
                func.count(AppelCotisation.id),
                func.coalesce(
                    func.sum(case((AppelCotisation.statut == StatutAppel.IMPAYE, 1), else_=0)), 0
                ),
            ).where(AppelCotisation.contrat_id == contrat_id)
        ).one()
        encaisse = session.scalar(
            select(func.coalesce(func.sum(Encaissement.montant_cents), 0)).where(
                Encaissement.contrat_id == contrat_id
            )
        )
    else:
        appels = session.execute(
            select(
                func.coalesce(func.sum(LigneAppelCotisation.montant_total_cents), 0),
                func.count(LigneAppelCotisation.id),
                func.coalesce(
                    func.sum(case((AppelCotisation.statut == StatutAppel.IMPAYE, 1), else_=0)), 0
                ),
            )
            .join(AppelCotisation, AppelCotisation.id == LigneAppelCotisation.appel_id)
            .where(LigneAppelCotisation.contrat_id == contrat_id)
        ).one()
        # L'encaissement d'un appel groupe est unique et porte sur toute la police. La part reellement
        # encaissee pour cet affilie est sa quote-part de l'appel, limitee a ce qui a ete regle.
        encaisse = session.scalar(
            select(
                func.coalesce(
                    func.sum(
                        case(
                            (
                                AppelCotisation.statut == StatutAppel.ENCAISSE,
                                LigneAppelCotisation.montant_total_cents,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                )
            )
            .join(AppelCotisation, AppelCotisation.id == LigneAppelCotisation.appel_id)
            .where(LigneAppelCotisation.contrat_id == contrat_id)
        )

    prestations = session.execute(
        select(
            func.coalesce(func.sum(Prestation.montant_rembourse_cents), 0),
            func.count(Prestation.id),
        ).where(Prestation.contrat_id == contrat_id, Prestation.statut == StatutPrestation.REGLEE)
    ).one()

    return SoldesContrat(
        cotisations_appelees_cents=int(appels[0]),
        cotisations_encaissees_cents=int(encaisse or 0),
        prestations_reglees_cents=int(prestations[0]),
        nombre_appels=int(appels[1]),
        nombre_appels_impayes=int(appels[2]),
        nombre_prestations=int(prestations[1]),
    )


def liste_apporteurs(session: Session, a_la_date: date | None = None) -> list:
    """Reseau de distribution, avec la production de chaque apporteur.

    Les contrats comptes sont ceux **en portefeuille**, en cours ou suspendus : y inclure les devis
    gonflerait la production d'affaires qui n'existent pas encore, et y inclure les resilies un portefeuille
    deja perdu. La cotisation annualisee, elle, ne retient que les contrats en cours : un contrat suspendu
    n'est pas appele, comme le definit docs/kpi.md.
    """
    jour = a_la_date or date.today()
    agregat = (
        select(
            Contrat.apporteur_id.label("apporteur_id"),
            func.count(Contrat.id).label("contrats"),
            func.coalesce(
                func.sum(
                    case((Contrat.statut == StatutContrat.EN_COURS, Contrat.cotisation_mensuelle_cents), else_=0)
                ),
                0,
            ).label("cotisation_mensuelle"),
        )
        .where(Contrat.statut.in_(STATUTS_EN_PORTEFEUILLE))
        .group_by(Contrat.apporteur_id)
        .subquery()
    )
    # Accord cadre en vigueur **a la date demandee**, et un seul par apporteur. La version precedente
    # ignorait la date et retenait toute ligne sans date de fin : un bareme futur pouvait s'afficher
    # comme actuel, un bareme clos mais encore en vigueur disparaissait, et deux lignes ouvertes
    # dupliquaient l'apporteur dans la liste en doublant ses totaux.
    cadres = (
        select(
            TauxRetrocession.apporteur_id.label("apporteur_id"),
            TauxRetrocession.taux_bp.label("taux_bp"),
            func.row_number()
            .over(
                partition_by=TauxRetrocession.apporteur_id,
                order_by=TauxRetrocession.date_debut_validite.desc(),
            )
            .label("rang"),
        )
        .where(
            TauxRetrocession.produit_id.is_(None),
            TauxRetrocession.date_debut_validite <= jour,
            or_(
                TauxRetrocession.date_fin_validite.is_(None),
                TauxRetrocession.date_fin_validite >= jour,
            ),
        )
        .subquery()
    )
    taux_cadre = select(cadres.c.apporteur_id, cadres.c.taux_bp).where(cadres.c.rang == 1).subquery()
    return list(
        session.execute(
            select(
                Apporteur,
                func.coalesce(agregat.c.contrats, 0),
                func.coalesce(agregat.c.cotisation_mensuelle, 0) * MOIS_PAR_AN,
                taux_cadre.c.taux_bp,
            )
            .outerjoin(agregat, agregat.c.apporteur_id == Apporteur.id)
            .outerjoin(taux_cadre, taux_cadre.c.apporteur_id == Apporteur.id)
            .order_by(Apporteur.code)
        ).all()
    )


def apporteur_par_code(session: Session, code: str):
    return session.scalars(select(Apporteur).where(Apporteur.code == code)).first()


def produit_par_code(session: Session, code: str):
    return session.scalars(select(Produit).where(Produit.code == code)).first()


@dataclass(frozen=True, slots=True)
class SyntheseAccueil:
    """Chiffres de l'ecran d'accueil du gestionnaire. Tous comptes en base, aucun en dur."""

    adherents: int
    contrats_en_portefeuille: int
    devis_en_attente: int
    contrats_suspendus: int
    contrats_resilies: int
    cotisation_annualisee_cents: int
    appels_impayes: int
    encaissements_non_rapproches: int
    prestations_en_cours: int


def synthese_accueil(session: Session) -> SyntheseAccueil:
    """Compteurs de l'ecran d'accueil, en quelques agregats."""
    par_statut = dict(
        session.execute(select(Contrat.statut, func.count(Contrat.id)).group_by(Contrat.statut)).all()
    )
    # Les contrats suspendus restent au portefeuille, mais ils ne sont pas appeles : leur cotisation ne
    # compte pas dans la cotisation annualisee. C'est le piege chiffre dans docs/kpi.md, et l'accueil le
    # commettait (33 015,00 EUR de trop sur le jeu de demonstration).
    cotisation = session.scalar(
        select(func.coalesce(func.sum(Contrat.cotisation_mensuelle_cents), 0)).where(
            Contrat.statut == StatutContrat.EN_COURS
        )
    )
    return SyntheseAccueil(
        adherents=session.scalar(select(func.count(Adherent.id))) or 0,
        contrats_en_portefeuille=sum(par_statut.get(s, 0) for s in STATUTS_EN_PORTEFEUILLE),
        devis_en_attente=par_statut.get(StatutContrat.DEVIS, 0),
        contrats_suspendus=par_statut.get(StatutContrat.SUSPENDU, 0),
        contrats_resilies=par_statut.get(StatutContrat.RESILIE, 0),
        cotisation_annualisee_cents=int(cotisation or 0) * MOIS_PAR_AN,
        appels_impayes=session.scalar(
            select(func.count(AppelCotisation.id)).where(AppelCotisation.statut == StatutAppel.IMPAYE)
        )
        or 0,
        encaissements_non_rapproches=session.scalar(
            select(func.count(Encaissement.id)).where(Encaissement.appel_id.is_(None))
        )
        or 0,
        prestations_en_cours=session.scalar(
            select(func.count(Prestation.id)).where(Prestation.statut == StatutPrestation.EN_COURS)
        )
        or 0,
    )
