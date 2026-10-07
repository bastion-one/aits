## aits — developer tasks
##
## Run `make` (or `make help`) for the list of targets.

# --- configuration -----------------------------------------------------------

OPENAPI_GENERATOR_IMAGE := openapitools/openapi-generator-cli:v7.10.0
OPENAPI_FILE            := openapi.json
CLIENT_DIR              := clients/python
# Hand-maintained generator inputs/overrides live OUTSIDE the generated tree, so
# clients/python is fully ephemeral (safe to rm -rf and regenerate).
CLIENT_BUILD            := clients/build
CLIENT_CONFIG           := $(CLIENT_BUILD)/openapi-generator-config.json
CLIENT_IGNORE           := $(CLIENT_BUILD)/.openapi-generator-ignore
CLIENT_PYPROJECT        := $(CLIENT_BUILD)/pyproject.toml
CLIENT_LICENSE_FILES    := LICENSE NOTICE
CLIENT_SHIM             := $(CLIENT_BUILD)/shim_openapi.py
# Derived, generator-only spec (see CLIENT_SHIM). Ephemeral -- never committed,
# rebuilt every `make client`; openapi.json stays the pristine 3.1 source.
CLIENT_OPENAPI          := $(CLIENT_BUILD)/openapi.client.json
CLIENT_BUMP             := $(CLIENT_BUILD)/bump_version.py

# Client publishing -> any PyPI-compatible package registry.
# Set these in the shell or .env; see .env.example for Forgejo-style examples.
PYTHON_PACKAGE_REGISTRY_URL      ?=
PYTHON_PACKAGE_REGISTRY_USERNAME ?=
PYTHON_PACKAGE_REGISTRY_TOKEN    ?=
# Build output for the ephemeral client (clients/python is gitignored, so is dist/).
CLIENT_DIST             := $(CLIENT_DIR)/dist

# Container image built from this FastAPI service. Override either value at call
# time, e.g. `make build-container CONTAINER_TAG=0.1.0`.
CONTAINER_IMAGE         ?= aits
CONTAINER_TAG           ?= latest
CONTAINER_REF           := $(CONTAINER_IMAGE):$(CONTAINER_TAG)

# Container publishing -> any OCI registry (Docker Hub, Forgejo, GHCR, ...).
# Set in the shell or .env; see .env.example. Defaults target example Forgejo.
CONTAINER_REGISTRY_URL       ?= vcs.example.com
CONTAINER_REGISTRY_NAMESPACE ?= example
CONTAINER_REGISTRY_USERNAME  ?= $(CONTAINER_REGISTRY_NAMESPACE)
CONTAINER_REGISTRY_TOKEN     ?=

# Paths black/pylint operate on. clients/python is generated code -- always skipped.
LINT_PATHS              := app tests tools/check_quality.py tools/mutation_probes.py

# Snyk: native uv.lock scanning needs Enterprise + Snyk Preview (enable-uv-cli). Until then,
# snyk-oss exports requirements for pip-mode scanning. snyk-code scopes SAST to app code.
SNYK                    := mise exec snyk -- snyk
SNYK_CODE_PATHS         := $(LINT_PATHS)

# uv.lock uses exclude-newer-span P7D. To bump a package inside that window, pass CLI overrides
# to `make lock` (persisted into uv.lock); routine install/run use --frozen against the lockfile.
UV                      := uv
UV_RUN                  := $(UV) run --frozen

# --- targets -----------------------------------------------------------------

.DEFAULT_GOAL := help

.PHONY: help
help:  ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} \
		/^[a-zA-Z_-]+:.*?## / { printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2 }' \
		$(MAKEFILE_LIST)

.PHONY: deps
deps:  ## Install Python dependencies from uv.lock
	$(UV) sync --frozen

.PHONY: lock
lock:  ## Re-resolve uv.lock (set UV_LOCK_FLAGS for exclude-newer overrides on security bumps)
	@# Example — bump packages younger than the P7D exclude-newer window:
	@#   make lock UV_LOCK_FLAGS='--upgrade-package starlette --exclude-newer-package starlette=false'
	$(UV) lock $(UV_LOCK_FLAGS)

