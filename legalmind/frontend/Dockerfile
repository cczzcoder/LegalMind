FROM node:22-alpine

WORKDIR /web

COPY package.json ./
RUN npm install

COPY index.html tsconfig.json vite.config.ts ./
COPY src ./src

RUN npm run build

USER node

EXPOSE 5173

CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
