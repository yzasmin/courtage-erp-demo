"""Enregistrement d'une prestation, sous controle de la delegation de gestion.

Pourquoi ce module existe. La documentation annoncait un controle de delegation avant sept actes de
gestion, alors que trois seulement avaient un chemin d'ecriture : affiliation, avenant et radiation. Le
texte etait donc en avance sur le code, ce qui est exactement ce qu'un editeur du metier repere.

Ce module ferme l'ecart pour l'acte le plus parlant, **la gestion des prestations**, qui est aussi celui
que les compagnies deleguent le moins volontiers. Les trois actes restants sont documentes dans
`docs/modele-donnees.md` avec l'etat exact de leur controle :

| Acte | Chemin d'ecriture | Controle |
| --- | --- | --- |
| `affiliation` | `services.contrats.souscrit_contrat` | oui |
| `avenant` | `services.contrats.ajoute_avenant` | oui |
| `radiation` | `services.contrats.resilie` | oui |
| `gestion_prestations` | `services.prestations.enregistre_prestation` | oui |
| `quittancement` | chaine du module 2 | a brancher au module 2 |
| `encaissement` | chaine du module 2 | a brancher au module 2 |
| `gestion_sinistres` | aucun | aucun acte de prevoyance n'est saisi dans ce projet |

Le generateur applique la meme regle, a la meme date, par `generateur.delegation_datee`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import extract, func, select
from sqlalchemy.orm import Session

from courtage.domaine.avenants import etat_a_la_date
from courtage.domaine.delegation import verifie_acte_delegue
from courtage.domaine.montants import applique_taux
from courtage.modeles.enums import ActeGestion, StatutPrestation
from courtage.modeles.financier import Prestation
from courtage.modeles.portefeuille import Contrat
from courtage.modeles.referentiel import FormuleGarantie, Garantie
from courtage.services.contrats import ReferenceInvalide


class GarantieNonCouverte(Exception):
    """La garantie invoquee ne figure pas dans la formule du contrat a la date des soins."""


class ReferenceDejaAttribuee(Exception):
    """Deux saisies ont calcule la meme reference de prestation, et la base en a refuse une.

    Le cas est rare, et il vient de la concurrence : le compteur est lu puis ecrit en deux temps. La
    contrainte d'unicite tranche, ce qui est le bon comportement ; ce qui ne l'etait pas, c'est de
    laisser l'exception remonter en erreur serveur. Le client lit maintenant un refus 409 qui lui dit
    de relancer sa saisie.
    """


class ContratSansGarantie(Exception):
    """Le contrat n'etait pas en vigueur a la date des soins.

    Un contrat resilie, suspendu ou pas encore en vigueur n'ouvre droit a rien. Le verifier a
    l'enregistrement evite de servir une prestation que la compagnie reprendra au cabinet.
    """


@dataclass(frozen=True, slots=True)
class DemandePrestation:
    """Demande de remboursement, telle qu'un gestionnaire ou l'agent du module 4 la presente."""

    numero_contrat: str
    code_garantie: str
    date_soins: date
    montant_demande_cents: int
    #: Jour du reglement. Par defaut le jour de l'enregistrement : la version precedente reprenait la date
    #: des soins, ce qui datait un reglement de plusieurs mois avant la decision qui l'autorisait.
    date_reglement: date | None = None


@dataclass(frozen=True, slots=True)
class ResultatPrestation:
    """Ce que l'enregistrement a decide, et pourquoi."""

    prestation: Prestation
    plafond_annuel_cents: int | None
    deja_rembourse_cents: int
    taux_remboursement_bp: int


def cumul_annuel_rembourse(session: Session, contrat_id: int, garantie_id: int, annee: int) -> int:
    """Montant deja rembourse sur cette garantie, pour ce contrat, dans l'annee civile.

    C'est ce cumul qui consomme le plafond annuel. Appliquer le plafond prestation par prestation
    reviendrait a l'offrir autant de fois qu'il y a de soins dans l'annee.
    """
    return int(
        session.scalar(
            select(func.coalesce(func.sum(Prestation.montant_rembourse_cents), 0)).where(
                Prestation.contrat_id == contrat_id,
                Prestation.garantie_id == garantie_id,
                Prestation.statut == StatutPrestation.REGLEE,
                # `extract` et non `strftime` : portable sur les deux moteurs.
                extract("year", Prestation.date_soins) == annee,
            )
        )
        or 0
    )


