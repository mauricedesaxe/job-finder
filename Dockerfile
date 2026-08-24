FROM oven/bun:1.2

WORKDIR /app

COPY package.json bun.lock ./
RUN bun install --frozen-lockfile --production --ignore-scripts

COPY . .

ENV NODE_ENV=production
ENV LOG_LEVEL=info

CMD ["bun", "run", "src/container.ts"]