.PHONY: fmt
fmt:  ## Format the codebase in-place with black
	$(UV_RUN) black $(LINT_PATHS)

.PHONY: fmt-check
fmt-check:  ## Verify formatting without writing (CI-friendly)
	$(UV_RUN) black --check $(LINT_PATHS)

.PHONY: lint
lint:  ## Lint the codebase with pylint
	$(UV_RUN) --group quality pylint $(LINT_PATHS)

.PHONY: check
check: fmt-check lint  ## Run all static checks (formatting + lint)

.PHONY: snyk snyk-oss snyk-code
snyk:  ## Run Snyk OSS (deps) and Snyk Code (SAST) scans
	@status=0; \
	$(MAKE) snyk-oss || status=1; \
	$(MAKE) snyk-code || status=1; \
	exit $$status

snyk-oss:  ## Scan Python dependencies with Snyk (pip workaround for uv projects)
	@# Snyk needs pip and SQLAlchemy's greenlet even where uv's platform marker excludes it.
	@set -e; \
	scan_dir=$$(mktemp -d); \
	trap 'rm -rf "$$scan_dir"' EXIT; \
	req="$$scan_dir/requirements.txt"; \
	$(UV) export --frozen --no-dev --no-hashes --format requirements-txt -o "$$req"; \
	$(UV) venv "$$scan_dir/venv"; \
	$(UV) pip install --python "$$scan_dir/venv/bin/python" pip -r "$$req" \
		"$$(sed -n 's/^\(greenlet==[^ ;]*\).*/\1/p' "$$req")"; \
	$(SNYK) test \
		--file="$$req" \
		--package-manager=pip \
		--command="$$scan_dir/venv/bin/python" \
		--project-name=aits

snyk-code:  ## Scan application source with Snyk Code (SAST)
	@status=0; \
	for path in $(SNYK_CODE_PATHS); do \
		echo "==> Snyk Code: $$path"; \
		$(SNYK) code test "$$path" || status=1; \
	done; \
	exit $$status

.PHONY: test
test:  ## Run the fast unit suite (TestClient + in-memory sqlite; excludes integration)
	$(UV_RUN) pytest

.PHONY: quality quality-coverage quality-complexity quality-mutation quality-probes
quality: quality-coverage quality-complexity quality-mutation  ## Enforce all quality thresholds

quality-coverage: regen  ## Require >90% statement/branch coverage and no skipped tests
	mkdir -p quality-results
	$(UV_RUN) --group quality --with ./clients/python coverage run -m pytest -q -rs -p no:cacheprovider -m '' --junitxml=quality-results/tests.xml
	$(UV_RUN) --group quality coverage combine
	$(UV_RUN) --group quality coverage json -o quality-results/coverage.json
	$(UV_RUN) --group quality python tools/check_quality.py coverage

quality-complexity:  ## Require every app function's cyclomatic complexity <20
	$(UV_RUN) --group quality python tools/check_quality.py complexity

quality-probes: regen  ## Kill all 17 hand-written mutation probes, including decorated routes
	$(UV_RUN) --group quality --with ./clients/python python tools/mutation_probes.py

quality-mutation: quality-probes  ## Require assertion kills for >95% of all assessed mutants
	@# Fresh results: cached kills can become stale when tests or dependencies change.
	$(UV_RUN) --group quality python -c 'from pathlib import Path; import shutil; shutil.rmtree("mutants") if Path("mutants").exists() else None'
	$(UV_RUN) --group quality mutmut run --max-children 4
	$(UV_RUN) --group quality mutmut results > quality-results/mutation-results.txt
	$(UV_RUN) --group quality python tools/check_quality.py mutation

.PHONY: test-integration
test-integration: regen  ## Regenerate the SDK, then run the integration suite (spawns uvicorn)
	@# The SDK is not a project dependency; install the freshly-generated client
	@# for this run only. clients/python stays fully ephemeral.
	$(UV_RUN) --with ./clients/python python -m pytest -m integration tests/integration

