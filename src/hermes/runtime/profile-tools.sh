# Alice runtimes (baked by Dockerfile.hermes) — see the alice/runtime-env skill.
#
# /opt/skills/.venv goes FIRST so `python`/`pip` in a skill's terminal session
# resolve to the bundled-skill venv: Hermes's own skills assume plain `python`
# and `pip install`, and the image's system python has neither pip nor a
# writable site-packages. /opt/tools/.venv (our plugins/MCPs) is deliberately
# NOT on PATH — reach it only via tools-python / $TOOLS_PYTHON.
export PATH="/opt/skills/.venv/bin:$PATH:/opt/node_modules/.bin"
export TOOLS_PYTHON=/opt/tools/.venv/bin/python3
