# IGNORE THIS

While this is technically built on Nanobot, its pretty much a rewrite. I wanted to add some specific guardrails and state management to Nanobot but the code quality was horrifying, so I wound up building this instead.

Here's what we have:

 1. Unified system prompt (TODO: Where?)
 2. Consistent architecture
   a.
 3. A lot less dead code
 4. Asynchronous tool use (LLM can recieve messages while using tools!)
 5. Easily extensible.
 6. Fewer dumb LLM design choices:
   a. Bus dispatches on multiple queues immediately instead of passing through a contested queue.
   b. Fewer redundant tools (HEARTBEAT now operates with CRON)
   c. Simpler LLM provider configuration

```sh
echo -e "\033[38;2;255;170;0m      _  _      \033[0m"
echo -e "\033[38;2;255;170;0m     / \/ \     \033[38;2;120;170;255m  ____                  _      ____ _                \033[0m"
echo -e "\033[38;2;255;170;0m    ( \033[38;2;0;255;255mO  O\033[38;2;255;170;0m )    \033[38;2;120;170;255m | __ )  ___ _ __   ___| |__  / ___| | __ ___      __\033[0m"
echo -e "\033[38;2;255;170;0m     \033[38;2;255;170;0m| \033[38;2;0;255;255m--\033[38;2;255;170;0m |     \033[38;2;120;170;255m |  _ \ / _ \ '_ \ / __| '_ \| |   | |/ _\` \ \ /\ / /\033[0m"
echo -e "\033[38;2;255;170;0m    / \__/ \    \033[38;2;120;170;255m | |_) |  __/ | | | (__| | | | |___| | (_| |\ V  V / \033[0m"
echo -e "\033[38;2;255;170;0m   _|      |_   \033[38;2;120;170;255m |____/ \___|_| |_|\___|_| |_|\____|_|\__,_| \_/\_/  \033[0m"
echo -e "\033[38;2;255;170;0m  (__/    \__)  \033[0m"
```

TODOS:

1. All data paths in the config.
2. Add support for an external connection interface (a unix socket)
3. Tool ideas:
  a. glob
  b. exec (with async support -- prints last few new lines every 30s)

Make this a heartbeat cron job that fires periodically and asks the agent to read HEARTBEAT.md

HEARTBEAT_JOB_ID = "__heartbeat__"
HEARTBEAT_INTERVAL_S = 30 * 60
HEARTBEAT_PROMPT = """Read HEARTBEAT.md in your workspace (if it exists).
Follow any instructions or tasks listed there.
If nothing needs attention, reply with just: HEARTBEAT_OK"""

---

THIS WHOLE FILE BELOW THIS IS OUTDATED.

---

