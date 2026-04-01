FROM public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /app

# Install system dependencies if needed


# Copy entire project (respecting .dockerignore)
COPY . .

# Install dependencies




# Install from requirements file
RUN python -m pip install --no-cache-dir -r requirements.txt





# Set AWS region environment variable

ENV AWS_REGION=ap-southeast-2
ENV AWS_DEFAULT_REGION=ap-southeast-2

# Approval workflow configuration
ENV APPROVALS_TABLE=devops-agent-approvals
ENV APPROVAL_API_URL=https://nrp0zqvt76.execute-api.ap-southeast-2.amazonaws.com
ENV SCHEDULER_ROLE_ARN=arn:aws:iam::650251690796:role/devops-agent-scheduler-invoke-role
ENV APPROVAL_HANDLER_ARN=arn:aws:lambda:ap-southeast-2:650251690796:function:devops-agent-approval-handler


# Signal that this is running in Docker for host binding logic
ENV DOCKER_CONTAINER=1


RUN python -m pip install aws_opentelemetry_distro_genai_beta>=0.1.2


# Create non-root user
RUN useradd -m -u 1000 bedrock_agentcore
USER bedrock_agentcore

EXPOSE 8080

# Use the full module path

CMD ["opentelemetry-instrument", "python", "-m", "sns_server"]
