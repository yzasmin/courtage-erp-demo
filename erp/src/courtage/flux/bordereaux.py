"""Production des bordereaux mensuels : commissions par compagnie, retrocessions par apporteur.

Deux documents, deux destinataires, et le vocabulaire compte. Le **bordereau de commissions** recapitule
ce que la compagnie doit au cabinet pour un mois. Le **bordereau de retrocessions** recapitule ce que le
cabinet doit a l'un de ses apporteurs pour ce meme mois : c'est le document que la specification appelle
« bordereau de commissions mensuel par apporteur », et c'est celui qu'un apporteur attend chaque mois
pour verifier sa remuneration.

Les deux sont ecrits en base, dans la table `bordereau` posee au module 1, et exportes en CSV dans
`data/flux/bordereaux/`. L'export n'est pas un gadget : un bordereau existe pour etre envoye, et un
apporteur qui conteste une ligne cite le fichier qu'il a recu.

Chaque ligne cite son **porteur** : le numero de contrat pour une commission individuelle, le numero
de police pour une commission sur appel groupe. C'est ce que le destinataire reconnait, et c'est la
reference qu'il citera pour contester une ligne.

Le total du bordereau est toujours la somme de ses lignes, et une verification le recalcule avant
l'ecriture. Un bordereau dont le total ne tombe pas juste est la seule anomalie qu'un apporteur remarque
immediatement, et celle qui coute le plus de credit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from courtage.domaine.montants import centimes_vers_euros
from courtage.flux.parametres import racine_flux
from courtage.generateur.outils import dernier_jour_du_mois
from courtage.modeles.collectif import PoliceCollective
from courtage.modeles.enums import StatutBordereau, TypeBordereau
from courtage.modeles.financier import AppelCotisation, Bordereau, Commission, Retrocession
from courtage.modeles.portefeuille import Contrat
from courtage.modeles.referentiel import Apporteur, Compagnie

#: Dossier des bordereaux produits. Distinct de l'archive des fichiers recus : ici, le cabinet emet.
NOM_DOSSIER_BORDEREAUX = "bordereaux"

#: Separateur et encodage des bordereaux emis. Memes conventions que les fichiers recus, pour qu'un
#: destinataire francais les ouvre sans manipulation.
SEPARATEUR = ";"
ENCODAGE = "utf-8-sig"


class TotalIncoherent(RuntimeError):
    """Le total recalcule d'un bordereau ne correspond pas a la somme de ses lignes.

    Levee, et non journalisee : un bordereau dont le total est faux ne doit pas sortir. Mieux vaut un
    graphe en echec, visible dans l'interface Airflow, qu'un document errone envoye a une compagnie.
    """


@dataclass(slots=True)
class ResultatBordereaux:
    """Compte rendu de la production, pour le journal d'execution."""

    periodes: list[str] = field(default_factory=list)
    bordereaux_commissions: int = 0
    bordereaux_retrocessions: int = 0
    crees: int = 0
    mis_a_jour: int = 0
    inchanges: int = 0
    lignes_rattachees: int = 0
    montant_commissions_cents: int = 0
    montant_retrocessions_cents: int = 0
    fichiers: list[str] = field(default_factory=list)

    def en_dictionnaire(self) -> dict[str, object]:
        return {
            "periodes": self.periodes,
            "bordereaux_de_commissions": self.bordereaux_commissions,
            "bordereaux_de_retrocessions": self.bordereaux_retrocessions,
            "crees": self.crees,
            "mis_a_jour": self.mis_a_jour,
            "inchanges": self.inchanges,
            "lignes_rattachees": self.lignes_rattachees,
            "montant_commissions_cents": self.montant_commissions_cents,
            "montant_retrocessions_cents": self.montant_retrocessions_cents,
            "fichiers": self.fichiers,
        }


def dossier_bordereaux() -> Path:
    return racine_flux() / NOM_DOSSIER_BORDEREAUX


def _periodes_a_produire(session: Session) -> list[date]:
    """Mois pour lesquels au moins une commission a ete calculee."""
    return list(session.scalars(select(Commission.periode).distinct().order_by(Commission.periode)))


def _enregistre_bordereau(
    session: Session,
    resultat: ResultatBordereaux,
    reference: str,
    type_bordereau: TypeBordereau,
    periode: date,
    compagnie_id: int | None,
    apporteur_id: int | None,
    nombre_lignes: int,
    montant_total: int,
) -> Bordereau:
    """Cree ou met a jour un bordereau, repere par sa reference. Idempotent."""
    bordereau = session.scalars(select(Bordereau).where(Bordereau.reference == reference)).first()
    if bordereau is None:
        bordereau = Bordereau(
            reference=reference,
            type_bordereau=type_bordereau,
            periode=periode,
            date_emission=dernier_jour_du_mois(periode),
            compagnie_id=compagnie_id,
            apporteur_id=apporteur_id,
            nombre_lignes=nombre_lignes,
            montant_total_cents=montant_total,
            statut=StatutBordereau.EMIS,
        )
        session.add(bordereau)
        session.flush()
        resultat.crees += 1
    elif bordereau.nombre_lignes != nombre_lignes or bordereau.montant_total_cents != montant_total:
        bordereau.nombre_lignes = nombre_lignes
        bordereau.montant_total_cents = montant_total
        bordereau.date_emission = dernier_jour_du_mois(periode)
        resultat.mis_a_jour += 1
    else:
        resultat.inchanges += 1
    return bordereau


