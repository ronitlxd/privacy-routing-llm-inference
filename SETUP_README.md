# Setting up Privacy Routing for LLM Inference

One command on an Ubuntu server, about ten short questions. Everything (the site, the router, the small model and
the large model) can run on that one server, or the models can stay where they already run.

```bash
git clone <repository> jlr-privacy-routing
cd jlr-privacy-routing
./install.sh
```

Every question shows its default as `(default …)`; **Enter** accepts it and **b** goes back one question.

## What you need first

| Need | Why | If missing |
|---|---|---|
| Ubuntu 22.04 or 24.04 (other Linux works) | tested there | |
| Python 3.11+ with `venv` | the site and the installer | the installer prints `sudo apt install python3 python3-venv` |
| A model server with the models you want, unless you use one elsewhere | runs the router and the small model (any models; none are built in) | the installer explains: `curl -fsSL https://ollama.com/install.sh \| sh`, or start vLLM, llama.cpp, LM Studio or TGI |
| About 8 GB free disk | models (≈2.5 GB small, ≈4.7 GB large) | |
| Internet access | Python packages; OpenStreetMap place search and routes | |

The installer **never runs sudo** and never changes the firewall or system services. When an administrator is
needed, it prints the exact command.

## What the installer does

1. **Checks this machine:** Ubuntu version, Python, memory, GPU and disk.
2. **Creates `.venv`** and installs `requirements.txt` (`cryptography`, `argon2-cffi`).
3. **Finds model servers by itself**, on their usual ports: Ollama (11434), vLLM (8000), LM Studio (1234),
   llama.cpp (8080) and TGI (8080/3000). You are never asked what kind of server you have.
4. **Asks where the models run:**
   1. everything on this server (default);
   2. my small model already runs on another server: type its address. It must be `https://`, or reached
      through an SSH tunnel, because that model reads the driver's private requests;
   3. this server, plus a cloud model for complex requests.
5. **You pick the models; there are no defaults.** The installer lists what the model server actually serves
   (with sizes, for Ollama) and you pick by number or type any model name:
   - the **small model** routes every request and answers the simple and private ones. The routing rules were
     tested with `qwen3:4b-instruct-2507-q4_K_M`; with any other model you get a warning that its routing accuracy
     has not been measured (test a few requests with private details before relying on it);
   - the **large model**: another model on the same server, a cloud model, or none. For a cloud model, the list
     comes from the service itself once your key works.
   - On Ollama, the large model goes on the GPU when both models fit in its memory, otherwise on the CPU (the small
     model keeps the GPU). A model you type that is not installed is downloaded only after you say yes.
6. **Cloud API key (cloud option only):** typed at a hidden prompt, tested with one small request (the list of
   models; no driver text), then saved to `.env`, readable by you only. If a key is already set in the
   environment, you are asked whether to use it. A key is never written to `models.toml`, never printed and
   never logged.
7. **Writes `models.toml` and `.env`.** `models.toml` is checked against the privacy rules before it is saved.
8. **Creates the first driver** (name, then a hidden password of 4 or more characters). Contacts and places
   are added later on the site. Separately, the site creates the demo driver **Alex** (password `1234`, sample
   data only) when it starts, and new sessions open signed in as Alex; `JLR_DEMO_PROFILE=0` in `.env` turns it off.
9. **Asks how the site is opened:**
   1. only through an SSH tunnel (default, safest): the site listens on `127.0.0.1:8770`;
   2. from other computers on this network: HTTPS only, on port 8443, with a self-signed certificate made for
      this server or your own. Plain HTTP on the network is refused. If a firewall is on, run the printed
      `sudo ufw allow 8443/tcp`. Use this on a trusted internal network, never on the internet.
10. **Checks everything:** models reachable, keys present, privacy rules met.
11. **Starts the site in the background** (a systemd user service). It is **not enabled**: it does not start by
    itself after a reboot. Start it again with `./install.sh --start`.

## Opening the site

With SSH-tunnel access (the default), on your own computer:

```bash
ssh -N -L 8770:127.0.0.1:8770 <user>@<server>
```

Then open http://localhost:8770/. The installer prints this line with the right user and server name.

With network access, open `https://<server-address>:8443/`. A self-signed certificate shows a browser warning
the first time.

## Commands

| Command | Does |
|---|---|
| `./install.sh` | guided install (safe to run again) |
| `./install.sh --start` / `--stop` | start or stop the site |
| `./install.sh --check` | check everything: models reachable, keys present, privacy rules met (exit code 1 if something is wrong) |
| `./install.sh --models` | change where the models run (restarts the site if it is running) |
| `./install.sh --set-key` | change a cloud API key (hidden prompt, tested) |
| `./install.sh --access` | switch between SSH tunnel only and HTTPS on the network |
| `./install.sh --update` | `git pull`, reinstall the Python packages, restart the site if it is running |
| `./install.sh --yes --small-model NAME [--large-model NAME] [--preset local\|cloud]` | no questions. The models are named on the command line (there are no defaults); `cloud` needs its key already in the environment (`ANTHROPIC_API_KEY`) |
| `./uninstall.sh` | stop the site, remove its service, release the models; deletes files only after a yes (see below) |

Logs: `journalctl --user -u jlr-privacy-routing -n 50`.

## Uninstall

```bash
./uninstall.sh          # or: ./uninstall.sh --yes  (no questions, deletes nothing)
```

1. **Stops the site** and removes its user service (`~/.config/systemd/user/jlr-privacy-routing.service`).
2. **Releases the models it was using** from memory: on each Ollama server in `models.toml` the router, small
   and large models are unloaded now (`keep_alive 0`). The model files stay on disk. Other model servers (vLLM,
   cloud) manage their own memory and are left alone.
3. **Asks before deleting anything** (default no): first `.venv`, `logs`, `run`, `certs`; then, separately, the
   driver profiles, `.env` (API keys) and `models.toml`. Deleting those cannot be undone.

It never deletes model files, never stops a model server or any system service, never touches anything outside
this folder and never runs sudo. To stop Ollama itself, run `sudo systemctl stop ollama` yourself (if no one else
uses it). To remove the code, delete this folder.

## Files it writes (all git-ignored)

| File | Holds |
|---|---|
| `.venv/` | the Python environment |
| `models.toml` | where each model runs (see `models.example.toml`). No keys. |
| `.env` | site settings and any API key. Readable by you only. |
| `profiles/` | one encrypted database per driver; the password is not stored |
| `certs/` | the HTTPS certificate and key (network access only; the key is readable by you only) |
| `~/.config/systemd/user/jlr-privacy-routing.service` | the site's user service (from `deploy/jlr-privacy-routing.service.example`) |

## Good to know

- **After a reboot** the site is stopped: run `./install.sh --start`. A user service also stops when you log out
  completely, unless an administrator turned on lingering for your user (`loginctl enable-linger <user>`); the
  installer never does.
- **Models elsewhere:** the router and the small model must run on this server or on a private server reached
  over HTTPS (or an SSH tunnel). A public cloud API is refused for them, because they read unmasked requests.
  Only the large model may be a cloud model; it receives masked text only.
- **Private certificate authority:** if the model server's HTTPS certificate is self-signed, the installer asks
  for the `.pem` file that signed it (stored as `ca_file` in `models.toml`).
- **On a laptop or Windows** (development and testing): `python jlr_setup.py` opens the same steps as a menu,
  and `python jlr_setup.py --install` runs the guided install. Without systemd, the site runs as a background
  process (`run/site.pid`, log in `logs/site.log`).
