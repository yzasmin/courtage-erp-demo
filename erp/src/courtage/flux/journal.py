"""Journal d'execution par lot : ce que la chaine a fait de chaque fichier recu.

C'est le document que la specification exige, et c'est aussi celui qu'un auditeur demande en premier :
pour chaque fichier arrive, son empreinte, son emplacement d'archive, son verdict, le nombre de lignes
chargees et, en cas de rejet, le motif et les anomalies qui l'ont provoque.

Le journal n'est pas un fichier de trace que la chaine ecrirait au fil de l'eau. Il est **reconstruit a
partir de la base**, par lecture des tables `lot_fichier` et `anomalie_lot`. La difference compte : une
trace ecrite au fil de l'eau peut divergier de l'etat reel apres une reprise sur echec, alors qu'une
lecture de la base dit toujours ce que la base contient. Un lot declare charge dans le journal a donc
reellement ses lignes en base.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from courtage.flux.enums import SeveriteAnomalie, StatutLot
from courtage.flux.modeles import AnomalieLot, LotFichier


def _anomalies_par_lot(session: Session) -> dict[int, list[dict[str, object]]]:
    requete = select(
        AnomalieLot.lot_id,
        AnomalieLot.code,
        AnomalieLot.severite,
        AnomalieLot.ligne_numero,
        AnomalieLot.reference_ligne,
        AnomalieLot.message,
    ).order_by(AnomalieLot.lot_id, AnomalieLot.id)
    par_lot: dict[int, list[dict[str, object]]] = defaultdict(list)
    for lot_id, code, severite, ligne, reference, message in session.execute(requete):
        par_lot[lot_id].append(
            {
                "code": code.value,
                "severite": severite.value,
                "ligne_numero": ligne,
                "reference_ligne": reference,
                "message": message,
            }
        )
    return par_lot


#: Duree de la fenetre d'alerte, en heures. La chaine tourne une fois par nuit : vingt-quatre heures
#: couvrent exactement un passage, sans rattraper celui de la veille.
FENETRE_ALERTE_HEURES = 24


def debut_de_fenetre(session: Session, heures: int = FENETRE_ALERTE_HEURES) -> datetime:
    """Debut de la fenetre d'alerte, calcule sur l'horloge de la base et non sur celle du poste.

    `lot_fichier.detecte_le` est rempli par `func.now()`, donc par le serveur de base de donnees.
    SQLite rend `CURRENT_TIMESTAMP` en temps universel, PostgreSQL suit le fuseau de son serveur :
    comparer ces horodatages a un minuit local calcule en Python laissait passer, en heure d'ete, les
    lots detectes entre minuit et deux heures. Les deux bornes sont donc lues sur la meme horloge.
    """
    maintenant = session.scalar(select(func.current_timestamp()))
    if isinstance(maintenant, str):  # pragma: no cover - SQLite rend parfois une chaine
        maintenant = datetime.fromisoformat(maintenant.replace("Z", ""))
    if maintenant.tzinfo is not None:
        maintenant = maintenant.replace(tzinfo=None)
    return maintenant - timedelta(hours=heures)


def lots_rejetes_depuis(session: Session, depuis: datetime) -> list[dict[str, object]]:
    """Lots rejetes detectes a partir de l'horodatage donne, du plus recent au plus ancien.

    Sert l'alerte du graphe Airflow. La fenetre est bornee par la date a laquelle le lot a ete
    **traite**, et non par la periode du bordereau ni par sa date de detection : ce qui doit faire
    rougir le graphe d'aujourd'hui, c'est ce qui a ete refuse aujourd'hui.

    La nuance entre les deux horodatages n'est pas theorique. Un lot detecte lundi, dont l'archivage
    echoue, puis rejete mercredi quand la chaine repasse, porte un `detecte_le` de lundi. Borner sur la
    detection le laissait hors de la fenetre de mercredi, et le rejet ne declenchait aucune alerte.
    `traite_le` est pose au moment du verdict ; il reste nul tant qu'aucun verdict n'est rendu, et on
    retombe alors sur la detection.
    """
    date_du_verdict = func.coalesce(LotFichier.traite_le, LotFichier.detecte_le)
    requete = (
        select(
            LotFichier.reference_lot,
            LotFichier.nom_fichier,
            LotFichier.type_flux,
            LotFichier.periode,
            LotFichier.motif_rejet,
            date_du_verdict,
        )
        .where(LotFichier.statut == StatutLot.REJETE, date_du_verdict >= depuis)
        .order_by(date_du_verdict.desc(), LotFichier.reference_lot)
    )
    return [
        {
            "reference_lot": reference,
            "nom_fichier": nom,
            "type_flux": type_flux.value,
            "periode": periode.isoformat(),
            "motif_rejet": motif,
            "rejete_le": rejete.isoformat(),
        }
        for reference, nom, type_flux, periode, motif, rejete in session.execute(requete)
    ]


def construit_journal(session: Session, anomalies_par_lot: int = 5) -> dict[str, object]:
    """Reconstruit le journal d'execution par lot a partir de la base.

    `anomalies_par_lot` borne le nombre d'anomalies citees par lot dans le journal. Les anomalies
    completes restent en base et dans le rapport de detection : un journal de plusieurs milliers de
    lignes ne serait pas relu.
    """
    anomalies = _anomalies_par_lot(session)
    lots: list[dict[str, object]] = []
    comptes: dict[str, int] = defaultdict(int)
    lignes_chargees = 0

    requete = select(LotFichier).order_by(LotFichier.periode, LotFichier.nom_fichier)
    for lot in session.scalars(requete):
        liste = anomalies.get(lot.id, [])
        bloquantes = [a for a in liste if a["severite"] == SeveriteAnomalie.BLOQUANT.value]
        avertissements = [a for a in liste if a["severite"] == SeveriteAnomalie.AVERTISSEMENT.value]
        comptes[lot.statut.value] += 1
        lignes_chargees += lot.nombre_lignes_chargees
        lots.append(
            {
                "reference_lot": lot.reference_lot,
                "nom_fichier": lot.nom_fichier,
                "type_flux": lot.type_flux.value,
                "format": lot.format_fichier.value,
                "periode": lot.periode.isoformat(),
                "sha256": lot.sha256,
                "taille_octets": lot.taille_octets,
                "statut": lot.statut.value,
                "chemin_archive": lot.chemin_archive,
                "lignes_fichier": lot.nombre_lignes_fichier,
                "lignes_chargees": lot.nombre_lignes_chargees,
                "total_declare_cents": lot.montant_total_declare_cents,
                "total_calcule_cents": lot.montant_total_calcule_cents,
                "motif_rejet": lot.motif_rejet,
                "anomalies_bloquantes": len(bloquantes),
                "avertissements": len(avertissements),
                "detail_anomalies": (bloquantes + avertissements)[:anomalies_par_lot],
                "detecte_le": lot.detecte_le.isoformat() if lot.detecte_le else None,
                "traite_le": lot.traite_le.isoformat() if lot.traite_le else None,
            }
        )

    total_anomalies = session.scalar(select(func.count(AnomalieLot.id))) or 0
    bloquantes_total = (
        session.scalar(
            select(func.count(AnomalieLot.id)).where(
                AnomalieLot.severite == SeveriteAnomalie.BLOQUANT
            )
        )
        or 0
    )
    return {
        "nombre_lots": len(lots),
        "lots_par_statut": {statut: comptes.get(statut, 0) for statut in sorted(s.value for s in StatutLot)},
        "lignes_chargees": lignes_chargees,
        "anomalies_enregistrees": total_anomalies,
        "anomalies_bloquantes": bloquantes_total,
        "anomalies_avertissement": total_anomalies - bloquantes_total,
        "lots": lots,
    }