def _ecrit_csv(chemin: Path, entete: list[str], lignes: list[list[str]], total: list[str]) -> None:
    import csv

    chemin.parent.mkdir(parents=True, exist_ok=True)
    with chemin.open("w", encoding=ENCODAGE, newline="") as flux:
        graveur = csv.writer(flux, delimiter=SEPARATEUR, lineterminator="\r\n")
        graveur.writerow(entete)
        graveur.writerows(lignes)
        graveur.writerow(total)


def _montant(valeur: int) -> str:
    """Montant en euros, virgule decimale, sans separateur de milliers."""
    return f"{centimes_vers_euros(valeur)}".replace(".", ",")


def _produit_bordereaux_commissions(
    session: Session,
    periode: date,
    resultat: ResultatBordereaux,
    a_ecrire: list[tuple[Path, list[str], list[list[str]], list[str]]],
) -> None:
    """Un bordereau par compagnie et par mois, avec le detail de ses lignes de commission."""
    compagnies = session.execute(
        select(
            Commission.compagnie_id,
            Compagnie.code,
            func.count(Commission.id),
            func.coalesce(func.sum(Commission.montant_cents), 0),
        )
        .join(Compagnie, Compagnie.id == Commission.compagnie_id)
        .where(Commission.periode == periode)
        .group_by(Commission.compagnie_id, Compagnie.code)
        .order_by(Compagnie.code)
    ).all()

    for compagnie_id, code, _nombre, montant in compagnies:
        reference = f"BC-{code}-{periode:%Y%m}"
        lignes = session.execute(
            select(
                AppelCotisation.reference_appel,
                func.coalesce(Contrat.numero_contrat, PoliceCollective.numero_police),
                AppelCotisation.date_echeance,
                Commission.assiette_cents,
                Commission.taux_bp,
                Commission.montant_cents,
                Commission.id,
            )
            .join(AppelCotisation, AppelCotisation.id == Commission.appel_id)
            .join(Contrat, Contrat.id == Commission.contrat_id, isouter=True)
            .join(PoliceCollective, PoliceCollective.id == Commission.police_id, isouter=True)
            .where(Commission.periode == periode, Commission.compagnie_id == compagnie_id)
            .order_by(AppelCotisation.reference_appel)
        ).all()

        somme = sum(int(ligne[5]) for ligne in lignes)
        if somme != int(montant):
            raise TotalIncoherent(
                f"bordereau {reference} : somme des lignes {somme} centimes,"
                f" total agrege {int(montant)} centimes"
            )

        bordereau = _enregistre_bordereau(
            session,
            resultat,
            reference,
            TypeBordereau.COMMISSIONS,
            periode,
            compagnie_id,
            None,
            len(lignes),
            somme,
        )
        for ligne in lignes:
            commission = session.get(Commission, ligne[6])
            if commission is not None and commission.bordereau_id != bordereau.id:
                commission.bordereau_id = bordereau.id
                resultat.lignes_rattachees += 1

        a_ecrire.append(
            (
                dossier_bordereaux() / f"{periode:%Y%m}" / f"{reference}.csv",
                ["reference_appel", "porteur", "date_echeance", "assiette_ht", "taux_pourcent", "commission"],
                [
                    [
                        reference_appel,
                        porteur,
                        echeance.strftime("%d/%m/%Y"),
                        _montant(int(assiette)),
                        f"{taux_bp / 100:.2f}".replace(".", ","),
                        _montant(int(montant_ligne)),
                    ]
                    for reference_appel, porteur, echeance, assiette, taux_bp, montant_ligne, _ in lignes
                ],
                ["TOTAL", str(len(lignes)), "", "", "", _montant(somme)],
            )
        )
        resultat.bordereaux_commissions += 1
        resultat.montant_commissions_cents += somme


