.PHONY: help install dev test lint fmt typecheck migrate revision docker clean

PY ?= python
VENV := .venv
ifeq ($(OS),Windows_NT)
	BIN := $(VENV)/Scripts
else
	BIN := $(VENV)/bin
endif

help: ## 显示全部可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## 创建虚拟环境并安装依赖（含 dev）
	$(PY) -m venv $(VENV)
	$(BIN)/python -m pip install -U pip
	$(BIN)/python -m pip install -e ".[dev]"

dev: ## 启动开发服务器（热重载）
	$(BIN)/python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test: ## 运行测试并统计覆盖率
	$(BIN)/python -m pytest --cov=app --cov-report=term-missing --cov-report=xml

lint: ## 静态检查
	$(BIN)/python -m ruff check src tests

fmt: ## 自动格式化 + 自动修复
	$(BIN)/python -m ruff check --fix src tests
	$(BIN)/python -m ruff format src tests

typecheck: ## mypy 严格模式
	$(BIN)/python -m mypy

migrate: ## 升级数据库到最新版本
	$(BIN)/python -m alembic upgrade head

revision: ## 生成迁移脚本：make revision m="add xxx"
	$(BIN)/python -m alembic revision --autogenerate -m "$(m)"

docker: ## 构建并启动（含 PostgreSQL + Redis）
	docker compose up --build -d

clean: ## 清理缓存与临时产物
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
