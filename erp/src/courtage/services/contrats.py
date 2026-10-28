"""Cas d'usage du contrat : souscrire, mettre en vigueur, modifier, suspendre, resilier.

Toute ecriture sur un contrat passe par ce module. C'est voulu : les regles qui protegent la coherence
d'un portefeuille (cycle de vie, motif de resiliation obligatoire, delegation de gestion, historisation)
ne peuvent pas etre reparties entre l'API, l'interface et les scripts, sans qu'un chemin finisse par en
oublier une.

L'ordre des verifications est toujours le meme, et il n'est pas indifferent :

1. **La delegation de gestion**, d'abord. Si la compagnie n'a pas delegue l'acte, le reste n'a pas a
   etre examine.
2. **Le cycle de vie** ensuite : la transition demandee est-elle possible, et le motif present quand il
   est exige.
3. **L'ecriture** enfin : un avenant est ajoute, puis le cache du contrat est reporte depuis cet avenant.

L'avenant est toujours ecrit avant la mise a jour du contrat, et les deux dans la meme transaction. Un
contrat dont le statut aurait change sans avenant correspondant serait un trou dans la piste d'audit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from courtage.domaine.avenants import applique_avenant_au_contrat, prochain_numero_ordre
from courtage.domaine.cycle_vie import verifie_transition
from courtage.domaine.delegation import verifie_acte_delegue
from courtage.modeles.enums import (
    ActeGestion,
    Fractionnement,
    MotifResiliation,
    StatutContrat,
    TypeAvenant,
)
from courtage.modeles.portefeuille import Adherent, Avenant, Contrat
from courtage.modeles.referentiel import Formule, Produit

#: Acte de gestion a verifier pour chaque type d'avenant.
ACTE_PAR_TYPE_AVENANT: dict[TypeAvenant, ActeGestion] = {
    TypeAvenant.SOUSCRIPTION: ActeGestion.AFFILIATION,
    TypeAvenant.MISE_EN_VIGUEUR: ActeGestion.AFFILIATION,
    TypeAvenant.CHANGEMENT_FORMULE: ActeGestion.AVENANT,
    TypeAvenant.REVALORISATION_TARIFAIRE: ActeGestion.AVENANT,
    TypeAvenant.CHANGEMENT_APPORTEUR: ActeGestion.AVENANT,
    TypeAvenant.CHANGEMENT_FRACTIONNEMENT: ActeGestion.AVENANT,
    TypeAvenant.SUSPENSION: ActeGestion.AVENANT,
    TypeAvenant.REMISE_EN_VIGUEUR: ActeGestion.AVENANT,
    TypeAvenant.RESILIATION: ActeGestion.RADIATION,
}


class ContratIntrouvable(Exception):
    """Aucun contrat ne porte ce numero."""


class ReferenceInvalide(Exception):
    """Une reference fournie a la souscription ne designe rien, ou designe des elements incompatibles."""


class DateAvenantIncoherente(Exception):
    """La date d'effet de l'avenant precede celle de l'avenant qui le precede.

    Un historique dont les dates d'effet ne sont pas croissantes rendrait la reconstitution a date
    ambigue : deux avenants se disputeraient la meme periode.
    """


@dataclass(frozen=True, slots=True)
class DemandeSouscription:
    """Donnees d'une souscription, telles que l'API ou le formulaire les fournissent."""

    numero_adherent: str
    code_produit: str
    code_formule: str
    code_apporteur: str
    date_souscription: date
    cotisation_mensuelle_cents: int
    fractionnement: Fractionnement


def contrat_par_numero(session: Session, numero_contrat: str) -> Contrat:
    """Charge un contrat par son numero, ou leve `ContratIntrouvable`."""
    contrat = session.scalars(select(Contrat).where(Contrat.numero_contrat == numero_contrat)).first()
    if contrat is None:
        raise ContratIntrouvable(f"aucun contrat ne porte le numero {numero_contrat}")
    return contrat


def _numero_contrat_suivant(session: Session, code_branche: str, annee: int) -> str:
    """Numero de contrat suivant, de la forme BRANCHE-ANNEE-00001.

    Le compteur repart de la plus grande valeur deja utilisee pour cette branche et cette annee. L'unicite
    reste garantie par la contrainte de base : si deux souscriptions simultanees calculaient le meme
    numero, la seconde serait rejetee plutot que de creer un doublon.
    """
    prefixe = f"{code_branche}-{annee}-"
    dernier = session.scalar(
        select(func.max(Contrat.numero_contrat)).where(Contrat.numero_contrat.like(f"{prefixe}%"))
    )
    suivant = 1 if dernier is None else int(dernier.rsplit("-", 1)[1]) + 1
    return f"{prefixe}{suivant:05d}"


def souscrit_contrat(session: Session, demande: DemandeSouscription) -> Contrat:
    """Cree un contrat a l'etat de devis, avec son avenant de souscription.

    Le contrat nait **devis** et non en cours : il n'a pas encore pris effet, donc il ne porte pas de
    date d'effet et ne se quittance pas. La mise en vigueur est un acte distinct, `met_en_vigueur`.
    """
    adherent = session.scalars(
        select(Adherent).where(Adherent.numero_adherent == demande.numero_adherent)
    ).first()
    if adherent is None:
        raise ReferenceInvalide(f"aucun adherent ne porte le numero {demande.numero_adherent}")

    produit = session.scalars(select(Produit).where(Produit.code == demande.code_produit)).first()
    if produit is None:
        raise ReferenceInvalide(f"aucun produit ne porte le code {demande.code_produit}")

    formule = session.scalars(
        select(Formule).where(Formule.produit_id == produit.id, Formule.code == demande.code_formule)
    ).first()
    if formule is None:
        raise ReferenceInvalide(
            f"la formule {demande.code_formule} n'existe pas sur le produit {demande.code_produit}"
        )

    from courtage.modeles.referentiel import Apporteur, Branche

    apporteur = session.scalars(
        select(Apporteur).where(Apporteur.code == demande.code_apporteur)
    ).first()
    if apporteur is None:
        raise ReferenceInvalide(f"aucun apporteur ne porte le code {demande.code_apporteur}")

    if demande.cotisation_mensuelle_cents < 0:
        raise ReferenceInvalide("la cotisation mensuelle ne peut pas etre negative")

    # Delegation d'abord : affilier un adherent au nom de la compagnie suppose que celle-ci l'ait delegue.
    verifie_acte_delegue(session, produit.id, ActeGestion.AFFILIATION, demande.date_souscription)

    branche = session.get(Branche, produit.branche_id)
    numero = _numero_contrat_suivant(session, branche.code, demande.date_souscription.year)

    contrat = Contrat(
        numero_contrat=numero,
        adherent_id=adherent.id,
        entreprise_id=adherent.entreprise_id,
        produit_id=produit.id,
        statut=StatutContrat.DEVIS,
        date_souscription=demande.date_souscription,
        date_effet=None,
        date_resiliation=None,
        motif_resiliation=None,
        formule_id=formule.id,
        apporteur_id=apporteur.id,
        cotisation_mensuelle_cents=demande.cotisation_mensuelle_cents,
        fractionnement=demande.fractionnement,
    )
    session.add(contrat)
    session.flush()

    session.add(
        Avenant(
            contrat_id=contrat.id,
            numero_ordre=0,
            type_avenant=TypeAvenant.SOUSCRIPTION,
            date_effet=demande.date_souscription,
            date_saisie=demande.date_souscription,
            statut=StatutContrat.DEVIS,
            formule_id=formule.id,
            apporteur_id=apporteur.id,
            cotisation_mensuelle_cents=demande.cotisation_mensuelle_cents,
            fractionnement=demande.fractionnement,
            motif_resiliation=None,
            commentaire="Souscription",
        )
    )
    session.flush()
    return contrat


def ajoute_avenant(
    session: Session,
    contrat: Contrat,
    *,
    type_avenant: TypeAvenant,
    date_effet: date,
    statut_cible: StatutContrat | None = None,
    formule_id: int | None = None,
    apporteur_id: int | None = None,
    cotisation_mensuelle_cents: int | None = None,
    fractionnement: Fractionnement | None = None,
    motif_resiliation: MotifResiliation | None = None,
    commentaire: str | None = None,
    date_saisie: date | None = None,
) -> Avenant:
    """Ajoute un avenant a un contrat, apres verification de la delegation et du cycle de vie.

    Les champs non fournis reprennent la valeur courante du contrat : un avenant de revalorisation n'a pas
    a repeter la formule et l'apporteur, et un avenant de changement de formule n'a pas a repeter la
    cotisation s'il ne la change pas.
    """
    statut_depart = contrat.statut
    statut_arrivee = statut_cible or statut_depart

    # 1. Delegation de gestion.
    verifie_acte_delegue(session, contrat.produit_id, ACTE_PAR_TYPE_AVENANT[type_avenant], date_effet)

    # 2. Cycle de vie.
    verifie_transition(statut_depart, statut_arrivee, type_avenant, motif_resiliation)

    # 3. Coherence des dates : l'historique doit rester croissant.
    dernier = session.scalars(
        select(Avenant)
        .where(Avenant.contrat_id == contrat.id)
        .order_by(Avenant.numero_ordre.desc())
        .limit(1)
    ).first()
    if dernier is not None and date_effet < dernier.date_effet:
        raise DateAvenantIncoherente(
            f"la date d'effet {date_effet:%d/%m/%Y} precede celle du dernier avenant"
            f" ({dernier.date_effet:%d/%m/%Y}, rang {dernier.numero_ordre})"
        )

    if formule_id is not None:
        formule = session.get(Formule, formule_id)
        if formule is None or formule.produit_id != contrat.produit_id:
            raise ReferenceInvalide(
                f"la formule {formule_id} n'appartient pas au produit du contrat {contrat.numero_contrat}"
            )
    if cotisation_mensuelle_cents is not None and cotisation_mensuelle_cents < 0:
        raise ReferenceInvalide("la cotisation mensuelle ne peut pas etre negative")

    avenant = Avenant(
        contrat_id=contrat.id,
        numero_ordre=prochain_numero_ordre(session, contrat.id),
        type_avenant=type_avenant,
        date_effet=date_effet,
        date_saisie=date_saisie or date_effet,
        statut=statut_arrivee,
        formule_id=formule_id if formule_id is not None else contrat.formule_id,
        apporteur_id=apporteur_id if apporteur_id is not None else contrat.apporteur_id,
        cotisation_mensuelle_cents=(
            cotisation_mensuelle_cents
            if cotisation_mensuelle_cents is not None
            else contrat.cotisation_mensuelle_cents
        ),
        fractionnement=fractionnement or contrat.fractionnement,
        motif_resiliation=motif_resiliation,
        commentaire=commentaire,
    )
    session.add(avenant)
    session.flush()

    applique_avenant_au_contrat(contrat, avenant)
    session.flush()
    return avenant


def met_en_vigueur(session: Session, contrat: Contrat, date_effet: date, commentaire: str | None = None) -> Avenant:
    """Fait passer un devis en contrat en cours. C'est l'acte qui donne sa date d'effet au contrat."""
    return ajoute_avenant(
        session,
        contrat,
        type_avenant=TypeAvenant.MISE_EN_VIGUEUR,
        date_effet=date_effet,
        statut_cible=StatutContrat.EN_COURS,
        commentaire=commentaire,
    )


