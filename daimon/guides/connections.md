---
description: connecting tools. Daimon's add-ons, any other MCP server, secrets and API keys, and chat channels like Telegram
---
# Connections

## Daimon's add-ons

Add-ons are separate programs (text messages, calendar, Canvas, ...) that you reach as MCP servers. The prompt
lists the connected ones with what each is for, and the ones not connected yet. For a connected one,
`guide("<name>")` shows its tools, their consent levels and the events it sends.

- The user connects an add-on with `daimon setup` (or just that step: `daimon config addons`): it installs it, signs in to the account (Google, Microsoft,
  Canvas token, phone pairing) and adds it to `mcp.json` with sensible consent. You can't do the sign-in; when a
  request needs an add-on that isn't connected, say which one and that `daimon setup` connects it.
- Add-ons don't know each other. Connect them with reactions (guide `reactions`), never by asking the user to
  change an add-on's settings.

## Any other MCP server

`add_mcp_server(name, reason, command+args | url, env?, headers?)`. It asks the user (it edits mcp.json).

```
add_mcp_server(name="github", reason="user wants PR summaries",
               url="https://api.githubcopilot.com/mcp/", headers={"Authorization": "Bearer ${GITHUB_TOKEN}"})
add_mcp_server(name="files", reason="read the user's notes folder",
               command="npx", args=["-y", "@modelcontextprotocol/server-filesystem", "C:/Users/me/Notes"])
```

- Its tools appear on your next step as `<name>__<tool>`; check `mcp_status` for errors.
- New tools default to `auto` if the server marks them read-only, else `ask` (guide `consent`). Tighten the
  risky ones right away if the defaults are too loose.
- `remove_mcp_server` disconnects it. `"disabled": true` on its entry keeps it but switches it off.
- A call to one of its tools may take up to `tools.mcp_timeout_s` (config.yaml, default 900s); `"timeout_s": 1800`
  on its entry changes that for one server. Calls that take more than 30s carry on as background jobs (guide
  `jobs`), so a long limit doesn't hold you up.
- Give a worker access by adding the name to its `mcps` list.

## Secrets

Keys and tokens live in the user's secrets file, outside your home folder; you can't read it. Config refers to
them as `${NAME}` and the value is filled in when used. When something needs a key that's missing, tell the
user the variable name to add to the secrets file (or `daimon setup`). Never ask them to paste a secret into
the chat.

## Channels

Where the user reaches you besides the terminal: `channels:` in config.yaml. Telegram needs TELEGRAM_TOKEN and
TELEGRAM_CHAT_ID in the secrets file; `events: [notify, error]` sets which background notices are sent there.
Changes to config.yaml ask, and channels take effect when the agent restarts.

The web page (`web:` in config.yaml) is a channel too: the user chats with you there and watches you work (each
worker's live progress, tool calls, tasks, spend, your change history). `listen` is where it's reachable (localhost,
tailscale, all, or an IP) and `auth` who gets in (token: WEB_TOKEN from the secrets file; tailscale: only their own
Tailscale devices; none). Point the user to `daimon config web` to set it up: it shows the address and a sign-in
link. Never put WEB_TOKEN in a message.