🐈 __nanobot__ is an __ultra-lightweight__ personal AI assistant inspired by [OpenClaw](https://github.com/openclaw/openclaw)

⚡️ Delivers core agent functionality in just __~4,000__ lines of code — __99% smaller__ than Clawdbot's 430k+ lines.

📏 Real-time line count: __3,536 lines__ (run `bash core_agent_lines.sh` to verify anytime)

## 📢 News

- __2026-02-13__ 🎉 Released v0.1.3.post7 — includes security hardening and multiple improvements. All users are recommended to upgrade to the latest version. See [release notes](https://github.com/HKUDS/nanobot/releases/tag/v0.1.3.post7) for more details.
- __2026-02-12__ 🧠 Redesigned memory system — Less code, more reliable. Join the [discussion](https://github.com/HKUDS/nanobot/discussions/566) about it!
- __2026-02-10__ 🎉 Released v0.1.3.post6 with improvements! Check the updates [notes](https://github.com/HKUDS/nanobot/releases/tag/v0.1.3.post6) and our [roadmap](https://github.com/HKUDS/nanobot/discussions/431).
- __2026-02-09__ 💬 Added Slack, Email, and QQ support — nanobot now supports multiple chat platforms!
- __2026-02-08__ 🔧 Refactored Providers—adding a new LLM provider now takes just 2 simple steps! Check [here](#providers).
- __2026-02-07__ 🚀 Released v0.1.3.post5 with Qwen support & several key improvements! Check [here](https://github.com/HKUDS/nanobot/releases/tag/v0.1.3.post5) for details.
- __2026-02-06__ ✨ Added Moonshot/Kimi provider, Discord integration, and enhanced security hardening!
- __2026-02-05__ ✨ Added Feishu channel, DeepSeek provider, and enhanced scheduled tasks support!
- __2026-02-04__ 🚀 Released v0.1.3.post4 with multi-provider & Docker support! Check [here](https://github.com/HKUDS/nanobot/releases/tag/v0.1.3.post4) for details.
- __2026-02-03__ ⚡ Integrated vLLM for local LLM support and improved natural language task scheduling!
- __2026-02-02__ 🎉 nanobot officially launched! Welcome to try 🐈 nanobot!

## Key Features of nanobot

🪶 __Ultra-Lightweight__: Just ~4,000 lines of core agent code — 99% smaller than Clawdbot.

🔬 __Research-Ready__: Clean, readable code that's easy to understand, modify, and extend for research.

⚡️ __Lightning Fast__: Minimal footprint means faster startup, lower resource usage, and quicker iterations.

💎 __Easy-to-Use__: One-click to deploy and you're ready to go.

## 🏗️ Architecture

<p align="center">
  <img src="nanobot_arch.png" alt="nanobot architecture" width="800">
</p>

## ✨ Features

<table align="center">
  <tr align="center">
    <th><p align="center">📈 24/7 Real-Time Market Analysis</p></th>
    <th><p align="center">🚀 Full-Stack Software Engineer</p></th>
    <th><p align="center">📅 Smart Daily Routine Manager</p></th>
    <th><p align="center">📚 Personal Knowledge Assistant</p></th>
  </tr>
  <tr>
    <td align="center"><p align="center"><img src="case/search.gif" width="180" height="400"></p></td>
    <td align="center"><p align="center"><img src="case/code.gif" width="180" height="400"></p></td>
    <td align="center"><p align="center"><img src="case/scedule.gif" width="180" height="400"></p></td>
    <td align="center"><p align="center"><img src="case/memory.gif" width="180" height="400"></p></td>
  </tr>
  <tr>
    <td align="center">Discovery • Insights • Trends</td>
    <td align="center">Develop • Deploy • Scale</td>
    <td align="center">Schedule • Automate • Organize</td>
    <td align="center">Learn • Memory • Reasoning</td>
  </tr>
</table>

## 📦 Install

__Install from source__ (latest features, recommended for development)

```bash
git clone https://github.com/HKUDS/nanobot.git
cd nanobot
pip install -e .
```

__Install with [uv](https://github.com/astral-sh/uv)__ (stable, fast)

```bash
uv tool install nanobot-ai
```

__Install from PyPI__ (stable)

```bash
pip install nanobot-ai
```

## 🚀 Quick Start

> [!TIP]
> Set your API key in `~/.nanobot/config.json`.
> Get API keys: [OpenRouter](https://openrouter.ai/keys) (Global) · [Brave Search](https://brave.com/search/api/) (optional, for web search)

__1. Initialize__

```bash
nanobot onboard
```

__2. Configure__ (`~/.nanobot/config.json`)

For OpenRouter - recommended for global users:

```json
{
  "providers": {
    "openrouter": {
      "apiKey": "sk-or-v1-xxx"
    }
  },
  "agents": {
    "defaults": {
      "model": "anthropic/claude-opus-4-5"
    }
  }
}
```

__3. Chat__

```bash
nanobot agent -m "What is 2+2?"
```

That's it! You have a working AI assistant in 2 minutes.

## 🖥️ Local Models (vLLM)

Run nanobot with your own local models using vLLM or any OpenAI-compatible server.

__1. Start your vLLM server__

```bash
vllm serve meta-llama/Llama-3.1-8B-Instruct --port 8000
```

__2. Configure__ (`~/.nanobot/config.json`)

```json
{
  "providers": {
    "vllm": {
      "apiKey": "dummy",
      "apiBase": "http://localhost:8000/v1"
    }
  },
  "agents": {
    "defaults": {
      "model": "meta-llama/Llama-3.1-8B-Instruct"
    }
  }
}
```

__3. Chat__

```bash
nanobot agent -m "Hello from my local LLM!"
```

> [!TIP]
> The `apiKey` can be any non-empty string for local servers that don't require authentication.

## 💬 Chat Apps

Talk to your nanobot through Telegram, Discord, WhatsApp, Feishu, Mochat, DingTalk, Slack, Email, or QQ — anytime, anywhere.

| Channel | Setup |
|---------|-------|
| __Telegram__ | Easy (just a token) |
| __Discord__ | Easy (bot token + intents) |
| __WhatsApp__ | Medium (scan QR) |
| __Feishu__ | Medium (app credentials) |
| __Mochat__ | Medium (claw token + websocket) |
| __DingTalk__ | Medium (app credentials) |
| __Slack__ | Medium (bot + app tokens) |
| __Email__ | Medium (IMAP/SMTP credentials) |
| __QQ__ | Easy (app credentials) |

<details>
<summary><b>Telegram</b> (Recommended)</summary>

__1. Create a bot__

- Open Telegram, search `@BotFather`
- Send `/newbot`, follow prompts
- Copy the token

__2. Configure__

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "YOUR_BOT_TOKEN",
      "allowFrom": ["YOUR_USER_ID"]
    }
  }
}
```

> You can find your __User ID__ in Telegram settings. It is shown as `@yourUserId`.
> Copy this value __without the `@` symbol__ and paste it into the config file.

__3. Run__

```bash
nanobot gateway
```

</details>

<details>
<summary><b>Mochat (Claw IM)</b></summary>

Uses __Socket.IO WebSocket__ by default, with HTTP polling fallback.

__1. Ask nanobot to set up Mochat for you__

Simply send this message to nanobot (replace `xxx@xxx` with your real email):

```
Read https://raw.githubusercontent.com/HKUDS/MoChat/refs/heads/main/skills/nanobot/skill.md and register on MoChat. My Email account is xxx@xxx Bind me as your owner and DM me on MoChat.
```

nanobot will automatically register, configure `~/.nanobot/config.json`, and connect to Mochat.

__2. Restart gateway__

```bash
nanobot gateway
```

That's it — nanobot handles the rest!

<br>

<details>
<summary>Manual configuration (advanced)</summary>

If you prefer to configure manually, add the following to `~/.nanobot/config.json`:

> Keep `claw_token` private. It should only be sent in `X-Claw-Token` header to your Mochat API endpoint.

```json
{
  "channels": {
    "mochat": {
      "enabled": true,
      "base_url": "https://mochat.io",
      "socket_url": "https://mochat.io",
      "socket_path": "/socket.io",
      "claw_token": "claw_xxx",
      "agent_user_id": "6982abcdef",
      "sessions": ["*"],
      "panels": ["*"],
      "reply_delay_mode": "non-mention",
      "reply_delay_ms": 120000
    }
  }
}
```

</details>

</details>

<details>
<summary><b>Discord</b></summary>

__1. Create a bot__

- Go to <https://discord.com/developers/applications>
- Create an application → Bot → Add Bot
- Copy the bot token

__2. Enable intents__

- In the Bot settings, enable __MESSAGE CONTENT INTENT__
- (Optional) Enable __SERVER MEMBERS INTENT__ if you plan to use allow lists based on member data

__3. Get your User ID__

- Discord Settings → Advanced → enable __Developer Mode__
- Right-click your avatar → __Copy User ID__

__4. Configure__

```json
{
  "channels": {
    "discord": {
      "enabled": true,
      "token": "YOUR_BOT_TOKEN",
      "allowFrom": ["YOUR_USER_ID"]
    }
  }
}
```

__5. Invite the bot__

- OAuth2 → URL Generator
- Scopes: `bot`
- Bot Permissions: `Send Messages`, `Read Message History`
- Open the generated invite URL and add the bot to your server

__6. Run__

```bash
nanobot gateway
```

</details>

<details>
<summary><b>WhatsApp</b></summary>

Requires __Node.js ≥18__.

__1. Link device__

```bash
nanobot channels login
# Scan QR with WhatsApp → Settings → Linked Devices
```

__2. Configure__

```json
{
  "channels": {
    "whatsapp": {
      "enabled": true,
      "allowFrom": ["+1234567890"]
    }
  }
}
```

__3. Run__ (two terminals)

```bash
# Terminal 1
nanobot channels login

# Terminal 2
nanobot gateway
```

</details>

<details>
<summary><b>Feishu (飞书)</b></summary>

Uses __WebSocket__ long connection — no public IP required.

__1. Create a Feishu bot__

- Visit [Feishu Open Platform](https://open.feishu.cn/app)
- Create a new app → Enable __Bot__ capability
- __Permissions__: Add `im:message` (send messages)
- __Events__: Add `im.message.receive_v1` (receive messages)
  - Select __Long Connection__ mode (requires running nanobot first to establish connection)
- Get __App ID__ and __App Secret__ from "Credentials & Basic Info"
- Publish the app

__2. Configure__

```json
{
  "channels": {
    "feishu": {
      "enabled": true,
      "appId": "cli_xxx",
      "appSecret": "xxx",
      "encryptKey": "",
      "verificationToken": "",
      "allowFrom": []
    }
  }
}
```

> `encryptKey` and `verificationToken` are optional for Long Connection mode.
> `allowFrom`: Leave empty to allow all users, or add `["ou_xxx"]` to restrict access.

__3. Run__

```bash
nanobot gateway
```

> [!TIP]
> Feishu uses WebSocket to receive messages — no webhook or public IP needed!

</details>

<details>
<summary><b>QQ (QQ单聊)</b></summary>

Uses __botpy SDK__ with WebSocket — no public IP required. Currently supports __private messages only__.

__1. Register & create bot__

- Visit [QQ Open Platform](https://q.qq.com) → Register as a developer (personal or enterprise)
- Create a new bot application
- Go to __开发设置 (Developer Settings)__ → copy __AppID__ and __AppSecret__

__2. Set up sandbox for testing__

- In the bot management console, find __沙箱配置 (Sandbox Config)__
- Under __在消息列表配置__, click __添加成员__ and add your own QQ number
- Once added, scan the bot's QR code with mobile QQ → open the bot profile → tap "发消息" to start chatting

__3. Configure__

> - `allowFrom`: Leave empty for public access, or add user openids to restrict. You can find openids in the nanobot logs when a user messages the bot.
> - For production: submit a review in the bot console and publish. See [QQ Bot Docs](https://bot.q.qq.com/wiki/) for the full publishing flow.

```json
{
  "channels": {
    "qq": {
      "enabled": true,
      "appId": "YOUR_APP_ID",
      "secret": "YOUR_APP_SECRET",
      "allowFrom": []
    }
  }
}
```

__4. Run__

```bash
nanobot gateway
```

Now send a message to the bot from QQ — it should respond!

</details>

<details>
<summary><b>DingTalk (钉钉)</b></summary>

Uses __Stream Mode__ — no public IP required.

__1. Create a DingTalk bot__

- Visit [DingTalk Open Platform](https://open-dev.dingtalk.com/)
- Create a new app -> Add __Robot__ capability
- __Configuration__:
  - Toggle __Stream Mode__ ON
- __Permissions__: Add necessary permissions for sending messages
- Get __AppKey__ (Client ID) and __AppSecret__ (Client Secret) from "Credentials"
- Publish the app

__2. Configure__

```json
{
  "channels": {
    "dingtalk": {
      "enabled": true,
      "clientId": "YOUR_APP_KEY",
      "clientSecret": "YOUR_APP_SECRET",
      "allowFrom": []
    }
  }
}
```

> `allowFrom`: Leave empty to allow all users, or add `["staffId"]` to restrict access.

__3. Run__

```bash
nanobot gateway
```

</details>

<details>
<summary><b>Slack</b></summary>

Uses __Socket Mode__ — no public URL required.

__1. Create a Slack app__

- Go to [Slack API](https://api.slack.com/apps) → __Create New App__ → "From scratch"
- Pick a name and select your workspace

__2. Configure the app__

- __Socket Mode__: Toggle ON → Generate an __App-Level Token__ with `connections:write` scope → copy it (`xapp-...`)
- __OAuth & Permissions__: Add bot scopes: `chat:write`, `reactions:write`, `app_mentions:read`
- __Event Subscriptions__: Toggle ON → Subscribe to bot events: `message.im`, `message.channels`, `app_mention` → Save Changes
- __App Home__: Scroll to __Show Tabs__ → Enable __Messages Tab__ → Check __"Allow users to send Slash commands and messages from the messages tab"__
- __Install App__: Click __Install to Workspace__ → Authorize → copy the __Bot Token__ (`xoxb-...`)

__3. Configure nanobot__

```json
{
  "channels": {
    "slack": {
      "enabled": true,
      "botToken": "xoxb-...",
      "appToken": "xapp-...",
      "groupPolicy": "mention"
    }
  }
}
```

__4. Run__

```bash
nanobot gateway
```

DM the bot directly or @mention it in a channel — it should respond!

> [!TIP]
>
> - `groupPolicy`: `"mention"` (default — respond only when @mentioned), `"open"` (respond to all channel messages), or `"allowlist"` (restrict to specific channels).
> - DM policy defaults to open. Set `"dm": {"enabled": false}` to disable DMs.

</details>

<details>
<summary><b>Email</b></summary>

Give nanobot its own email account. It polls __IMAP__ for incoming mail and replies via __SMTP__ — like a personal email assistant.

__1. Get credentials (Gmail example)__

- Create a dedicated Gmail account for your bot (e.g. `my-nanobot@gmail.com`)
- Enable 2-Step Verification → Create an [App Password](https://myaccount.google.com/apppasswords)
- Use this app password for both IMAP and SMTP

__2. Configure__

> - `consentGranted` must be `true` to allow mailbox access. This is a safety gate — set `false` to fully disable.
> - `allowFrom`: Leave empty to accept emails from anyone, or restrict to specific senders.
> - `smtpUseTls` and `smtpUseSsl` default to `true` / `false` respectively, which is correct for Gmail (port 587 + STARTTLS). No need to set them explicitly.
> - Set `"autoReplyEnabled": false` if you only want to read/analyze emails without sending automatic replies.

```json
{
  "channels": {
    "email": {
      "enabled": true,
      "consentGranted": true,
      "imapHost": "imap.gmail.com",
      "imapPort": 993,
      "imapUsername": "my-nanobot@gmail.com",
      "imapPassword": "your-app-password",
      "smtpHost": "smtp.gmail.com",
      "smtpPort": 587,
      "smtpUsername": "my-nanobot@gmail.com",
      "smtpPassword": "your-app-password",
      "fromAddress": "my-nanobot@gmail.com",
      "allowFrom": ["your-real-email@gmail.com"]
    }
  }
}
```

__3. Run__

```bash
nanobot gateway
```

</details>

## 🌐 Agent Social Network

🐈 nanobot is capable of linking to the agent social network (agent community). __Just send one message and your nanobot joins automatically!__

| Platform | How to Join (send this message to your bot) |
|----------|-------------|
| [__Moltbook__](https://www.moltbook.com/) | `Read https://moltbook.com/skill.md and follow the instructions to join Moltbook` |
| [__ClawdChat__](https://clawdchat.ai/) | `Read https://clawdchat.ai/skill.md and follow the instructions to join ClawdChat` |

Simply send the command above to your nanobot (via CLI or any chat channel), and it will handle the rest.

## ⚙️ Configuration

Config file: `~/.nanobot/config.json`

### Providers

> [!TIP]
>
> - __Groq__ provides free voice transcription via Whisper. If configured, Telegram voice messages will be automatically transcribed.
> - __Zhipu Coding Plan__: If you're on Zhipu's coding plan, set `"apiBase": "https://open.bigmodel.cn/api/coding/paas/v4"` in your zhipu provider config.
> - __MiniMax (Mainland China)__: If your API key is from MiniMax's mainland China platform (minimaxi.com), set `"apiBase": "https://api.minimaxi.com/v1"` in your minimax provider config.

| Provider | Purpose | Get API Key |
|----------|---------|-------------|
| `openrouter` | LLM (recommended, access to all models) | [openrouter.ai](https://openrouter.ai) |
| `anthropic` | LLM (Claude direct) | [console.anthropic.com](https://console.anthropic.com) |
| `openai` | LLM (GPT direct) | [platform.openai.com](https://platform.openai.com) |
| `deepseek` | LLM (DeepSeek direct) | [platform.deepseek.com](https://platform.deepseek.com) |
| `groq` | LLM + __Voice transcription__ (Whisper) | [console.groq.com](https://console.groq.com) |
| `gemini` | LLM (Gemini direct) | [aistudio.google.com](https://aistudio.google.com) |
| `minimax` | LLM (MiniMax direct) | [platform.minimax.io](https://platform.minimax.io) |
| `aihubmix` | LLM (API gateway, access to all models) | [aihubmix.com](https://aihubmix.com) |
| `dashscope` | LLM (Qwen) | [dashscope.console.aliyun.com](https://dashscope.console.aliyun.com) |
| `moonshot` | LLM (Moonshot/Kimi) | [platform.moonshot.cn](https://platform.moonshot.cn) |
| `zhipu` | LLM (Zhipu GLM) | [open.bigmodel.cn](https://open.bigmodel.cn) |
| `vllm` | LLM (local, any OpenAI-compatible server) | — |

<details>
<summary><b>Adding a New Provider (Developer Guide)</b></summary>

nanobot uses a __Provider Registry__ (`nanobot/providers/registry.py`) as the single source of truth.
Adding a new provider only takes __2 steps__ — no if-elif chains to touch.

__Step 1.__ Add a `ProviderSpec` entry to `PROVIDERS` in `nanobot/providers/registry.py`:

```python
ProviderSpec(
    name="myprovider",                   # config field name
    keywords=("myprovider", "mymodel"),  # model-name keywords for auto-matching
    env_key="MYPROVIDER_API_KEY",        # env var for LiteLLM
    display_name="My Provider",          # shown in `nanobot status`
    litellm_prefix="myprovider",         # auto-prefix: model → myprovider/model
    skip_prefixes=("myprovider/",),      # don't double-prefix
)
```

__Step 2.__ Add a field to `ProvidersConfig` in `nanobot/config/schema.py`:

```python
class ProvidersConfig(BaseModel):
    ...
    myprovider: ProviderConfig = ProviderConfig()
```

That's it! Environment variables, model prefixing, config matching, and `nanobot status` display will all work automatically.

__Common `ProviderSpec` options:__

| Field | Description | Example |
|-------|-------------|---------|
| `litellm_prefix` | Auto-prefix model names for LiteLLM | `"dashscope"` → `dashscope/qwen-max` |
| `skip_prefixes` | Don't prefix if model already starts with these | `("dashscope/", "openrouter/")` |
| `env_extras` | Additional env vars to set | `(("ZHIPUAI_API_KEY", "{api_key}"),)` |
| `model_overrides` | Per-model parameter overrides | `(("kimi-k2.5", {"temperature": 1.0}),)` |
| `is_gateway` | Can route any model (like OpenRouter) | `True` |
| `detect_by_key_prefix` | Detect gateway by API key prefix | `"sk-or-"` |
| `detect_by_base_keyword` | Detect gateway by API base URL | `"openrouter"` |
| `strip_model_prefix` | Strip existing prefix before re-prefixing | `True` (for AiHubMix) |

</details>

### Security

> For production deployments, set `"restrictToWorkspace": true` in your config to sandbox the agent.

| Option | Default | Description |
|--------|---------|-------------|
| `tools.restrictToWorkspace` | `false` | When `true`, restricts __all__ agent tools (shell, file read/write/edit, list) to the workspace directory. Prevents path traversal and out-of-scope access. |
| `channels.*.allowFrom` | `[]` (allow all) | Whitelist of user IDs. Empty = allow everyone; non-empty = only listed users can interact. |

## CLI Reference

| Command | Description |
|---------|-------------|
| `nanobot onboard` | Initialize config & workspace |
| `nanobot agent -m "..."` | Chat with the agent |
| `nanobot agent` | Interactive chat mode |
| `nanobot agent --no-markdown` | Show plain-text replies |
| `nanobot agent --logs` | Show runtime logs during chat |
| `nanobot gateway` | Start the gateway |
| `nanobot status` | Show status |
| `nanobot channels login` | Link WhatsApp (scan QR) |
| `nanobot channels status` | Show channel status |

Interactive mode exits: `exit`, `quit`, `/exit`, `/quit`, `:q`, or `Ctrl+D`.

<details>
<summary><b>Scheduled Tasks (Cron)</b></summary>

```bash
# Add a job
nanobot cron add --name "daily" --message "Good morning!" --cron "0 9 * * *"
nanobot cron add --name "hourly" --message "Check status" --every 3600

# List jobs
nanobot cron list

# Remove a job
nanobot cron remove <job_id>
```

</details>

## 🐳 Docker

> [!TIP]
> The `-v ~/.nanobot:/root/.nanobot` flag mounts your local config directory into the container, so your config and workspace persist across container restarts.

Build and run nanobot in a container:

```bash
# Build the image
docker build -t nanobot .

# Initialize config (first time only)
docker run -v ~/.nanobot:/root/.nanobot --rm nanobot onboard

# Edit config on host to add API keys
vim ~/.nanobot/config.json

# Run gateway (connects to enabled channels, e.g. Telegram/Discord/Mochat)
docker run -v ~/.nanobot:/root/.nanobot -p 18790:18790 nanobot gateway

# Or run a single command
docker run -v ~/.nanobot:/root/.nanobot --rm nanobot agent -m "Hello!"
docker run -v ~/.nanobot:/root/.nanobot --rm nanobot status
```

## 📁 Project Structure

```
nanobot/
├── agent/          # 🧠 Core agent logic
│   ├── loop.py     #    Agent loop (LLM ↔ tool execution)
│   ├── context.py  #    Prompt builder
│   ├── memory.py   #    Persistent memory
│   ├── skills.py   #    Skills loader
│   ├── subagent.py #    Background task execution
│   └── tools/      #    Built-in tools (incl. spawn)
├── skills/         # 🎯 Bundled skills (github, weather, tmux...)
├── channels/       # 📱 Chat channel integrations
├── bus/            # 🚌 Message routing
├── cron/           # ⏰ Scheduled tasks
├── heartbeat/      # 💓 Proactive wake-up
├── providers/      # 🤖 LLM providers (OpenRouter, etc.)
├── session/        # 💬 Conversation sessions
├── config/         # ⚙️ Configuration
└── cli/            # 🖥️ Commands
```

## 🤝 Contribute & Roadmap

PRs welcome! The codebase is intentionally small and readable. 🤗

__Roadmap__ — Pick an item and [open a PR](https://github.com/HKUDS/nanobot/pulls)!

- [x] __Voice Transcription__ — Support for Groq Whisper (Issue #13)
- [ ] __Multi-modal__ — See and hear (images, voice, video)
- [ ] __Long-term memory__ — Never forget important context
- [ ] __Better reasoning__ — Multi-step planning and reflection
- [ ] __More integrations__ — Calendar and more
- [ ] __Self-improvement__ — Learn from feedback and mistakes

### Contributors

<a href="https://github.com/HKUDS/nanobot/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=HKUDS/nanobot&max=100&columns=12&updated=20260210" alt="Contributors" />
</a>

## ⭐ Star History

<div align="center">
  <a href="https://star-history.com/#HKUDS/nanobot&Date">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=HKUDS/nanobot&type=Date&theme=dark" />
      <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=HKUDS/nanobot&type=Date" />
      <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=HKUDS/nanobot&type=Date" style="border-radius: 15px; box-shadow: 0 0 30px rgba(0, 217, 255, 0.3);" />
    </picture>
  </a>
</div>

<p align="center">
  <em> Thanks for visiting ✨ nanobot!</em><br><br>
  <img src="https://visitor-badge.laobi.icu/badge?page_id=HKUDS.nanobot&style=for-the-badge&color=00d4ff" alt="Views">
</p>

<p align="center">
  <sub>nanobot is for educational, research, and technical exchange purposes only</sub>
</p>
