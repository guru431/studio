<p align="center">
  <img src="app/static/icons/icon.svg" alt="Studio" width="88" height="88">
</p>

<h1 align="center">Studio</h1>

<p align="center">
  <strong>Your AI workspace, on your own server.</strong><br>
  Projects, images, video, speech, documents, Open WebUI and coding agents — in one installable web app.
</p>

<p align="center">
  <a href="README.md"><strong>English</strong></a> · <a href="README.ru.md">Русский</a><br>
  <a href="#features">Features</a> · <a href="#screenshots">Screenshots</a> ·
  <a href="#getting-started">Getting started</a> · <a href="#documentation">Documentation</a>
</p>

<p align="center">
  <img alt="Self-hosted" src="https://img.shields.io/badge/self--hosted-your_server-c45100">
  <img alt="FastAPI" src="https://img.shields.io/badge/backend-FastAPI-009688">
  <img alt="Installable PWA" src="https://img.shields.io/badge/web-installable_PWA-5265a2">
  <img alt="Claude Code, Codex, OpenCode and DeepSeek Harness" src="https://img.shields.io/badge/agents-Claude_Code_·_Codex_·_OpenCode_·_DeepSeek_Harness-555">
</p>

![Studio coding workspace: project navigation, agent steps, code and chat controls](docs/screenshots/agents.png)

Studio brings everyday AI tools and remote development into a single account. Ask a model about a document, create an image, animate it, transcribe a meeting, translate a scan or continue a coding session from your phone. History, settings and open agent tabs follow your account across devices.

The server runs on infrastructure you control. Connect local models through the **srvllm gateway** or use **OpenAI-compatible services** for supported tasks. The Windows/macOS/Linux bridge connects the agents already installed on your computers, using their existing accounts and project permissions.

**Nine workspaces:** Projects · Images · Video · Text to speech · Transcription · Scans · Translation · Open WebUI · Agents.

> The interface supports Russian and English. Choose a language on the sign-in page or in **Settings → Appearance**. Studio Agent follows the computer's system language (Russian, otherwise English). Set `STUDIO_AGENT_LANG=ru|en` to override it. Screenshots show the English interface in dark mode with fictional demo data.

## Features

- Connect **MCP servers** in Settings. Choose tools per chat or run compatible tools from Images, Video, Text to speech, Transcription, Scans and Translation. Each run in those sections has its own model, status and result history. Open returned files in **My files** or from the result. Coding agents have separate project MCP settings.

### Projects

- Project conversations with shared instructions and memory. Unassigned conversations appear under **No project** and can move between projects.
- Project files with nested folders, multi-file upload, and ZIP export. Results from other workspaces can be added to a project from **My files**.
- Streaming answers with Markdown, tables, syntax highlighting, copy, stop and regenerate.
- Generate another answer from the same prompt, switch between variants and compare them side by side on wider screens. Studio can summarize older turns and continue answers that hit a length limit in server-routed chats.
- Up to 10 attachments per message: images, PDF, DOCX, text and code files; scanned PDFs can pass through OCR before the model answers, and large documents and **knowledge bases** reach the model as excerpts found for the question.
- Tool-call confirmation and model questions as buttons under the answer, a work plan above the answer and optional personal **memory** across conversations.
- Profile and MCP prompt suggestions, a context-usage ring and **Talk** mode: speak and hear the answer read aloud.
- Optional reasoning controls for supported local models, with a collapsible reasoning history.
- Optional web search and page reading through Exa, plus YouTube subtitles. Tool activity and saved sources show what was actually retrieved.
- Send transcripts and recognized documents to a new chat with a prepared question, summary or task extraction prompt.
- Server-routed answers continue after the page closes. Direct browser connections are also available for compatible chat endpoints.
- Export complete conversations to **PDF or Markdown**, including agent tool steps where available, or a chat **ZIP** archive with its files.

### Images

