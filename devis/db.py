"""Base de données : plans, analyses, décisions de l'utilisateur, devis émis, paramètres atelier versionnés.

DATABASE_URL :
  - PostgreSQL en déploiement : postgresql+psycopg://devis:***@db:5432/devismaker
  - SQLite par défaut en local : sqlite:///data/devismaker.db

Le schéma est créé au démarrage (create_all). Pour faire évoluer le schéma en production, passer à Alembic.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

ROOT = Path(__file__).resolve().parents[1]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class Plan(Base):
    """Un fichier de plan, identifié par son empreinte (le même fichier déposé deux fois = un seul plan)."""
    __tablename__ = "plans"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)      # sha1 du contenu (16 car.)
    file_name: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer)
    content: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True, deferred=True)  # None si DEVIS_LEARNING=0
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    analyses: Mapped[list["Analysis"]] = relationship(back_populates="plan")


class Analysis(Base):
    """Une analyse d'un plan : ce que la machine a proposé (sortie complète du pipeline)."""
    __tablename__ = "analyses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    duration_s: Mapped[Optional[float]] = mapped_column(Float)
    text_source: Mapped[Optional[str]] = mapped_column(String(64))
    vlm_provider: Mapped[Optional[str]] = mapped_column(String(255))
    vlm_error: Mapped[Optional[str]] = mapped_column(Text)
    review_fields: Mapped[list[Any]] = mapped_column(default=list)     # champs « à vérifier »
    result: Mapped[dict[str, Any]] = mapped_column(deferred=True)      # champs, textes lus, alertes (sans l'aperçu)
    plan: Mapped[Plan] = relationship(back_populates="analyses")
    decisions: Mapped[list["Decision"]] = relationship(back_populates="analysis")


class Decision(Base):
    """Ce que l'utilisateur a fait d'une valeur proposée : validation, correction, infirmation, ajout."""
    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("analyses.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    field: Mapped[str] = mapped_column(String(64), index=True)
    type: Mapped[str] = mapped_column(String(16), index=True)          # validation | correction | infirmation | ajout
    ui_action: Mapped[str] = mapped_column(String(32))                 # confirmation | saisie | choix_sur_plan
    machine_unsure: Mapped[bool] = mapped_column(Boolean)
    machine: Mapped[dict[str, Any]] = mapped_column()                  # valeur, source, confiance, alertes, preuve, journal
    user_value: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
    analysis: Mapped[Analysis] = relationship(back_populates="decisions")


class PricingVersion(Base):
    """Paramètres atelier, historisés : un devis garde la version en vigueur le jour où il a été émis."""
    __tablename__ = "pricing_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    comment: Mapped[Optional[str]] = mapped_column(String(255))
    params: Mapped[dict[str, Any]] = mapped_column()


class Quote(Base):
    """Un devis émis (PDF téléchargé), avec les valeurs et paramètres exacts utilisés."""
    __tablename__ = "quotes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    analysis_id: Mapped[Optional[int]] = mapped_column(ForeignKey("analyses.id"), nullable=True, index=True)
    file_name: Mapped[str] = mapped_column(String(255))
    client: Mapped[Optional[str]] = mapped_column(String(255))
    quantity: Mapped[int] = mapped_column(Integer)
    pricing_version_id: Mapped[Optional[int]] = mapped_column(ForeignKey("pricing_versions.id"), nullable=True)
    pricing_overrides: Mapped[dict[str, Any]] = mapped_column(default=dict)
    values: Mapped[dict[str, Any]] = mapped_column()
    quote: Mapped[dict[str, Any]] = mapped_column()                    # lignes, totaux, hypothèses, alertes
    unit_price: Mapped[float] = mapped_column(Float)
    total_price: Mapped[float] = mapped_column(Float)
    pdf: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)


# --------------------------------------------------------------------------- moteur et sessions
_ENGINE = None
_SESSION: Optional[sessionmaker] = None
_LOCK = threading.Lock()


def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if url:
        return url.replace("postgres://", "postgresql+psycopg://", 1) if url.startswith("postgres://") else url
    (ROOT / "data").mkdir(exist_ok=True)
    return f"sqlite:///{(ROOT / 'data' / 'devismaker.db').as_posix()}"


def engine():
    global _ENGINE, _SESSION
    with _LOCK:
        if _ENGINE is None:
            url = database_url()
            sqlite = url.startswith("sqlite")
            _ENGINE = create_engine(url, pool_pre_ping=True,
                                    connect_args={"check_same_thread": False} if sqlite else {})
            if sqlite:
                @event.listens_for(_ENGINE, "connect")
                def _fk(conn, _):  # clés étrangères actives sous SQLite
                    conn.execute("PRAGMA foreign_keys=ON")
            Base.metadata.create_all(_ENGINE)
            _SESSION = sessionmaker(_ENGINE, expire_on_commit=False)
    return _ENGINE


@contextmanager
def session() -> Iterator[Session]:
    engine()
    s = _SESSION()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def reset_engine() -> None:
    """Tests : repartir d'une base neuve après avoir changé DATABASE_URL."""
    global _ENGINE, _SESSION
    with _LOCK:
        if _ENGINE is not None:
            _ENGINE.dispose()
        _ENGINE, _SESSION = None, None


def ping() -> bool:
    try:
        with engine().connect() as c:
            c.exec_driver_sql("SELECT 1")
        return True
    except Exception:
        return False
