# Multi-stage — the first one in this repo (gateway/catalog-service are both
# single-stage Python/uvicorn, nothing to copy verbatim here). Build stage
# compiles the Vite/React app to static assets; runtime stage only ever
# serves those assets, so none of the Node toolchain or source ships in the
# final image.

FROM node:22-alpine AS build

WORKDIR /srv

# package*.json copied and installed before the rest of the source so this
# layer only rebuilds when dependencies actually change, not on every
# source edit.
COPY package.json package-lock.json ./
RUN npm ci

COPY index.html vite.config.ts tsconfig*.json ./
COPY public ./public
COPY src ./src
# .env.production carries the real, cluster-constant VITE_KEYCLOAK_* values
# (ui-shell-plan.md item 3) — `vite build` needs it present in this
# directory to bake them into the bundle. Without this line the build still
# succeeds silently but every import.meta.env.VITE_* reference in the
# shipped bundle is undefined — see src/auth/config.ts's requireEnv().
COPY .env.production ./
RUN npm run build

# nginx-unprivileged, not stock nginx: stock nginx needs root to bind port
# 80 and would need manual chown/port-remap steps to run non-root; this
# image already listens on 8080 as a non-root user (uid 101) by default, so
# — unlike gateway's Dockerfile — no useradd/USER block is needed here.
FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime

COPY --from=build /srv/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Matches gateway's own container port convention (gateway.yaml's
# Deployment/Service); see ui-shell.yaml for this service's equivalent.
EXPOSE 8080
