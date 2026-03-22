# uisurf-agent

`uisurf-agent` is a Python package for running UI automation agents against either:

- a web browser via Playwright
- the local desktop via desktop control utilities

The project includes:

- a package-level Typer CLI at [src/uisurf_agent/cli.py](/Users/haruiz/open-source/uisurf-agent/src/uisurf_agent/cli.py)
- A2A server entrypoints for browser and desktop agents
- interactive local execution for manual testing

## Requirements

- Python 3.11+
- `uv`
- Node.js only if you also use the bundled `a2a-inspector`
- Playwright browser binaries installed for browser automation
- a desktop environment if you run the desktop agent

## Install

Create the environment and install dependencies:

```bash
uv sync
```

Install Playwright browsers if needed:

```bash
uv run playwright install
```

## Environment

The package reads configuration from environment variables and `.env` files via `python-dotenv`.

Common variables:

- `MODEL_ID`: Gemini model identifier. Default is `gemini-3-flash-preview`.
- `AGENT_HOST`: default bind host for A2A servers
- `BROWSER_AGENT_PORT`: default browser A2A port, default `8001`
- `DESKTOP_AGENT_PORT`: default desktop A2A port, default `8002`
- `BROWSER_FAST_MODE`: speeds up browser settling by waiting less aggressively
- `INCLUDE_THOUGHTS`: global default for model thought streaming when supported
- `BROWSER_INCLUDE_THOUGHTS`: browser-only override for thought streaming
- `DESKTOP_INCLUDE_THOUGHTS`: desktop-only override for thought streaming
- `DESKTOP_OBSERVATION_DELAY_MS`: delay before each desktop screenshot capture
- `MAX_OBSERVATION_IMAGES`: number of screenshot observations that keep image payloads in history
- `OBSERVATION_SCALE`: default screenshot scale for both agents, from `0 < scale <= 1`
- `BROWSER_OBSERVATION_SCALE`: browser-only screenshot scale override
- `DESKTOP_OBSERVATION_SCALE`: desktop-only screenshot scale override

Screenshot scaling only changes the image sent to the model. Action coordinates
still map to the full browser viewport or desktop resolution.

You will also need the credentials required by the Google client used by the agents.

## CLI

