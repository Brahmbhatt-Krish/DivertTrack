# On Windows, `make` is often launched from PowerShell/cmd, which would
# otherwise make it run recipes through cmd.exe (no trap/&/wait support).
# Point SHELL at a real bash.exe so recipes behave the same from any shell.
# Detection lives in scripts/find-bash.ps1, not inline here: a Windows
# profile path containing a space (e.g. "Krish Brahmbhatt") gets torn into
# two words by Make's own $(wildcard)/$(firstword) argument splitting, and
# a hand-rolled cmd/PowerShell one-liner is nearly impossible to quote
# correctly once it is itself embedded inside a Makefile recipe string.
ifeq ($(OS),Windows_NT)
BASH_EXE := $(strip $(shell powershell -NoProfile -ExecutionPolicy Bypass -File "scripts/find-bash.ps1" 2>NUL))
ifneq ($(strip $(BASH_EXE)),)
SHELL := $(BASH_EXE)
endif
endif
# Every target runs the project's own .venv once it exists (created by
# install-backend), never whatever "python" happens to mean globally — the
# global interpreter has none of our dependencies installed.
ifeq ($(OS),Windows_NT)
VENV_PYTHON := $(CURDIR)/.venv/Scripts/python.exe
else
VENV_PYTHON := $(CURDIR)/.venv/bin/python
endif
ifeq ($(origin PYTHON),undefined)
PYTHON := $(if $(wildcard $(VENV_PYTHON)),$(VENV_PYTHON),python)
endif

.PHONY: install install-backend install-frontend dev dev-backend dev-frontend test test-fuzz demo clean

install: install-backend install-frontend

install-backend:
	python -m venv .venv
	$(VENV_PYTHON) -m pip install --upgrade pip
	$(VENV_PYTHON) -m pip install -r backend/requirements.txt

install-frontend:
	cd frontend && npm install

# Runs uvicorn (backend) and vite (frontend) together; Ctrl+C stops both.
dev:
	@trap 'kill 0' EXIT INT TERM; \
	(cd backend && $(PYTHON) -m uvicorn app.main:app --reload --port 8000) & \
	(cd frontend && npm run dev) & \
	wait

dev-backend:
	cd backend && $(PYTHON) -m uvicorn app.main:app --reload --port 8000

dev-frontend:
	cd frontend && npm run dev

test:
	cd backend && $(PYTHON) -m pytest -q

# Fuzz suite only (Phase 6+), run on its own since it is the slowest file.
test-fuzz:
	cd backend && $(PYTHON) -m pytest -q tests/test_fuzz.py

# Backend with human-visible timing (see .env.example vs. demo overrides in README).
demo:
	cd backend && $(PYTHON) -m uvicorn app.main:app --port 8000

clean:
	find backend -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf frontend/node_modules frontend/dist
