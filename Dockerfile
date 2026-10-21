ARG NODE_IMAGE=node:24.22.0-bookworm-slim@sha256:91acba91b842bec0bf7ea6f5e09c644c7ccad72c4996ed9616d861d1c7cd60d4
FROM ${NODE_IMAGE} AS base
ENV PNPM_HOME=/pnpm
ENV PATH=$PNPM_HOME:$PATH
RUN npm install --global pnpm@11.10.0
WORKDIR /app

FROM base AS build
RUN apt-get update && apt-get install -y --no-install-recommends python3 make g++ && rm -rf /var/lib/apt/lists/*
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY . .
RUN pnpm typecheck && pnpm lint && pnpm test && pnpm build
RUN pnpm prune --prod

FROM ${NODE_IMAGE} AS runtime
ENV NODE_ENV=production
ENV STOCK_ALERT_CONTAINER=true
WORKDIR /app
COPY --from=build --chown=node:node /app/package.json ./package.json
COPY --from=build --chown=node:node /app/node_modules ./node_modules
COPY --from=build --chown=node:node /app/dist ./dist
RUN mkdir /app/data && chown node:node /app/data
USER node
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 CMD ["node", "dist/healthcheck.js"]
CMD ["node", "dist/main.js"]