def suspend(session: Session, contrat: Contrat, date_effet: date, commentaire: str | None = None) -> Avenant:
    """Suspend les garanties, typiquement apres mise en demeure pour cotisations impayees."""
    return ajoute_avenant(
        session,
        contrat,
        type_avenant=TypeAvenant.SUSPENSION,
        date_effet=date_effet,
        statut_cible=StatutContrat.SUSPENDU,
        commentaire=commentaire or "Suspension des garanties apres mise en demeure",
    )


def remet_en_vigueur(
    session: Session, contrat: Contrat, date_effet: date, commentaire: str | None = None
) -> Avenant:
    """Remet les garanties en vigueur apres regularisation."""
    return ajoute_avenant(
        session,
        contrat,
        type_avenant=TypeAvenant.REMISE_EN_VIGUEUR,
        date_effet=date_effet,
        statut_cible=StatutContrat.EN_COURS,
        commentaire=commentaire or "Remise en vigueur apres encaissement de l'arriere",
    )


def resilie(
    session: Session,
    contrat: Contrat,
    date_effet: date,
    motif: MotifResiliation,
    commentaire: str | None = None,
) -> Avenant:
    """Resilie un contrat. Le motif est obligatoire, et `verifie_transition` le refuse s'il manque."""
    return ajoute_avenant(
        session,
        contrat,
        type_avenant=TypeAvenant.RESILIATION,
        date_effet=date_effet,
        statut_cible=StatutContrat.RESILIE,
        motif_resiliation=motif,
        commentaire=commentaire,
    )
