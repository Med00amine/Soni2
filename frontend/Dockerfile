# Build the frontend, then serve the built files with nginx.
#
# The previous image ran `vite` (the development server) in production. Besides
# being slow and unminified, Vite refuses requests whose Host header it does not
# recognise, so reaching the site by a server's public address answered
# "Blocked request. This host is not allowed." A built bundle behind nginx has
# no such notion of an allowed host, and nginx also proxies /api to the backend
# so the browser talks to one origin and needs no CORS configuration.
FROM node:22-alpine AS build
WORKDIR /app
COPY package*.json ./
# `npm ci` is reproducible and faster than `npm install` from a lockfile.
RUN npm ci
COPY . .
# Vite bakes VITE_* values in at build time. Left empty, the app calls /api on
# its own origin, which nginx proxies to the backend.
ARG VITE_API_URL=""
ENV VITE_API_URL=$VITE_API_URL
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://127.0.0.1/ >/dev/null 2>&1 || exit 1
