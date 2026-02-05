.PHONY: help run init notify web docker-build docker-up docker-down docker-logs clean install

# Переменные
PYTHON := python3
PIP := pip3
DOCKER_COMPOSE := docker-compose

# Цвета для вывода
BLUE := \033[0;34m
GREEN := \033[0;32m
YELLOW := \033[0;33m
NC := \033[0m # No Color

help: ## Показать справку по командам
	@echo "$(BLUE)Доступные команды:$(NC)"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  $(GREEN)%-15s$(NC) %s\n", $$1, $$2}'

install: ## Установить зависимости Python
	@echo "$(BLUE)Устанавливаю зависимости...$(NC)"
	$(PIP) install -r requirements.txt
	@echo "$(GREEN)Зависимости установлены!$(NC)"

init: ## Первый запуск - загрузить исторические данные (60 дней)
	@echo "$(BLUE)Загружаю исторические данные за 60 дней...$(NC)"
	$(PYTHON) detector.py --init --days 60
	@echo "$(GREEN)Данные загружены!$(NC)"

init-90: ## Загрузить исторические данные за 90 дней
	@echo "$(BLUE)Загружаю исторические данные за 90 дней...$(NC)"
	$(PYTHON) detector.py --init --days 90
	@echo "$(GREEN)Данные загружены!$(NC)"

run: ## Запустить анализ аномалий (вчерашний день)
	@echo "$(BLUE)Запускаю анализ аномалий...$(NC)"
	$(PYTHON) detector.py
	@echo "$(GREEN)Анализ завершен!$(NC)"

run-today: ## Запустить анализ для сегодняшнего дня
	@echo "$(BLUE)Запускаю анализ для сегодняшнего дня...$(NC)"
	$(PYTHON) detector.py --date $$(date +%Y-%m-%d)
	@echo "$(GREEN)Анализ завершен!$(NC)"

run-date: ## Запустить анализ для конкретной даты (использование: make run-date DATE=2026-01-31)
	@if [ -z "$(DATE)" ]; then \
		echo "$(YELLOW)Укажите дату: make run-date DATE=2026-01-31$(NC)"; \
		exit 1; \
	fi
	@echo "$(BLUE)Запускаю анализ для даты $(DATE)...$(NC)"
	$(PYTHON) detector.py --date $(DATE)
	@echo "$(GREEN)Анализ завершен!$(NC)"

run-force: ## Запустить анализ с принудительным обновлением данных
	@echo "$(BLUE)Запускаю анализ с обновлением данных...$(NC)"
	$(PYTHON) detector.py --force
	@echo "$(GREEN)Анализ завершен!$(NC)"

notify: ## Отправить уведомление в Telegram
	@echo "$(BLUE)Отправляю уведомление в Telegram...$(NC)"
	$(PYTHON) notify.py
	@echo "$(GREEN)Уведомление отправлено!$(NC)"

web: ## Сгенерировать HTML страницу с отчетами
	@echo "$(BLUE)Генерирую веб-страницу...$(NC)"
	$(PYTHON) web/generate_index.py
	@echo "$(GREEN)Страница сгенерирована!$(NC)"

docker-build: ## Собрать Docker образ
	@echo "$(BLUE)Собираю Docker образ...$(NC)"
	$(DOCKER_COMPOSE) build
	@echo "$(GREEN)Образ собран!$(NC)"

docker-up: ## Запустить контейнер через docker-compose
	@echo "$(BLUE)Запускаю контейнер...$(NC)"
	$(DOCKER_COMPOSE) up -d
	@echo "$(GREEN)Контейнер запущен! Веб-интерфейс: http://localhost:8080$(NC)"

docker-down: ## Остановить контейнер
	@echo "$(BLUE)Останавливаю контейнер...$(NC)"
	$(DOCKER_COMPOSE) down
	@echo "$(GREEN)Контейнер остановлен!$(NC)"

docker-logs: ## Показать логи контейнера
	$(DOCKER_COMPOSE) logs -f

docker-restart: docker-down docker-up ## Перезапустить контейнер

docker-rebuild: docker-down docker-build docker-up ## Пересобрать и перезапустить контейнер

clean: ## Очистить кеш и временные файлы
	@echo "$(BLUE)Очищаю временные файлы...$(NC)"
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type f -name "*.pyo" -delete 2>/dev/null || true
	find . -type f -name "*.log" -delete 2>/dev/null || true
	@echo "$(GREEN)Очистка завершена!$(NC)"

clean-data: ## Очистить кешированные данные (будьте осторожны!)
	@echo "$(YELLOW)Очищаю кешированные данные...$(NC)"
	rm -rf data/*.json
	@echo "$(GREEN)Данные очищены!$(NC)"

clean-reports: ## Очистить отчеты
	@echo "$(YELLOW)Очищаю отчеты...$(NC)"
	rm -rf reports/*.txt reports/*.json
	@echo "$(GREEN)Отчеты очищены!$(NC)"

clean-all: clean clean-data clean-reports ## Полная очистка (данные + отчеты + кеш)
	@echo "$(GREEN)Полная очистка завершена!$(NC)"

status: ## Показать статус проекта
	@echo "$(BLUE)Статус проекта:$(NC)"
	@echo "Данные: $$(ls -1 data/*.json 2>/dev/null | wc -l | xargs) файлов"
	@echo "Отчеты: $$(ls -1 reports/*.json 2>/dev/null | wc -l | xargs) файлов"
	@echo "Docker: $$($(DOCKER_COMPOSE) ps -q 2>/dev/null | wc -l | xargs) контейнеров запущено"

test: ## Быстрый тест (проверка импортов)
	@echo "$(BLUE)Проверяю импорты...$(NC)"
	$(PYTHON) -c "import requests; print('✓ requests')"
	$(PYTHON) -c "import detector; print('✓ detector.py')"
	$(PYTHON) -c "import notify; print('✓ notify.py')"
	@echo "$(GREEN)Все импорты работают!$(NC)"
