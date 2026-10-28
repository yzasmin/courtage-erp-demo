"""Score de risque de resiliation, ecrit chaque mois par le graphe de scoring (module 5).

Une ligne par contrat et par date d'observation : l'historique des scores est conserve, ce qui permet de
voir un risque monter d'un mois sur l'autre et, plus tard, de mesurer le modele sur ses propres
predictions. Les trois facteurs sont des phrases deja redigees : la fiche adherent les affiche telles
quelles, sans connaitre le modele.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from courtage.modeles.base import Base, cle_primaire


class ScoreResiliation(Base):
    __tablename__ = "score_resiliation"
    __table_args__ = (
        UniqueConstraint("contrat_id", "date_observation", name="un_score_par_contrat_et_date"),
        CheckConstraint("probabilite >= 0 AND probabilite <= 1", name="probabilite_bornee"),
        CheckConstraint("decile BETWEEN 1 AND 10", name="decile_valide"),
    )

    id: Mapped[int] = cle_primaire()
    contrat_id: Mapped[int] = mapped_column(ForeignKey("contrat.id"), index=True)
    date_observation: Mapped[date] = mapped_column(Date, index=True)
    probabilite: Mapped[float] = mapped_column(Float)
    #: Decile de risque dans le portefeuille du mois, 10 etant le plus risque.
    decile: Mapped[int] = mapped_column(Integer)
    facteur_1: Mapped[str | None] = mapped_column(String(200))
    facteur_2: Mapped[str | None] = mapped_column(String(200))
    facteur_3: Mapped[str | None] = mapped_column(String(200))
    version_modele: Mapped[str] = mapped_column(String(40))
    calcule_le: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())
