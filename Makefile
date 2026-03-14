.PHONY: help install lint format typecheck test test-unit test-integration \
       run-mcp-aws run-mcp-monitoring run-mcp-teams clean \
       commit bump changelog hooks check lint-staged

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

install: ## Install project with dev + infra extras
	pip install -e ".[dev,infra]"

lint: ## Run ruff linter
	ruff check src/ tests/

format: ## Auto-format code with ruff
	ruff format src/ tests/
	ruff check --fix src/ tests/

typecheck: ## Run mypy type checker
	mypy src/

test: ## Run all tests
	pytest -v

test-unit: ## Run unit tests only
	pytest -v -m unit

test-integration: ## Run integration tests only
	pytest -v -m integration

run-mcp-aws: ## Start the AWS Infra MCP server (stdio)
	python -m src.mcp_servers.aws_infra.server

run-mcp-monitoring: ## Start the Monitoring MCP server (stdio)
	python -m src.mcp_servers.monitoring.server

run-mcp-teams: ## Start the Teams MCP server (stdio)
	python -m src.mcp_servers.teams.server

clean: ## Remove caches and build artifacts
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	rm -rf dist/ build/ *.egg-info

# ---------------------------------------------------------------------------
# Commit tooling  (backstage-style: lint-staged + changeset workflow)
# ---------------------------------------------------------------------------
hooks: ## Install pre-commit git hooks (run once after clone)
	pre-commit install --hook-type pre-commit --hook-type commit-msg

lint-staged: ## Run pre-commit hooks on staged files
	@echo ">>> Running pre-commit checks on staged files..."
	@pre-commit run || { echo ""; echo ">>> Files were auto-fixed. Run: git add . && make commit"; exit 1; }
	@echo ">>> All checks passed!"

commit: lint-staged ## Lint, format, then interactive commit
	@echo ""
	@echo ">>> Opening commit prompt..."
	@echo ""
	@set "SKIP=trailing-whitespace,end-of-file-fixer,check-yaml,check-added-large-files,check-merge-conflict,ruff,ruff-format" && cz commit

bump: ## Bump version, update changelog, create git tag
	cz bump

changelog: ## Regenerate CHANGELOG.md from commit history
	cz changelog

check: ## Run all pre-commit hooks on entire codebase
	pre-commit run --all-files
