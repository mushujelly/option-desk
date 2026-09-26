FROM node:22-alpine AS frontend
WORKDIR /build
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir '.[live]'
COPY --from=frontend /build/dist ./web/dist
RUN useradd -m desk && mkdir .state && chown -R desk:desk /app
USER desk
EXPOSE 8765
CMD ["option-desk", "serve"]