def _produit_bordereaux_retrocessions(
    session: Session,
    periode: date,
    resultat: ResultatBordereaux,
    a_ecrire: list[tuple[Path, list[str], list[list[str]], list[str]]],
) -> None:
    """Un bordereau par apporteur et par mois : le document que l'apporteur attend."""
    apporteurs = session.execute(
        select(
            Retrocession.apporteur_id,
            Apporteur.code,
            func.count(Retrocession.id),
            func.coalesce(func.sum(Retrocession.montant_cents), 0),
        )
        .join(Apporteur, Apporteur.id == Retrocession.apporteur_id)
        .where(Retrocession.periode == periode)
        .group_by(Retrocession.apporteur_id, Apporteur.code)
        .order_by(Apporteur.code)
    ).all()

    for apporteur_id, code, _nombre, montant in apporteurs:
        reference = f"BR-{code}-{periode:%Y%m}"
        lignes = session.execute(
            select(
                AppelCotisation.reference_appel,
                func.coalesce(Contrat.numero_contrat, PoliceCollective.numero_police),
                AppelCotisation.date_echeance,
                Commission.montant_cents,
                Retrocession.taux_bp,
                Retrocession.montant_cents,
                Retrocession.id,
            )
            .join(Commission, Commission.id == Retrocession.commission_id)
            .join(AppelCotisation, AppelCotisation.id == Commission.appel_id)
            .join(Contrat, Contrat.id == Commission.contrat_id, isouter=True)
            .join(PoliceCollective, PoliceCollective.id == Commission.police_id, isouter=True)
            .where(Retrocession.periode == periode, Retrocession.apporteur_id == apporteur_id)
            .order_by(AppelCotisation.reference_appel)
        ).all()

        somme = sum(int(ligne[5]) for ligne in lignes)
        if somme != int(montant):
            raise TotalIncoherent(
                f"bordereau {reference} : somme des lignes {somme} centimes,"
                f" total agrege {int(montant)} centimes"
            )

        bordereau = _enregistre_bordereau(
            session,
            resultat,
            reference,
            TypeBordereau.RETROCESSIONS,
            periode,
            None,
            apporteur_id,
            len(lignes),
            somme,
        )
        for ligne in lignes:
            retrocession = session.get(Retrocession, ligne[6])
            if retrocession is not None and retrocession.bordereau_id != bordereau.id:
                retrocession.bordereau_id = bordereau.id
                resultat.lignes_rattachees += 1

        a_ecrire.append(
            (
                dossier_bordereaux() / f"{periode:%Y%m}" / f"{reference}.csv",
                [
                    "reference_appel",
                    "porteur",
                    "date_echeance",
                    "commission_cabinet",
                    "taux_retrocession_pourcent",
                    "retrocession",
                ],
                [
                    [
                        reference_appel,
                        porteur,
                        echeance.strftime("%d/%m/%Y"),
                        _montant(int(commission_montant)),
                        f"{taux_bp / 100:.2f}".replace(".", ","),
                        _montant(int(montant_ligne)),
                    ]
                    for reference_appel, porteur, echeance, commission_montant, taux_bp, montant_ligne, _ in lignes
                ],
                ["TOTAL", str(len(lignes)), "", "", "", _montant(somme)],
            )
        )
        resultat.bordereaux_retrocessions += 1
        resultat.montant_retrocessions_cents += somme


def produit(session: Session, periodes: list[date] | None = None) -> ResultatBordereaux:
    """Produit tous les bordereaux des periodes demandees, en base et en fichier.

    Idempotente : un bordereau est repere par sa reference, ses lignes sont rattachees par identifiant.
    Rejouer la production reecrit les memes fichiers et ne cree aucun doublon en base.
    """
    resultat = ResultatBordereaux()
    # Meme normalisation que le calcul des commissions : une periode au milieu du mois ne trouverait
    # aucune commission, puisque celles-ci sont enregistrees au premier du mois.
    mois = sorted(
        {
            date(jour.year, jour.month, 1)
            for jour in (periodes if periodes is not None else _periodes_a_produire(session))
        }
    )
    resultat.periodes = [jour.isoformat() for jour in mois]
    a_ecrire: list[tuple[Path, list[str], list[list[str]], list[str]]] = []
    for periode in mois:
        _produit_bordereaux_commissions(session, periode, resultat, a_ecrire)
        _produit_bordereaux_retrocessions(session, periode, resultat, a_ecrire)
    session.commit()
    # Les fichiers sont ecrits **apres** la validation en base, et c'est volontaire. Si une incoherence
    # de total avait arrete la production a mi-chemin, les bordereaux deja ecrits seraient restes sur
    # le disque alors que la base aurait ete annulee : des documents prets a etre envoyes sans
    # contrepartie en base.
    for chemin, entete, lignes, total in a_ecrire:
        _ecrit_csv(chemin, entete, lignes, total)
        resultat.fichiers.append(str(chemin))
    return resultat


def bordereaux_en_base(session: Session) -> list[dict[str, object]]:
    """Liste les bordereaux enregistres, pour le journal et pour l'interface."""
    requete = (
        select(
            Bordereau.reference,
            Bordereau.type_bordereau,
            Bordereau.periode,
            Bordereau.nombre_lignes,
            Bordereau.montant_total_cents,
            Compagnie.code,
            Apporteur.code,
        )
        .join(Compagnie, Compagnie.id == Bordereau.compagnie_id, isouter=True)
        .join(Apporteur, Apporteur.id == Bordereau.apporteur_id, isouter=True)
        .where(or_(Bordereau.compagnie_id.is_not(None), Bordereau.apporteur_id.is_not(None)))
        .order_by(Bordereau.periode, Bordereau.reference)
    )
    return [
        {
            "reference": reference,
            "type": type_bordereau.value,
            "periode": periode.isoformat(),
            "nombre_lignes": nombre,
            "montant_cents": int(montant),
            "destinataire": code_compagnie or code_apporteur,
        }
        for reference, type_bordereau, periode, nombre, montant, code_compagnie, code_apporteur in session.execute(
            requete
        )
    ]
