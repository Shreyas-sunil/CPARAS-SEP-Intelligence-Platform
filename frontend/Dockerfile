# CPARAS — C-DOT SEP Intelligence Platform — Frontend (CPARAS Bulk + CPARAS ISLD)
# Multi-stage build: compile the React/TypeScript app with Vite, then serve
# the static output with nginx (which also reverse-proxies /api to the
# backend container — see nginx.conf).
#
# NOTE: this file did not exist before this pass, despite being referenced
# by docker-compose.yml — `docker compose up --build` could not have worked
# without it.

# ── Build stage ──────────────────────────────────────────────────────────────
FROM node:20-alpine AS build

WORKDIR /app

# Install dependencies first so this layer is cached unless package*.json
# actually changes (avoids a full npm ci on every source edit).
COPY package.json package-lock.json* ./
RUN npm ci

COPY . .
RUN npm run build

# ── Runtime stage ────────────────────────────────────────────────────────────
FROM nginx:1.27-alpine AS runtime

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Unprivileged nginx already runs as the `nginx` user inside this image by
# default; no extra USER directive needed.

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://localhost:80/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
