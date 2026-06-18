FROM node:20-slim

WORKDIR /app

# Install deps first for better layer caching.
COPY package*.json ./
RUN npm install --omit=dev

COPY . .

ENV NODE_ENV=production
EXPOSE 3000

CMD ["node", "src/server.js"]
