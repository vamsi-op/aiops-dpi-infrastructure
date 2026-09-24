# Makefile for AIOps DPI Infrastructure
# Wraps common PowerShell commands for easier workflow
# Windows is the primary environment - these targets call the PowerShell scripts

.PHONY: help setup up down logs test check seed train verify clean

help: ## Show this help message
	@echo "AIOps DPI Infrastructure - Common Commands"
	@echo ""
	@echo "Setup & Start:"
	@echo "  make setup    - Bootstrap development environment (first time setup)"
	@echo "  make up       - Start all containers (17 total)"
	@echo "  make down     - Stop all containers"
	@echo ""
	@echo "Development:"
	@echo "  make logs     - Follow container logs"
	@echo "  make test     - Run all tests (76 unit + integration)"
	@echo "  make check    - Run full check sequence (format, lint, typecheck, test)"
	@echo ""
	@echo "Data & Training:"
	@echo "  make seed     - Generate 28 days of training history"
	@echo "  make train    - Train anomaly detection models"
	@echo ""
	@echo "Verification:"
	@echo "  make verify   - Run all verification scripts"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean    - Remove build artifacts and caches"

setup: ## Bootstrap development environment
	@powershell.exe -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1

up: ## Start all containers
	@powershell.exe -ExecutionPolicy Bypass -File .\scripts\stack.ps1 up -Layer all

down: ## Stop all containers
	@powershell.exe -ExecutionPolicy Bypass -File .\scripts\stack.ps1 down

logs: ## Follow container logs
	@docker compose -f docker-compose.yml -f docker-compose.observability.yml -f docker-compose.aiops.yml logs -f

test: ## Run all tests
	@powershell.exe -ExecutionPolicy Bypass -Command ".\.venv\Scripts\pytest.exe tests\ -v --tb=short"

check: ## Run full check sequence
	@echo "Running code formatting..."
	@powershell.exe -ExecutionPolicy Bypass -Command ".\.venv\Scripts\ruff.exe format ."
	@echo "Running linter..."
	@powershell.exe -ExecutionPolicy Bypass -Command ".\.venv\Scripts\ruff.exe check . --fix"
	@echo "Running type checker..."
	@powershell.exe -ExecutionPolicy Bypass -Command ".\.venv\Scripts\mypy.exe libs\aiops_common\src services --check-untyped-defs"
	@echo "Running tests..."
	@powershell.exe -ExecutionPolicy Bypass -Command ".\.venv\Scripts\pytest.exe tests\ --tb=short"
	@echo ""
	@echo "✓ All checks passed!"

seed: ## Generate 28 days of training history
	@powershell.exe -ExecutionPolicy Bypass -Command ".\.venv\Scripts\python.exe scripts\replay_history.py --days 28"

train: ## Train anomaly detection models
	@powershell.exe -ExecutionPolicy Bypass -Command "Invoke-RestMethod -Method POST -Uri http://localhost:8021/train -ContentType 'application/json' -Body '{\"days\":28}'"

verify: ## Run all verification scripts
	@echo "Verifying infrastructure..."
	@powershell.exe -ExecutionPolicy Bypass -Command ".\.venv\Scripts\python.exe scripts\healthcheck.ps1"
	@echo ""
	@echo "Verifying observability..."
	@powershell.exe -ExecutionPolicy Bypass -Command ".\.venv\Scripts\python.exe scripts\check_observability.py"
	@echo ""
	@echo "Verifying metrics agent..."
	@powershell.exe -ExecutionPolicy Bypass -Command ".\.venv\Scripts\python.exe scripts\check_metrics_agent.py"
	@echo ""
	@echo "Verifying anomaly detector..."
	@powershell.exe -ExecutionPolicy Bypass -Command ".\.venv\Scripts\python.exe scripts\check_anomaly_detector.py"
	@echo ""
	@echo "Verifying traffic predictor..."
	@powershell.exe -ExecutionPolicy Bypass -Command ".\.venv\Scripts\python.exe scripts\check_traffic_predictor.py"
	@echo ""
	@echo "✓ All verifications passed!"

clean: ## Remove build artifacts and caches
	@echo "Cleaning Python caches..."
	@powershell.exe -ExecutionPolicy Bypass -Command "Get-ChildItem -Path . -Include __pycache__,*.pyc,*.pyo -Recurse -Force | Remove-Item -Force -Recurse"
	@echo "Cleaning mypy cache..."
	@powershell.exe -ExecutionPolicy Bypass -Command "if (Test-Path .mypy_cache) { Remove-Item -Path .mypy_cache -Recurse -Force }"
	@echo "Cleaning pytest cache..."
	@powershell.exe -ExecutionPolicy Bypass -Command "if (Test-Path .pytest_cache) { Remove-Item -Path .pytest_cache -Recurse -Force }"
	@echo "Cleaning ML artifacts..."
	@powershell.exe -ExecutionPolicy Bypass -Command "if (Test-Path ml\artifacts) { Remove-Item -Path ml\artifacts\* -Force }"
	@echo ""
	@echo "✓ Cleaned!"
