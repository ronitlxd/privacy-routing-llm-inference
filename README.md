# Privacy Routing for LLM Inference

An in-car assistant prototype that decides, for every driver request, which model should handle it and how much personal data may leave the local system.

- Simple and sensitive requests stay on a **small local model**.
- Complex requests can use a **larger model**, with personal data masked first.
- Restricted data, such as passwords and card numbers, is kept local.

> **Prototype:** music, navigation, calls and messages are simulated. There is no real vehicle or phone integration.

## Requirements

- Ubuntu 22.04 or 24.04
- Python 3.11 or newer, with `venv`
- A model server, such as [Ollama](https://ollama.com), vLLM, LM Studio, llama.cpp or TGI
- At least one small model on that server (no models are included)
- Internet access for Python packages and OpenStreetMap place search and routes

The routing rules were tested with `qwen3:4b-instruct-2507-q4_K_M`. Other models work, but their routing accuracy has not been measured.

## Setup

**1. Install Ollama and a model** (skip this if you already have a model server):

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3:4b-instruct-2507-q4_K_M
```

**2. Install the app:**

```bash
git clone <repository-url> privacy-routing
cd privacy-routing
./install.sh
```

The installer asks a few short questions. Press **Enter** to accept a default, or **b** to go back. It finds your model server, lets you pick models, creates the first driver and starts the site. It never uses `sudo`.

**3. Open the site.**

With the default SSH-tunnel access, run this on your own computer:

```bash
ssh -N -L 8770:127.0.0.1:8770 <user>@<server>
```

Then open <http://localhost:8770/>.

If you chose network access, open `https://<server-address>:8443/`. The browser shows a warning the first time because the certificate is self-signed. If a firewall is on, allow the port with `sudo ufw allow 8443/tcp`. Use this on a trusted internal network only, never on the public internet.

## Everyday commands

| Command | What it does |
|---|---|
| `./install.sh --start` | Start the site |
| `./install.sh --stop` | Stop the site |
| `./install.sh --check` | Check models, keys and privacy rules |
| `./install.sh --models` | Change models |
| `./install.sh --set-key` | Change a cloud API key |
| `./install.sh --access` | Switch between SSH tunnel and HTTPS |
| `./install.sh --update` | Update the app |
| `./uninstall.sh` | Remove the app (asks before deleting data) |

The site does not start automatically after a reboot. Run `./install.sh --start`.

## Troubleshooting

| Problem | Fix |
|---|---|
| Something is not working | Run `./install.sh --check` |
| Need the logs | `journalctl --user -u jlr-privacy-routing -n 50` |
| No model server found | Start it, then run `./install.sh --models` |
| Site cannot be reached from another computer | Check the firewall and use `https://` on port 8443 |

## Known limitation

Some credentials are not detected when written without a pattern such as "PIN is 1234". If you connect a cloud model, do not type real passwords, PINs or card numbers into the assistant.

## More information

- [Full setup guide](SETUP_README.md)
- [How privacy routing works](docs/PRIVACY_ROUTING.md)
- [Architecture](docs/ARCHITECTURE.md)
