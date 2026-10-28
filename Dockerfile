# Demonstration en ligne (service web Docker gratuit de Render) : l'interface seule, sur une base SQLite
# fournie. Ni PostgreSQL, ni Airflow, ni appel au modele de langage : les dossiers pre-remplis et les
# scores sont deja dans `demo.db`. Le depot de demonstration s'assemble avec deploy/demo/preparer.py.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    COURTAGE_BASE_URL="sqlite+pysqlite:////tmp/courtage.db" \
    COURTAGE_MODE_DEMONSTRATION=true

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

RUN useradd --create-home --uid 1000 courtage
WORKDIR /app

COPY erp/pyproject.toml erp/uv.lock erp/README.md ./
RUN uv sync --frozen --no-dev --extra ia --no-install-project
COPY erp/alembic.ini ./
COPY erp/migrations ./migrations
COPY erp/src ./src
RUN uv sync --frozen --no-dev --extra ia
COPY demo.db /app/demo.db
RUN chown -R courtage:courtage /app

USER courtage
EXPOSE 7860
# La base est recopiee a chaque demarrage : ce que les visiteurs saisissent disparait au redemarrage.
# L'hebergeur impose son port par la variable PORT ; 7860 en local.
CMD ["sh", "-c", "cp /app/demo.db /tmp/courtage.db && exec courtage servir --hote 0.0.0.0 --port ${PORT:-7860}"]