.PHONY: view
view: regen  ## Render the ledger as a static HTML page (set AITS_VIEW_ARGS, e.g. AITS_VIEW_ARGS=--no-open)
	@# The SDK is not a project dependency; install the freshly-generated client
	@# for this run only. clients/python stays fully ephemeral.
	$(UV_RUN) --with ./clients/python python3 tools/aits_view.py $(AITS_VIEW_ARGS)

.PHONY: db-up
db-up:  ## Start the postgres container (detached) and wait until healthy
	docker compose up -d --wait postgres

.PHONY: db-down
db-down:  ## Stop the postgres container (keeps the volume)
	docker compose down

.PHONY: db-reset
db-reset:  ## Stop the postgres container and delete its data volume
	docker compose down -v

.PHONY: dev
dev: db-up  ## Run the FastAPI app in dev mode (auto-reload); ensures postgres is up
	$(UV_RUN) fastapi dev

.PHONY: build-container
build-container:  ## Build the FastAPI service container image
	docker build -t $(CONTAINER_REF) .

.PHONY: publish-container
publish-container: build-container  ## Publish the container image to an OCI registry
	@set -e; set -a; [ -f .env ] && . ./.env || true; set +a; \
	: "$${CONTAINER_REGISTRY_TOKEN:?ERROR: set CONTAINER_REGISTRY_TOKEN in your environment or .env}"; \
	: "$${CONTAINER_REGISTRY_NAMESPACE:?ERROR: set CONTAINER_REGISTRY_NAMESPACE in your environment or .env}"; \
	registry="$${CONTAINER_REGISTRY_URL:-$(CONTAINER_REGISTRY_URL)}"; \
	namespace="$${CONTAINER_REGISTRY_NAMESPACE:-$(CONTAINER_REGISTRY_NAMESPACE)}"; \
	user="$${CONTAINER_REGISTRY_USERNAME:-$$namespace}"; \
	if [ -z "$$registry" ] || [ "$$registry" = "docker.io" ]; then \
		remote_ref="$$namespace/$(CONTAINER_IMAGE):$(CONTAINER_TAG)"; \
		login_host=""; \
	else \
		remote_ref="$$registry/$$namespace/$(CONTAINER_IMAGE):$(CONTAINER_TAG)"; \
		login_host="$$registry"; \
	fi; \
	echo "==> Publishing $(CONTAINER_REF) to $$remote_ref"; \
	if [ -n "$$login_host" ]; then \
		printf '%s' "$$CONTAINER_REGISTRY_TOKEN" | docker login "$$login_host" --username "$$user" --password-stdin; \
	else \
		printf '%s' "$$CONTAINER_REGISTRY_TOKEN" | docker login --username "$$user" --password-stdin; \
	fi; \
	docker tag $(CONTAINER_REF) "$$remote_ref"; \
	docker push "$$remote_ref"

.PHONY: openapi
openapi:  ## Refresh openapi.json from the in-process FastAPI app
	@echo "==> Dumping OpenAPI spec (in-process) -> $(OPENAPI_FILE)"
	@PYTHONPATH=. $(UV_RUN) python -c 'import json, sys; from app.main import app; json.dump(app.openapi(), sys.stdout, indent=2, sort_keys=True); sys.stdout.write("\n")' > $(OPENAPI_FILE).tmp
	@mv $(OPENAPI_FILE).tmp $(OPENAPI_FILE)

