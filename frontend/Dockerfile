# Standalone Railway service for the carrier "device" UI.
# Build stage compiles the Vite app to a single static dist/index.html; the
# runtime stage serves it with `serve`. The adapter's public URL is baked in at
# build time via VITE_ADAPTER_BASE (set it as a Railway service variable).
FROM node:20-slim AS build
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
ARG VITE_ADAPTER_BASE=""
ENV VITE_ADAPTER_BASE=$VITE_ADAPTER_BASE
RUN npm run build

FROM node:20-slim
WORKDIR /app
RUN npm install -g serve
COPY --from=build /app/dist ./dist
ENV PORT=3000
EXPOSE 3000
# Railway injects $PORT; serve the SPA on it.
CMD ["sh", "-c", "serve -s dist -l ${PORT:-3000}"]
