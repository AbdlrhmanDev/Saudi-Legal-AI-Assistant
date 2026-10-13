FROM node:22-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Vite embeds public variables during the build. Railway exposes service
# variables to Docker builds when they are declared as build arguments.
ARG VITE_API_URL
ENV VITE_API_URL=$VITE_API_URL
RUN npm run build

FROM node:22-alpine

WORKDIR /app
RUN npm install --global serve@14.2.5
COPY --from=build /app/dist ./dist

EXPOSE 3000
CMD ["sh", "-c", "exec serve -s dist -l tcp://0.0.0.0:${PORT:-3000}"]