.PHONY: client
client:  ## Generate the Python client into clients/python/ (requires Docker)
	@test -f $(OPENAPI_FILE) || { echo "ERROR: $(OPENAPI_FILE) is missing. Run 'make openapi' first."; exit 1; }
	@command -v docker >/dev/null 2>&1 || { echo "ERROR: docker is required for 'make client'."; exit 1; }
	@echo "==> Regenerating client in $(CLIENT_DIR)/ (ephemeral)"
	rm -rf $(CLIENT_DIR)
	mkdir -p $(CLIENT_DIR)
	@# Pre-seed the ignore file: the generator only honors .openapi-generator-ignore
	@# when it already exists in the output dir (--ignore-file-override is a no-op
	@# in v7.10.0 -- verified). Seeding it from clients/build keeps the source of
	@# truth out of the generated tree.
	cp $(CLIENT_IGNORE) $(CLIENT_DIR)/.openapi-generator-ignore
	@# FastAPI emits binary fields as OpenAPI 3.1 `contentMediaType`, which the
	@# generator can't read -- shim them to `format: binary` in a derived spec so
	@# the server definition and openapi.json stay free of generator workarounds.
	@echo "==> Shimming 3.1 binary fields for the generator -> $(CLIENT_OPENAPI)"
	@$(UV_RUN) python $(CLIENT_SHIM) $(OPENAPI_FILE) $(CLIENT_OPENAPI)
	docker run --rm \
		--user $$(id -u):$$(id -g) \
		-v "$$(pwd):/local" \
		$(OPENAPI_GENERATOR_IMAGE) \
		generate \
			-i /local/$(CLIENT_OPENAPI) \
			-g python \
			-o /local/$(CLIENT_DIR) \
			-c /local/$(CLIENT_CONFIG)
	@rm -f $(CLIENT_OPENAPI)
	@# Overlay the hand-written PEP 621 / setuptools packaging.
	cp $(CLIENT_PYPROJECT) $(CLIENT_DIR)/pyproject.toml
	cp $(CLIENT_LICENSE_FILES) $(CLIENT_DIR)/

.PHONY: regen
regen: openapi client  ## Refresh the spec and regenerate the client

.PHONY: clean-client
clean-client:  ## Remove the generated client tree under clients/python/ (fully ephemeral)
	@echo "==> Removing generated client tree $(CLIENT_DIR)/"
	rm -rf $(CLIENT_DIR)

.PHONY: client-version
client-version:  ## Print the current client package version
	@$(UV_RUN) python $(CLIENT_BUMP) --print

.PHONY: build-client
build-client: regen  ## Regenerate, then build the client sdist + wheel into clients/python/dist/
	@echo "==> Building client distribution -> $(CLIENT_DIST)/"
	uv build $(CLIENT_DIR) --out-dir $(CLIENT_DIST)

.PHONY: publish-client
publish-client: build-client  ## Publish the built client to a PyPI-compatible registry (reads package registry vars from env or .env)
	@# Resolve config from the shell environment, falling back to .env. Strict semver:
	@# most registries reject an existing version (HTTP 409) -- bump first.
	@set -a; [ -f .env ] && . ./.env || true; set +a; \
	: "$${PYTHON_PACKAGE_REGISTRY_URL:?ERROR: set PYTHON_PACKAGE_REGISTRY_URL in your environment or .env}"; \
	: "$${PYTHON_PACKAGE_REGISTRY_TOKEN:?ERROR: set PYTHON_PACKAGE_REGISTRY_TOKEN in your environment or .env}"; \
	user="$${PYTHON_PACKAGE_REGISTRY_USERNAME:-__token__}"; \
	echo "==> Publishing $$($(UV_RUN) python $(CLIENT_BUMP) --print) to $$PYTHON_PACKAGE_REGISTRY_URL"; \
	UV_PUBLISH_USERNAME="$$user" UV_PUBLISH_PASSWORD="$$PYTHON_PACKAGE_REGISTRY_TOKEN" \
		uv publish --publish-url "$$PYTHON_PACKAGE_REGISTRY_URL" $(CLIENT_DIST)/*

.PHONY: release-patch release-minor release-major
release-patch:  ## Bump patch version (in both tracked files) and publish
	@$(UV_RUN) python $(CLIENT_BUMP) patch
	@$(MAKE) publish-client

release-minor:  ## Bump minor version (in both tracked files) and publish
	@$(UV_RUN) python $(CLIENT_BUMP) minor
	@$(MAKE) publish-client

release-major:  ## Bump major version (in both tracked files) and publish
	@$(UV_RUN) python $(CLIENT_BUMP) major
	@$(MAKE) publish-client