- Text to image and image to image through the gateway, including SNOFS and FLUX. Compatible external image endpoints are also supported.
- Portrait, square and landscape formats; image strength where supported.
- Edit a whole image or paint a mask to change a selected region with the gateway.
- Paste, drop or attach a reference image. The composer keeps it when you switch models or tools. Images saved from compatible MCP tools appear in the main gallery with their prompt and source.
- Generate two to four variations, reuse a seed where supported, save presets and inspect generation parameters.
- Follow prompt and edit chains, browse related variants and compare two images with a draggable before/after divider.
- Describe an image or translate its prompt into English using a local model.
- Preview, grid and table views; select ranges or whole days, download a ZIP and delete in bulk.
- Send an image directly to Video; mobile downloads use the system share sheet where supported.

### Video

- Animate an uploaded image or an image from your Studio history.
- Separate source-image description and motion prompt; optional automatic motion prompt.
- Gateway controls for **1–30 seconds**, **480p / 576p / 720p** and frame interpolation up to **32 fps**.
- Background generation with progress, cancellation, repeat and two to four new variants.
- Playback, prompt chains, preview/grid/table views and bulk downloads.

### Text to speech and voice input

- Turn text into speech with local Qwen3-TTS through the gateway or a compatible external speech service.
- Create voices from a short recording with an automatic, editable ASR transcript, or choose a provider's built-in voice.
- Preview reference voices, listen to completed phrases while synthesis continues and download the final MP3.
- Dictate into chat, media prompts, translation and search. Recognized text is inserted at the cursor for review before sending.
- Voice input audio is processed without being saved as a recording in Studio history.
- Recognition language is Auto, Russian or English; gateway speech synthesis speaks Russian or English (chosen next to the voice). For other languages, use a compatible external service, which takes the language from the text or the recording.
- Long texts are voiced in parts of one chat, with Play all and a single MP3; **Read aloud** a chat answer, translation or scan.

### Transcription

- Upload common audio formats or video with audio, up to **200 MiB / 4 hours** (long ones in parts), or record a meeting right from the microphone with loss protection.
- Speaker separation with gateway WhisperX (Russian recordings); compatible external transcription endpoints are also supported, including for other languages.
- Timestamped utterances linked to the original player; click a line to play that moment.
- Correct text, rename speakers and reset edits without rerunning recognition.
- Export **TXT, DOCX or SRT**, or send the result to Chat to discuss, summarize or extract decisions.
- Background processing, a per-user queue, cancellation and GPU-wait status.

### Scans and OCR

- Recognize PDFs and document photos through gateway HunyuanOCR, up to **50 pages / 20 MiB** per upload.
- Side-by-side page preview and recognized Markdown, including headings, lists and tables.
- Inspect finished pages while the rest of the document is processing.
- Retry failed pages, copy text, export **TXT / Markdown / DOCX** or send the document to Chat.
- **Searchable PDF**: the original pages with invisible recognized text placed on the lines, so any viewer can search and select it.
- The same OCR pipeline handles scanned PDFs attached to Chat and Translation.

### Translation

