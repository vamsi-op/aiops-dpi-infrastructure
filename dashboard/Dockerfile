# Operations dashboard: static React build served by nginx, which also reverse-proxies
# /api, /ws and /health to the API gateway. The browser therefore talks to one origin -
# exactly the setup the relative URLs in src/api/client.ts were written for - so there is
# no CORS configuration and no gateway URL baked into the bundle.
#
#   docker build -t aiops/dashboard:0.1.0 dashboard

FROM node:20.18-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY default.conf.template /etc/nginx/templates/default.conf.template
# Basic auth is off unless the deployment sets a realm (see the template header).
ENV DASHBOARD_AUTH=off
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
