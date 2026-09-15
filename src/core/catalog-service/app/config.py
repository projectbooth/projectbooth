"""Settings, read from environment / .env — see .env.example.

Two ways to configure the database connection, both supported:

- DATABASE_URL — a complete connection string. What .env.example uses for
  local dev, where you picked the password yourself and know it doesn't
  contain anything URL-unsafe.
- PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD — separate components, combined
  via SQLAlchemy's URL.create() (see the database_url property below).
  What ../../argocd/manifests/catalog-service.yaml's Deployment uses,
  reading PGUSER/PGPASSWORD out of the platform-postgres-catalog-credentials
  Secret (bootstrap/install.sh generates it with `openssl rand -base64 24`,
  see postgres-cluster.yaml's managed.roles) — that password can and does
  contain "+", "/", "=" characters that are NOT safe to splice unescaped
  into "postgresql://user:PASSWORD@host/db" (a "/" in the password alone
  would silently corrupt the URL's path segment). URL.create() percent-
  encodes each component correctly instead of gambling on what random
  bytes never happen to contain. Worth getting right once here rather than
  risking a connection string that's subtly wrong depending on which random
  password bootstrap/install.sh happened to generate.
"""
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url_raw: str | None = Field(default=None, alias="DATABASE_URL")
    pghost: str | None = None
    pgport: int = 5432
    pgdatabase: str | None = None
    pguser: str | None = None
    pgpassword: str | None = None
    cors_origins: str = ""

    @property
    def database_url(self) -> str:
        if self.pghost and self.pgdatabase and self.pguser:
            return URL.create(
                "postgresql+psycopg",
                username=self.pguser,
                password=self.pgpassword,
                host=self.pghost,
                port=self.pgport,
                database=self.pgdatabase,
            ).render_as_string(hide_password=False)
        if self.database_url_raw:
            return self.database_url_raw
        return "postgresql+psycopg://catalog:catalog-dev-password@localhost:5432/catalog"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(" ") if o.strip()]


settings = Settings()
