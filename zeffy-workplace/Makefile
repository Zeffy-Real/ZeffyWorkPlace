# Zeffy-Workplace 常用命令封装

.PHONY: up down vector migrate test lint format dev

up: ## 起 base 依赖（PG+Redis）
	docker compose -f docker-compose.base.yml up -d

down: ## 停并清 base 依赖 volume（重置数据库）
	docker compose -f docker-compose.base.yml down -v

vector: ## 起向量库（P1 启用）
	docker compose -f docker-compose.vector.yml up -d

migrate: ## 执行数据库迁移（alembic，P1 起替代 create_all）
	cd server && uv run alembic upgrade head

revision: ## 生成迁移脚本（须人工核对后再 upgrade）
	cd server && uv run alembic revision --autogenerate -m "latest change"

test: ## 后端测试
	cd server && uv run pytest

lint: ## 静态检查
	cd server && uv run ruff check . && uv run mypy app

dev: ## 起后端服务（单 worker）
	cd server && uv run uvicorn app.main:app --reload --workers 1