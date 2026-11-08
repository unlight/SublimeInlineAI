# Inline AI for Sublime Text 4

Stream selected text or code through an OpenAI-compatible API and replace the selection as tokens arrive. The editor stays responsive because network I/O and SSE parsing run on a background thread.

## Features

- Works with any selected text or code in the active view.
- `ctrl+enter` opens an instruction panel.
- Streaming replacement with a typing effect.
- Network I/O and SSE parsing run on a background thread.
- Buffer updates run on the main thread through `sublime.set_timeout`.
- Standard library only, no third-party dependencies.
- One OpenAI-compatible endpoint, configurable globally or per project.
- Extension-specific defaults and per-file instruction history.
- Global, user, and project-specific settings.
- API keys from environment variables.
- `Escape` cancels an active stream.
- Detailed errors in the status bar and Sublime Text console.

## Requirements

- Sublime Text 4.
- Embedded Python 3.3 or 3.8.
- An OpenAI-compatible SSE endpoint, for example Ollama, llama.cpp, LM Studio, or DeepSeek.
- No additional Python packages.

## Installation

Package Control is not available yet. Install directly from the repository with `git clone`.

### Linux

```bash
cd "$HOME/.config/sublime-text/Packages"
git clone https://github.com/unlight/SublimeInlineAI.git InlineAI
```

### macOS

```bash
cd "$HOME/Library/Application Support/Sublime Text/Packages"
git clone https://github.com/unlight/SublimeInlineAI.git InlineAI
```

### Windows

```bat
cd "%APPDATA%\Sublime Text\Packages"
git clone https://github.com/unlight/SublimeInlineAI.git InlineAI
```

Remove or rename an existing `Packages/InlineAI` directory or symlink before cloning.

Restart Sublime Text after cloning.

### Update

```bash
cd "<packages-path>/InlineAI"
git pull
```

Replace `<packages-path>` with the path used during installation.

### Settings

Package defaults are in `Packages/InlineAI/InlineAI.sublime-settings`. User overrides belong in `Packages/User/InlineAI.sublime-settings`.

## Usage

1. Select text or code.
2. Press `ctrl+enter`.
3. The panel is prefilled with the last instruction for this file, the closest known file, or the extension default. Edit it or replace it.
4. Press `Enter`.
5. The selection is replaced as tokens arrive.
6. Press `Escape` to cancel an active stream.
7. The generated region is selected when streaming completes.

## Key bindings

`Default.sublime-keymap`:

```json
[
  {
    "keys": ["ctrl+enter"],
    "command": "inline_ai",
    "context": [
      { "key": "selection_empty", "operator": "equal", "operand": false }
    ]
  },
  {
    "keys": ["escape"],
    "command": "inline_ai_cancel",
    "context": [
      { "key": "setting.inline_ai_streaming", "operator": "equal", "operand": true }
    ]
  }
]
```

`ctrl+enter` triggers only when the selection is not empty. `Escape` triggers only while a stream is active.

### Conflict with an existing `ctrl+enter` binding

The user keymap has priority over package keymaps. If `ctrl+enter` is already bound, for example to `goto_file`, the plugin will not receive the event.

Option 1. Restrict the existing binding to empty selections. Keep any other contexts that are already present:

```json
{
  "keys": ["ctrl+enter"],
  "command": "goto_file",
  "context": [
    { "key": "selection_empty", "operator": "equal", "operand": true }
  ]
}
```

Option 2. Add the plugin binding to `Packages/User/Default.sublime-keymap` after the existing binding:

```json
{
  "keys": ["ctrl+enter"],
  "command": "inline_ai",
  "context": [
    { "key": "selection_empty", "operator": "equal", "operand": false }
  ]
}
```

## Settings

Priority:

1. `.sublime-project` key `settings.inline_ai`
2. `Packages/User/InlineAI.sublime-settings`
3. `Packages/InlineAI/InlineAI.sublime-settings`

Project values override user and package values.

### API configuration

```json
{
  "api_url": "http://localhost:11434/v1/chat/completions",
  "api_key": "",
  "model": "llama3",
  "temperature": 0.2,
  "request_timeout": 120,
  "pump_interval_ms": 33,
  "system_prompt": "You are an automated inline text and code transformation tool...",
  "instruction_defaults": {
    "ts,js,mts,mjs,jsx,tsx": "Refactor the selected code for clarity and maintainability without changing its behavior.",
    "md,markdown": "Improve the documentation: clarify the wording, fix grammar, and preserve the original meaning and structure.",
    "default": "Improve the selected text while preserving its meaning and formatting."
  }
}
```