def enregistre_prestation(
    session: Session, demande: DemandePrestation, reference: str
) -> ResultatPrestation:
    """Enregistre une prestation, apres les quatre verifications qu'un gestionnaire ferait.

    L'ordre est le meme que partout ailleurs dans le projet, et il n'est pas indifferent :

    1. **La delegation**, appreciee a la date des soins. Si la compagnie n'a pas delegue la gestion des
       prestations sur ce produit, le reste n'a pas a etre examine.
    2. **La garantie du contrat a la date des soins**, reconstituee par les avenants. Un contrat
       resilie ou suspendu ce jour-la n'ouvre droit a rien.
    3. **La couverture de la garantie invoquee** dans la formule en vigueur ce jour-la.
    4. **Le plafond annuel**, consomme par le cumul deja rembourse sur l'annee.
    """
    contrat = session.scalars(
        select(Contrat).where(Contrat.numero_contrat == demande.numero_contrat)
    ).first()
    if contrat is None:
        raise ReferenceInvalide(f"aucun contrat ne porte le numero {demande.numero_contrat}")
    if demande.montant_demande_cents < 0:
        raise ReferenceInvalide("le montant demande ne peut pas etre negatif")

    # 1. Delegation, a la date des soins.
    verifie_acte_delegue(
        session, contrat.produit_id, ActeGestion.GESTION_PRESTATIONS, demande.date_soins
    )

    # 2. Le contrat etait-il en garantie ce jour-la.
    etat = etat_a_la_date(session, contrat, demande.date_soins)
    # `garantie_en_cours` et non `est_en_portefeuille` : un contrat suspendu compte encore au
    # portefeuille, mais sa garantie ne court plus et il n'ouvre donc droit a rien.
    if not etat.garantie_en_cours:
        raise ContratSansGarantie(
            f"le contrat {contrat.numero_contrat} etait au statut {etat.statut.value}"
            f" le {demande.date_soins:%d/%m/%Y} : aucune garantie n'y court"
        )

    garantie = session.scalars(
        select(Garantie).where(Garantie.code == demande.code_garantie)
    ).first()
    if garantie is None:
        raise ReferenceInvalide(f"aucune garantie ne porte le code {demande.code_garantie}")

    # 3. La formule en vigueur a la date des soins couvre-t-elle cette garantie.
    couverture = session.scalars(
        select(FormuleGarantie).where(
            FormuleGarantie.formule_id == etat.formule_id,
            FormuleGarantie.garantie_id == garantie.id,
        )
    ).first()
    if couverture is None:
        raise GarantieNonCouverte(
            f"la garantie {garantie.code} n'est pas couverte par la formule en vigueur sur le"
            f" contrat {contrat.numero_contrat} le {demande.date_soins:%d/%m/%Y}"
        )

    # 4. Plafond annuel, consomme par le cumul de l'annee.
    rembourse = min(
        applique_taux(demande.montant_demande_cents, couverture.taux_remboursement_bp),
        demande.montant_demande_cents,
    )
    deja = cumul_annuel_rembourse(session, contrat.id, garantie.id, demande.date_soins.year)
    motif_rejet = None
    if couverture.plafond_annuel_cents is not None:
        reste = max(0, couverture.plafond_annuel_cents - deja)
        if rembourse > reste:
            rembourse = reste
            motif_rejet = "plafond annuel de la garantie atteint"

    if rembourse == 0 and motif_rejet is not None:
        prestation = Prestation(
            reference_prestation=reference,
            contrat_id=contrat.id,
            adherent_id=contrat.adherent_id,
            garantie_id=garantie.id,
            date_soins=demande.date_soins,
            date_reglement=None,
            montant_demande_cents=demande.montant_demande_cents,
            montant_rembourse_cents=0,
            statut=StatutPrestation.REJETEE,
            motif_rejet=motif_rejet,
        )
    else:
        prestation = Prestation(
            reference_prestation=reference,
            contrat_id=contrat.id,
            adherent_id=contrat.adherent_id,
            garantie_id=garantie.id,
            date_soins=demande.date_soins,
            date_reglement=demande.date_reglement or date.today(),
            montant_demande_cents=demande.montant_demande_cents,
            montant_rembourse_cents=rembourse,
            statut=StatutPrestation.REGLEE,
            motif_rejet=None,
        )

    session.add(prestation)
    session.flush()
    return ResultatPrestation(
        prestation=prestation,
        plafond_annuel_cents=couverture.plafond_annuel_cents,
        deja_rembourse_cents=deja,
        taux_remboursement_bp=couverture.taux_remboursement_bp,
    )


def reference_suivante(session: Session, date_soins: date) -> str:
    """Reference de prestation suivante, de la forme PRE-AAAAMM-0000001.

    Le compteur est **propre au mois des soins**, et c'est la correction d'un defaut qui rendait l'API
    inutilisable des la deuxieme saisie sur un mois ancien.

    La version precedente prenait le plus grand numero toutes periodes confondues :
    `max(reference_prestation)`. Or ce maximum porte sur la chaine entiere, donc d'abord sur le mois.
    Pour un portefeuille dont la derniere prestation est `PRE-202609-0049398`, une saisie datee de
    juillet produisait `PRE-202607-0049399` ; la saisie suivante, toujours en juillet, relisait le
    meme maximum de septembre et reproduisait **exactement la meme reference**. La contrainte
    d'unicite refusait l'ecriture, et l'exception remontait en HTTP 500 en texte brut.

    Le compteur par mois est a la fois correct et conforme au format : la reference porte deja le
    mois, l'unicite n'a donc a tenir que dans le mois. Deux saisies simultanees sur le meme mois
    calculeraient encore la meme reference ; la contrainte de base les departage, et l'API rend
    desormais un refus 409 lisible au lieu d'une erreur serveur, voir `reference_deja_attribuee`.
    """
    prefixe = f"PRE-{date_soins:%Y%m}-"
    dernier = session.scalar(
        select(func.max(Prestation.reference_prestation)).where(
            Prestation.reference_prestation.like(f"{prefixe}%")
        )
    )
    suivant = 1 if dernier is None else int(dernier.rsplit("-", 1)[1]) + 1
    return f"{prefixe}{suivant:07d}"