The CLI is implemented with [Typer](https://github.com/fastapi/typer) and is exposed through both:

```bash
uv run uisurf-agent --help
```

Current top-level command:

```bash
uv run uisurf_agent run --help
```

Convenience `make` targets are also available for local A2A server runs:

```bash
make run-browser
make run-desktop
make run-both
```

These targets start long-running local servers instead of one-off tasks. By default they bind to:

```bash
browser: http://127.0.0.1:8001/
desktop: http://127.0.0.1:8002/
```

You can override the bind host and ports from the shell:

```bash
make run-browser HOST=127.0.0.1 BROWSER_PORT=8001
make run-desktop HOST=127.0.0.1 DESKTOP_PORT=8002
make run-both HOST=127.0.0.1 BROWSER_PORT=8001 DESKTOP_PORT=8002
```

If one of those ports is already in use, the `make` target will exit early with a
clear message. Rerun with free ports if needed.

If you want the previous one-off local task mode, use the interactive targets:

```bash
make run-browser-interactive TASK="Open example.com and summarize the page"
make run-desktop-interactive TASK="Open Terminal and run pwd"
```

### Run the browser agent interactively

```bash
uv run uisurf_agent run browser_agent \
  --task "Open example.com and summarize the page" \
  --fast-mode \
  --no-include-thoughts \
  --max-observation-images 2 \
  --observation-scale 0.75 \
  --max-steps 20
```

Run headless:

```bash
uv run uisurf_agent run browser_agent \
  --headless \
  --task "Go to Hacker News and summarize the top 5 stories"
```

### Run the desktop agent interactively

```bash
uv run uisurf_agent run desktop_agent \
  --task "Open Terminal and run pwd" \
  --desktop-observation-delay-ms 750 \
  --no-include-thoughts \
  --max-observation-images 2 \
  --observation-scale 0.75 \
  --max-steps 10
```

Automatically approve safety-gated actions:

```bash
uv run uisurf_agent run desktop_agent \
  --task "Open a text editor and type Hello World" \
  --auto-confirm
```

## A2A Server Mode

Both agents can be exposed as A2A servers through the CLI.

### Browser A2A server

```bash
uv run uisurf_agent run browser_agent \
  --mode a2a \
  --host 0.0.0.0 \
  --port 8080
```

If `--port` is omitted, the browser agent defaults to `8001`.

### Desktop A2A server

```bash
uv run uisurf_agent run desktop_agent \
  --mode a2a \
  --host 0.0.0.0 \
  --port 8081
```

If `--port` is omitted, the desktop agent defaults to `8002`.

## MCP Mode

The CLI accepts `--mode mcp`, but MCP server mode is not implemented yet. The command currently exits with a clear error instead of starting a server.

## Docker

The repository includes a containerized runtime that starts:

- noVNC on port `6080`
- a Chromium instance inside the container with remote debugging on port `9222`
- the browser agent A2A server on port `8001`
- the desktop agent A2A server on port `8002`

### Provide environment variables

The recommended approach is to place secrets and runtime settings in a local `.env` file in the repository root. The wrapper script [run.sh](/Users/haruiz/open-source/uisurf-agent/run.sh) will automatically pass that file to Docker with `--env-file` if it exists.

Example `.env`:

```dotenv
GEMINI_API_KEY=your_key_here
MODEL_ID=gemini-3-flash-preview
AGENT_HOST=0.0.0.0
BROWSER_AGENT_PORT=8001
DESKTOP_AGENT_PORT=8002
BROWSER_FAST_MODE=true
INCLUDE_THOUGHTS=false
DESKTOP_OBSERVATION_DELAY_MS=750
MAX_OBSERVATION_IMAGES=2
OBSERVATION_SCALE=0.75
PASSWORD_REQUIRED=false
```

You can also point the wrapper at a different file:

```bash
ENV_FILE=.env.local sh ./run.sh
```

### Build and run the container

From the repository root:

```bash
sh ./run.sh
```

The script builds the image from [docker/Dockerfile](/Users/haruiz/open-source/uisurf-agent/docker/Dockerfile), starts the container, and publishes the default ports to the host.

Default host endpoints:

- noVNC: `http://localhost:6080`
- browser A2A server: `http://localhost:6080/browser/`
- desktop A2A server: `http://localhost:6080/desktop/`

### Override published ports

The wrapper script supports environment variable overrides:

```bash
PORT=6081 \
BROWSER_A2A_PORT=9001 \
DESKTOP_A2A_PORT=9002 \
sh ./run.sh
```

### View logs

```bash
docker logs -f uisurf-agent-test
```

### Stop the container

```bash
docker rm -f uisurf-agent-test
```

### Notes

- The container startup will fail early if neither `GEMINI_API_KEY` nor `GOOGLE_API_KEY` is provided.
- VNC password auth is disabled by default. Set `PASSWORD_REQUIRED=true` if you want the frontend to require a VNC password again.
- Inside the container, Chromium is started separately and the browser controller connects to it over CDP at `http://127.0.0.1:9222`.
- The desktop and browser agents are both started through the package CLI from [src/uisurf_agent/cli.py](/Users/haruiz/open-source/uisurf-agent/src/uisurf_agent/cli.py).

## Logging

The package configures a Rich-backed library logger in [src/uisurf_agent/__init__.py](/Users/haruiz/open-source/uisurf-agent/src/uisurf_agent/__init__.py). The logger name is `uisurf_agent`.

## Package Layout

- [src/uisurf_agent/cli.py](/Users/haruiz/open-source/uisurf-agent/src/uisurf_agent/cli.py): Typer CLI
- [src/uisurf_agent/a2a/browser_a2a.py](/Users/haruiz/open-source/uisurf-agent/src/uisurf_agent/a2a/browser_a2a.py): browser A2A server
- [src/uisurf_agent/a2a/desktop_a2a.py](/Users/haruiz/open-source/uisurf-agent/src/uisurf_agent/a2a/desktop_a2a.py): desktop A2A server
- [src/uisurf_agent/agents/browser_agent.py](/Users/haruiz/open-source/uisurf-agent/src/uisurf_agent/agents/browser_agent.py): browser automation agent
- [src/uisurf_agent/agents/desktop_agent.py](/Users/haruiz/open-source/uisurf-agent/src/uisurf_agent/agents/desktop_agent.py): desktop automation agent

## Development

Validate the package modules compile:

```bash
PYTHONPYCACHEPREFIX=/tmp/pycache python3 -m compileall src/uisurf_agent
```

Show CLI help locally through the package module:

```bash
PYTHONPATH=src python3 -m uisurf_agent --help
```
