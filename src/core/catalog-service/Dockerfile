# Not wired into a Deployment yet — see README.md's "Not yet built" list.
# This image is deliberately usable on its own already (`docker build` +
# `docker run` against any reachable Postgres) so the container story isn't
# blocked on the registry/Deployment decision that comes next.
#
# Same image serves two roles depending on the command it's run with:
#   docker run <image>                              # uvicorn (default CMD)
#   docker run <image> alembic upgrade head          # migrations
# That's intentional, not a shortcut — a K8s Job running migrations and the
# Deployment running the API should always be running identical code, and
# "identical code" is easiest to guarantee by literally being the same image.
FROM python:3.12-slim AS base

WORKDIR /srv

# System deps for psycopg[binary] are none (it ships its own libpq), so this
# stays a single stage — revisit if a future dependency needs a build step
# the slim image doesn't have.
COPY pyproject.toml ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

RUN pip install --no-cache-dir --root-user-action=ignore .

# Runs as a non-root uid — no reason for this process to be root, and
# nothing here needs to bind under 1024 (uvicorn's default 8000).
RUN useradd --system --uid 10001 catalog
USER catalog

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
