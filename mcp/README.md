# RizomUV MCP server

Lets an AI assistant — Claude Desktop, Claude Code, Cursor, any [MCP](https://modelcontextprotocol.io)
client — drive **RizomUV Standalone**: load a mesh, unfold, pack, measure the layout, look at it,
diagnose it, save it.

It attaches to **the RizomUV you already have open** and works on your scene. If none is open it
starts a private headless one, which it closes when the assistant disconnects.

Everything stays on your machine: the client starts this server as a child process and talks to it
over its standard input and output; the server talks to RizomUV over RizomUVLink, on `127.0.0.1`.
Nothing listens on the network, and no mesh data leaves the machine — the assistant receives a few
numbers and, when it asks for one, an image.

## Requirements

* **RizomUV 2027.0 or later**, installed. The server ships with it, and so does the Python it runs on.
* An MCP client.
* Windows for now. The server code ships on macOS and Linux too, but the dependencies it needs
  (`mcp`, `numpy`) and the launcher are Windows-only so far.

## Setting it up

Add one entry to your client's MCP configuration. The path never changes from one RizomUV version to
the next:

```json
{
  "mcpServers": {
    "rizomuv": { "command": "C:\\Program Files\\Rizom Lab\\mcp\\rizomuv-mcp.exe" }
  }
}
```

* **Claude Desktop**: `%APPDATA%\Claude\claude_desktop_config.json`, then restart it.
* **Claude Code**: `claude mcp add rizomuv "C:\Program Files\Rizom Lab\mcp\rizomuv-mcp.exe"`.
* **Cursor** and others: the same block, in the file they document.

`rizomuv-mcp.exe` finds the most recent RizomUV installed and runs the server from it, so an update
of RizomUV needs no change here. To pin one installation, set `RIZOMUV_MCP_APP_DIR` to its folder in
the entry's `env`. `rizomuv-mcp --launcher-info` prints what it resolved (on stderr).

## What the assistant can do

| Tool | What it does |
|---|---|
| `session_info` | Which RizomUV this session drives, its scene, and the instances found on the machine. Starts nothing. |
| `connect` | Choose or switch: the open RizomUV, a private headless one, or one port in particular. |
| `load` | Load a mesh file with its UVs. Replaces the scene — refused on your own RizomUV unless you agreed. |
| `unfold` | Flatten the islands of the working set. |
| `pack` | Lay the islands into the 0–1 tile at a given resolution and padding, then measure the result. |
| `measure` | Coverage, overlap between islands, island sizes, out-of-tile islands, border length, padding share. |
| `diagnose` | The same numbers turned into findings, each naming the lever that fixes it. |
| `render_layout` | A PNG of the layout: island borders, or a distortion heat map; the whole tile or a zoom. |
| `save` | Write the scene with its UVs to a new file. Your current file in RizomUV is left alone. |
| `undo` | Undo the last steps. |
| `run_command` | Any other RizomUV command, behind a policy (see below). |

Resources: `rizomuv://commands` (every command, its category and whether `run_command` passes it),
`rizomuv://command/<name>` (the parameters of one command, read from the running RizomUV) and
`rizomuv://guide/packing` (what actually moves coverage, measured).

Measurements are **independent of RizomUV**: coverage and overlap are recomputed in the server from
the UV coordinates, so they cannot repeat a mistake RizomUV would make about its own work.

## What it will not do to your scene

* **It never closes the RizomUV you opened.** `Quit` and `Exit` are refused; only a headless instance
  the server started itself is closed, when the assistant disconnects.
* **It refuses to load over your scene** unless you say so: loading through the link replaces the
  scene without offering to save it.
* **It saves to new files**, and leaves the file path RizomUV shows you untouched — your next Ctrl+S
  still goes to your own file.
* **On your instance, `run_command` only passes reads, undoable edits and undo.** Preferences, the
  window layout, arbitrary file writes and `Quit` are refused. A private headless instance is allowed
  more, since nothing there is yours.
* **One assistant per RizomUV.** A second server finds the instance taken and starts its own headless
  one instead: two clients on one RizomUVLink port would cross each other's answers.

Two things worth knowing: every command sent over the link marks your scene as modified, so RizomUV
may offer to save when you close it; and a headless instance holds a licence seat while it runs.

## Driving it without an installation (developers)

The server is plain Python. From a checkout of this repository, build its dependencies once:

```
python tools/make_vendor.py --compile-with <RizomUV>\python.exe
```

then point the client at `boot.py` with the Python that has the matching RizomUVLink module:

```json
{ "command": "<RizomUV>\\python.exe",
  "args": ["-I", "-S", "-X", "utf8", "<checkout>\\mcp\\boot.py"],
  "env": { "RIZOMUV_EXE": "<RizomUV>\\rizomuv.exe" } }
```

Options (command line, or the environment variable): `--instance auto|attach|headless`
(`RIZOMUV_MCP_INSTANCE`), `--port <n>` (`RIZOMUV_MCP_PORT`, attach to an instance started with
`-id <n>`), `--exe <path>` (`RIZOMUV_EXE`), `--launch-timeout <s>`, `--log-level`.

Tests: `python -m pytest mcp/tests` (the ones that drive RizomUV skip when no executable is found;
`RIZOMUV_MCP_TEST_EXE` names one).

## How it finds RizomUV

Every RizomUV that listens writes a small file — port, process id, version, a token — into
`%LOCALAPPDATA%\rizomuv\instances`. The server reads those files, checks the process is alive, takes
an exclusive lock on the one it picks, connects, and verifies the token. Since 2027.0 an interactive
RizomUV listens on a local port of its own without being asked; older builds only listen when started
with `-id <port>`, and are reached with `--port`.

## Licence

MIT, like the rest of RizomUVLink. See [LICENSE.md](../LICENSE.md).