Fields:

- `api_url`: full `http` or `https` URL to `/v1/chat/completions`.
- `api_key`: optional. A literal value or `{env:NAME}`.
- `model`: model name.
- `temperature`: generation temperature.
- `request_timeout`: timeout for one socket operation, in seconds.
- `pump_interval_ms`: UI update interval, in milliseconds.
- `system_prompt`: system prompt.
- `instruction_defaults`: object mapping comma-separated extensions to default instruction text. Must contain a `default` key.

### Instruction defaults and history

`instruction_defaults` maps comma-separated file extensions to the text that prefills the instruction panel. Matching is case-insensitive. The `default` key is required and is used when no extension group matches. A file without an extension uses the `default` group.

The last submitted instruction is kept in memory per file path for the current Sublime Text session.

If the current file has a history entry, that text is used. Otherwise, the plugin selects the stored file with the longest common leading path prefix. Ties use the most recently used file. If there is no history, the extension default is used.

Files without a path (untitled views) do not store history and use the extension default.

History is per file path. Defaults can group several extensions. Restarting Sublime Text clears the history.

### User overrides

Create `Packages/User/InlineAI.sublime-settings` and specify only the keys you want to override:

```json
{
  "api_url": "https://api.deepseek.com/v1/chat/completions",
  "api_key": "{env:DEEPSEEK_API_KEY}",
  "model": "deepseek-chat",
  "temperature": 0.1
}
```

Keys that are absent from the user file are inherited from the package `InlineAI.sublime-settings`.

### Project settings

In `.sublime-project`:

```json
{
  "folders": [
    { "path": "." }
  ],
  "settings": {
    "inline_ai": {
      "api_url": "http://localhost:11434/v1/chat/completions",
      "model": "qwen2.5-coder",
      "temperature": 0.1,
      "system_prompt": "Return only the final result."
    }
  }
}
```

`settings.inline_ai` must be an object. Arrays are rejected.

Keys that are absent from the project settings are inherited from the global settings.

`instruction_defaults` can also be overridden per project. The value replaces the global object as a whole.

### Environment variable API keys

`api_key` supports this format:

```json
"api_key": "{env:DEEPSEEK_API_KEY}"
```

Rules:

- The variable is read when a request is attempted.
- If the variable is not set, the request fails with an error in the status bar and console.
- An empty variable means no `Authorization` header.
- The variable must be present in the environment of the Sublime Text process. If Sublime Text is launched from a GUI, `~/.bashrc` may not apply. Export the variable before launching Sublime Text, or set it in the desktop environment.

```bash
export DEEPSEEK_API_KEY="sk-..."
```

Restart Sublime Text after changing the variable.

Errors:

- `api_key environment variable DEEPSEEK_API_KEY is not set`
- `invalid api_key environment reference`
- `empty environment variable name in api_key`

## Cancellation and undo

- `Escape` during a stream stops generation.
- The partial result is not removed.
- The stream is written in batches, so a full rollback may require several `ctrl+z` presses.
- The generated region is selected after a successful completion.

## Error handling

The plugin reports these conditions in the status bar:

- empty selection;
- empty instruction;
- read-only view;
- no active window;
- external buffer changes during streaming;
- missing or invalid settings;
- HTTP, connection, timeout, and JSON errors;
- the view is closed during streaming;
- the model returns no content.

If the buffer changes externally, the stream stops to avoid corrupting the text.

Full error details are written to the Sublime Text console. Open it with `View` > `Show Console`.

## Project layout

```text
inline_ai.py                    main plugin
InlineAI.sublime-settings       default package settings
Default.sublime-keymap          key bindings
README.md                       documentation
tests/                          unit tests
```

## Verification

```bash
python3 -m py_compile inline_ai.py
python3 -m json.tool InlineAI.sublime-settings
python3 -m json.tool Default.sublime-keymap
python3 -m unittest discover -s tests -v
```

After restarting Sublime Text:

1. Select text.
2. Press `ctrl+enter`.
3. Enter an instruction.
4. Check the stream and the status bar.

## Dependencies

None. Only standard Python modules are used:

- `json`
- `os`
- `queue`
- `socket`
- `threading`
- `urllib.error`
- `urllib.parse`
- `urllib.request`
