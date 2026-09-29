PY ?= python3
VENV := .venv
RUNTIME ?= runc

.PHONY: test image venv bench demo-scripted clean-sandboxes

venv:
	$(PY) -m venv $(VENV)
	$(VENV)/bin/pip install -q -e '.[test]'

image:
	docker build -q -t codenode-sandbox:dev sandbox

test: venv image
	$(VENV)/bin/python -m pytest -q --runtime $(RUNTIME)

bench: venv image
	$(VENV)/bin/python -m codenode.bench $(RUNTIME)

demo-scripted: venv image
	$(VENV)/bin/python examples/line3/run_demo.py --scripted

clean-sandboxes:
	$(VENV)/bin/python -c "from codenode.sandbox import janitor; print(janitor(now=float('inf')))"
