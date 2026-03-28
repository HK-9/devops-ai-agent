FROM public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /app

# Install deps first (cached unless requirements.txt changes)
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip install --no-cache-dir aws_opentelemetry_distro_genai_beta>=0.1.2

# Copy application code (changes more often — layer after deps)
COPY . .

ENV AWS_REGION=ap-southeast-2
ENV AWS_DEFAULT_REGION=ap-southeast-2
ENV DOCKER_CONTAINER=1

RUN useradd -m -u 1000 bedrock_agentcore
USER bedrock_agentcore

EXPOSE 8080

CMD ["opentelemetry-instrument", "python", "-m", "agent"]
