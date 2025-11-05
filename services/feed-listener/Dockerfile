# Stage 1: Runtime image
FROM public.ecr.aws/docker/library/node:20-alpine

# Set working directory
WORKDIR /app

# Copy package definitions
COPY package.json package-lock.json* ./

# Install production-only dependencies
RUN npm ci --omit=dev

# Copy compiled JS output from local build
COPY dist ./dist

# Set the startup command
CMD ["node", "dist/index.js"]