- Translate text, PDF, DOCX, TXT, Markdown and photos using a selected local or connected model.
- Automatic source-language detection, language swapping and approximately **35 languages**.
- Long text up to **300,000 characters**, split into paragraphs with progress and partial results.
- OCR before translation for scanned PDFs and photos.
- Display translated text **over the original photo**, switch to plain text or save the translated image.
- Persistent history, copy, TXT/DOCX exports, translate again with another language/model and resume interrupted translations.
- Separate queues for text and file/photo work, so a short paragraph can run alongside a long document.
- Optional **Cleaner integration** for chat and translation with data cleaning: a "Via Cleaner" switch on a user's
  own external model connection (the provider key stays in Studio; MCP tools, web search and knowledge keep working,
  "What the model received" shows the cleaned request), an administrator flag that sends every external model
  through Cleaner, and the older Cleaner connection with its own Cleaner account; see the [user guide](docs/guide.ru.md#cleaner-чат-и-перевод-с-очисткой-данных).
- Administrators can require Cleaner for chat web search, page reading and MCP tools marked as web tools.

### Open WebUI

- Connect your own **Open WebUI 0.13+** accounts — several, on one server or on different ones — and use them from Studio on every device you sign in to.
- Pinned chats, nested folders and other chats in a tree, with search by title; changes made in Open WebUI appear while the section is open.
- Read the current branch with Markdown, folded reasoning, tool steps and attachments; continue a chat or start a new one with the model of your choice, attach files and images, and stop an answer.
- Answers stream live; a model question or a tool approval appears as a card under the answer, with a push notification when Studio is not on screen.
- Only the Studio server talks to Open WebUI: the password is never stored, session tokens stay on the server and conversations are not copied into Studio. The administrator adds the server address to the allowlist; a self-signed certificate is trusted by its fingerprint. See the [user guide](docs/guide.ru.md#open-webui).

### Agents: Claude Code, Codex, OpenCode and DeepSeek Harness

Use installed coding agents on your own Windows, macOS or Linux computers from a desktop browser or phone.

- Connect multiple computers; browse projects under each bridge's configured root.
- Inspect project summaries, findings/idea counts and Git state; sort projects and view connected-computer information.
- Open existing CLI and VS Code sessions or create new chats. Model, reasoning effort and access settings are separate for each agent.
- Keep several session tabs, reorder them with mouse, touch or keyboard and restore the workspace on another device.
- Watch streaming text, tool calls, commands, file edits and progress; answer questions and permission requests.
- Send guidance during a task, queue follow-up work, pause/edit the queue and attach images or supported documents.
- Save reusable quick actions and choose the preferred agent order and defaults for new chats.
- Search project files with **@file** suggestions and search local agent conversation content through the bridge.
- Fork supported Claude/Codex conversations, inspect available subagent sessions and create **Git worktrees** as separate projects.
- Launch a draft in a separate worktree from the agent chat's branch menu. [Agents workspace](docs/architecture/agents-workspace.md)
- Share an agent project with another Studio account as an editor or read-only viewer. Set an expiry and change or revoke access from Studio. [Project collaboration](docs/architecture/project-collaboration.md)
- Mirror projects across your connected computers. Review file differences, set shared exclusions and optionally sync changes after a successful agent run. [Project mirrors](docs/guide.ru.md)
- Rename Claude/Codex sessions, delete sessions started in Studio, and close empty or month-old sessions in bulk.
- DeepSeek Harness (npm CLI, Python package or Desktop) runs with the models, providers, MCP servers and memory from its own Web or Desktop profile; Studio lists any profile settings it could not apply and shows the DeepSeek balance for the official provider. A chat open in DeepSeek Harness on the computer cannot be continued from Studio until that app is closed; there is no forking, subagent tree or chat deletion, and access is chosen when the chat is created.
- Browse and edit files, preview Markdown/PDF/images, create folders, rename, move and delete. Conflict checks protect against overwriting a file changed on the computer.
- Inspect Git changes and diffs, branch/upstream status, unpushed commits, configured remotes and public GitHub synchronization.
- Ask the current agent to validate, commit, push or deploy through project-aware Git actions; these run as agent tasks under its permissions.
- View context use, cache information and subscription usage/reset times when the installed agent reports them.
- Open generated HTML/SVG visualizations in an isolated viewer.
- Optionally view and control the Windows desktop: monitors, pointer, keyboard, scrolling and clipboard. Remote desktop access must be enabled at the bridge.
- Protect computer access with a separate PIN, revoke devices and update the bridge after active work finishes.
- Tasks continue on the computer when the browser closes; activity and attention indicators show running, waiting and unread sessions.

Agent features depend on the installed CLI and bridge capabilities. Agent conversations and project files stay on the computer; an explicitly created share snapshot is the encrypted server-side exception.

### Accounts, privacy and sharing

- Administrators can send email invitations or copy a one-time invitation link. Users choose their login and password. Accounts support login by username or email, email password recovery and optional email login codes.
- **Active Directory sign-in** over LDAP with TLS. Administrators configure and test the connection in Studio, link existing accounts and gate access by directory group. Linked accounts keep their Studio history, roles and devices. [Active Directory setup](docs/architecture/ldap.md)
- Administrators can change SMTP settings and test the connection from Studio.
- Isolated user histories and encrypted connection credentials.
- Per-section connections, selected models/defaults and an administrator-controlled allowlist for server-side destinations.
- Password login, brute-force limits, session management and a security event history.
- Authenticator app (TOTP) second step with backup codes that does not depend on email; email notice about sign-ins from a new device.
- Lock the whole app or selected sections using an application PIN, account password or passkey, including device biometrics where supported.
- Emergency sign-out revokes sessions, shared links and notifications, with an option to revoke connected computers.
- Share a read-only conversation snapshot with a **four-digit PIN**, optional expiry and revocation. Creating a new link rotates the old one; links do not grant account access. Update snapshot replaces the content under the same address and PIN.
- The same kind of link shares a transcript (with speakers and times), translation, scan, image or video.
- Shared snapshots are encrypted with the server data key; reasoning is included only when explicitly selected.
- Prompts, conversation/document text, credentials, media and filenames are excluded from service logs.
- Content is sent to the model/provider selected for the task. Local processing requires the corresponding local backend; cloud connections and optional web tools contact external services.

### History, notifications and everyday use

- Full-text search across chat, media prompts, transcripts, scans, translations and speech, with section/model/date filters.
- Agent chat search runs separately on the selected computer through its bridge.
- A task panel for background jobs, cancellation, completed work and agent sessions needing attention.
- Per-device **Web Push** and optional **ntfy** delivery, including Android devices without Google services, with a
  per-device choice of notification kinds, quiet hours and reminders about waiting agent questions.
- Installable PWA with shortcuts, an app-icon badge and files or text received from the system share sheet; touch layouts, keyboard navigation and configurable mobile bottom tabs.
- Light, dark and system themes; four accent colors, text size, UI scale, compact density and account timezone.
- Storage usage, a 14-day **trash** for deleted items, history export, password-confirmed history deletion and an administrator backup/restore check.
- **My files** on MCP servers with one-day personal file links, and **Help** with the Studio guide and MCP server guides.
- Daily consistent SQLite backups with retention. Media is stored separately; SQLite stays on local disk.

## Screenshots

Click any image to open it at full size. The [complete gallery](docs/screenshots/README.md) explains each screen and how to reproduce the captures.

| Chat | Scans and OCR |
| --- | --- |
| [![Chat with formatted answers](docs/screenshots/chat.png)](docs/screenshots/chat.png) | [![Document preview and OCR](docs/screenshots/ocr.png)](docs/screenshots/ocr.png) |
| Translation | Files and Markdown |
| [![Translation and history](docs/screenshots/translation.png)](docs/screenshots/translation.png) | [![Project files and Markdown preview](docs/screenshots/files.png)](docs/screenshots/files.png) |
| Appearance | Active Directory |
| [![Theme and display settings](docs/screenshots/appearance.png)](docs/screenshots/appearance.png) | [![Active Directory settings](docs/screenshots/directory.png)](docs/screenshots/directory.png) |

<p align="center">
  <a href="docs/screenshots/chat-mobile.png"><img src="docs/screenshots/chat-mobile.png" alt="Chat on a phone" width="260"></a>
  &nbsp;
  <a href="docs/screenshots/agents-mobile.png"><img src="docs/screenshots/agents-mobile.png" alt="Coding agent on a phone" width="260"></a>
</p>

## Getting started

Studio is the application layer. Models and inference services are configured separately.

| Component | Requirement |
| --- | --- |
| Server | Linux, Docker Compose and systemd; the supplied production profile also uses an Apache HTTPS reverse proxy |
| Data | Local disk for SQLite; the supplied Docker profile expects a mounted CIFS share for media |
| Models | srvllm gateway for the complete local media/OCR workflow; OpenAI-compatible endpoints for supported individual tasks |
| Coding agents | Windows, macOS or Linux computer, Studio bridge and at least one installed, authenticated Claude Code, Codex or OpenCode CLI, or DeepSeek Harness |
| Optional services | Exa for chat web tools, Web Push or an HTTPS ntfy server for notifications, Cleaner for data cleaning, Active Directory for domain sign-in |
| Deployment workstation | Windows with Git Bash, the project `.venv` and `powershell.exe` (builds the bridge EXE), plus a Mac reachable over SSH to build the macOS PKG |

1. Clone the repository: `git clone https://github.com/guru431/studio.git`.
2. Follow the [Docker deployment guide](docs/architecture/deploy-docker.md) to configure hosts, media mounts, Apache and the ignored `deploy/local-profile.sh`.
3. Provide a persistent `STUDIO_DATA_KEY` and initial administrator credentials using the deployment secret flow described in [publication and local installation](docs/publication.md). Back up the data key alongside the data.
4. Deploy a committed revision with `bash deploy/deploy.sh`; the explicit `--new-secret` and `--create-admin` options provision the key and first account.
5. Sign in, add connections and select models for the sections you want. Add the Windows, macOS or Linux bridge from Agents if you want remote coding.

The supplied deployment scripts target a managed installation with SSH, shared media storage and a separate HTTPS proxy. They require a local profile; this repository does not provision the inference gateway. Addresses and paths in tracked examples are placeholders.

Chat PDF export uses the bundled `deploy/md2pdf.py`. Deployment verifies its SHA-256 against `deploy/md2pdf.sha256` before contacting any host. [Converter requirements](docs/publication.md#конвертер-pdf)

### Development

The frontend uses native ES modules with bundled vendor assets: **no frontend build step or CDN**.

On Windows, from the repository root:

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q
```

The fast suite uses isolated databases and fake backends. Browser, PowerShell and live-model acceptance checks are separate; see the [test contract](docs/architecture/testing.md).

To reproduce the screenshots with Edge/Chromium and the development dependencies installed:

```powershell
.venv\Scripts\python tools/capture_screenshots.py
```

### Batch clients

The [PowerShell](client/studio-batch.ps1) and [Python](client/studio-batch.py) image clients, plus their [video](client/studio-video.py) [counterparts](client/studio-video.ps1), call the gateway directly. They support batch workflows without opening the web app. Python clients use only the standard library and run on Linux/macOS as well as Windows. Gateway keys belong in local configuration, never in tracked scripts.

## Architecture

```mermaid
flowchart LR
    UI["Browser / installed PWA"] --> HTTPS["Apache HTTPS proxy"]
    HTTPS --> NGINX["Restricted nginx ingress"]
    NGINX -->|Unix socket| APP["FastAPI application"]
    APP --> DB[("Local SQLite")]
    APP --> MEDIA[("Media storage")]
    APP --> MODELS["srvllm / allowed model services"]
    APP <-->|REST + socket.io| OWUI["Open WebUI servers"]
    APP <-->|WebSocket| BRIDGE["Windows / macOS / Linux bridge"]
    BRIDGE --> CLI["Claude Code / Codex / OpenCode / DeepSeek Harness"]
    BRIDGE --> PROJECTS["Local projects, files and Git"]
```

The main server uses FastAPI, SQLite/FTS5 and encrypted credentials. The image includes ffmpeg, Chromium and yt-dlp for audio, PDF export and subtitle workflows. The Windows/macOS/Linux bridge is part of this repository under [agent/](agent/).

## Documentation

| Read this | For |
| --- | --- |
| [Русское описание](README.ru.md) | The complete project overview in Russian |
| [User guide (Russian)](docs/guide.ru.md) | Detailed workflows, limits, settings and troubleshooting |
| [Screenshot gallery](docs/screenshots/README.md) | Desktop/mobile captures and reproduction |
| [Architecture index](docs/architecture/README.md) | Current behavior by subsystem |
| [Docker deployment](docs/architecture/deploy-docker.md) | Installation, backups, migration and rollback |
| [Publication and local profile](docs/publication.md) | Public examples, private configuration and secret handling |
| [Windows/macOS/Linux bridge](agent/README.md) | Installation, accounts, access and compatibility |
| [Tests](docs/architecture/testing.md) | Fast, integration and live acceptance suites |
| [Glossary](docs/glossary.md) | UI names and section terminology |
