# Inline AI for Sublime Text 4.
# Install: symlink this folder to Packages/InlineAI
# Keymap: loaded from this package automatically
# User overrides: Packages/User/InlineAI.sublime-settings
# Project overrides: .sublime-project -> settings.inline_ai

import json
import queue
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request

import sublime
import sublime_plugin


SETTINGS_FILE = "InlineAI.sublime-settings"
CONFIG_KEY = "inline_ai"
REQUIRED_SETTINGS = (
    "models",
    "temperature",
    "request_timeout",
    "pump_interval_ms",
    "system_prompt",
)


_SESSIONS = {}
_LAST_WRITE_END = [0]


def _project_overrides(view):
    window = view.window()
    if window is None:
        return {}
    project_data = window.project_data()
    if not isinstance(project_data, dict):
        return {}
    project_settings = project_data.get("settings")
    if not isinstance(project_settings, dict):
        return {}
    overrides = project_settings.get(CONFIG_KEY)
    if isinstance(overrides, list):
        return {"models": overrides}
    return overrides if isinstance(overrides, dict) else {}


def _normalize_models(models):
    if not isinstance(models, list) or not models:
        raise RuntimeError("models must be a non-empty list")

    normalized = []
    for index, item in enumerate(models):
        if not isinstance(item, dict):
            raise RuntimeError("models[{}] must be an object".format(index))
        api_url = str(item.get("api_url") or "").strip()
        model = str(item.get("model") or "").strip()
        api_key = str(item.get("api_key") or "").strip()
        parsed = urllib.parse.urlparse(api_url)
        if not api_url or parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise RuntimeError("models[{}].api_url is invalid".format(index))
        if not model:
            raise RuntimeError("models[{}].model must be non-empty".format(index))
        normalized.append({
            "api_url": api_url,
            "api_key": api_key,
            "model": model,
        })
    return normalized


def _load_config(view):
    settings = sublime.load_settings(SETTINGS_FILE)
    config = {key: settings.get(key) for key in REQUIRED_SETTINGS}

    overrides = _project_overrides(view)
    for key in REQUIRED_SETTINGS:
        if key in overrides:
            config[key] = overrides[key]

    missing = [key for key in REQUIRED_SETTINGS if config.get(key) is None]
    if missing:
        raise RuntimeError("missing settings: " + ", ".join(missing))

    config["models"] = _normalize_models(config["models"])
    config["system_prompt"] = str(config["system_prompt"])

    try:
        config["temperature"] = float(config["temperature"])
        config["request_timeout"] = int(config["request_timeout"])
        config["pump_interval_ms"] = int(config["pump_interval_ms"])
    except (TypeError, ValueError):
        raise RuntimeError(
            "invalid numeric setting: temperature, request_timeout, or pump_interval_ms"
        )

    if config["request_timeout"] <= 0 or config["pump_interval_ms"] <= 0:
        raise RuntimeError("request_timeout and pump_interval_ms must be positive")
    return config


def _model_label(model):
    return "{} @ {}".format(model["model"], model["api_url"])


def _build_headers(model):
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Cache-Control": "no-cache",
        "User-Agent": "SublimeInlineAI/1.0",
    }
    api_key = model["api_key"]
    if api_key:
        token = api_key
        if not token.lower().startswith("bearer "):
            token = "Bearer " + token
        headers["Authorization"] = token
    return headers


def _open_request(config, model, request):
    host = (urllib.parse.urlparse(model["api_url"]).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(request, timeout=config["request_timeout"])
    return urllib.request.urlopen(request, timeout=config["request_timeout"])


def _build_payload(config, model, instruction, source_text):
    user_content = (
        "INSTRUCTION:\n" + instruction +
        "\n\nTEXT OR CODE TO TRANSFORM:\n" + source_text
    )
    return {
        "model": model["model"],
        "stream": True,
        "temperature": config["temperature"],
        "messages": [
            {"role": "system", "content": config["system_prompt"]},
            {"role": "user", "content": user_content},
        ],
    }


def _parse_sse_line(raw_line):
    line = raw_line.rstrip(b"\r").decode("utf-8", "replace").strip()
    if not line or line.startswith(":"):
        return None
    if not line.startswith("data:"):
        return None
    return line[5:].strip()


def _iter_sse_data(response, cancel_event):
    read = getattr(response, "read1", None) or response.read
    buffer = b""
    while not cancel_event.is_set():
        chunk = read(4096)
        if not chunk:
            break
        buffer += chunk
        while b"\n" in buffer:
            raw_line, buffer = buffer.split(b"\n", 1)
            data = _parse_sse_line(raw_line)
            if data is not None:
                yield data
    if buffer and not cancel_event.is_set():
        data = _parse_sse_line(buffer)
        if data is not None:
            yield data


def _extract_content(obj):
    if not isinstance(obj, dict):
        return None
    choices = obj.get("choices")
    if not choices or not isinstance(choices, list):
        return None
    choice = choices[0]
    if not isinstance(choice, dict):
        return None
    delta = choice.get("delta") or {}
    content = delta.get("content")
    if content is None:
        message = choice.get("message") or {}
        content = message.get("content")
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        content = "".join(parts)
    return content if isinstance(content, str) else None


def _extract_api_error(obj):
    if not isinstance(obj, dict):
        return None
    error = obj.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        return message if message else json.dumps(error)
    if error:
        return str(error)
    return None


def _timeout_message(timeout):
    if timeout is None:
        return "request timed out"
    return "request timed out after {}s".format(timeout)


def _describe_exception(exc, timeout=None):
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8", "replace").strip()
        except Exception:
            body = ""
        if len(body) > 500:
            body = body[:500] + "..."
        reason = " " + str(exc.reason) if exc.reason else ""
        suffix = ": " + body if body else ""
        return "HTTP {}{}{}".format(exc.code, reason, suffix)
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, socket.timeout):
            return _timeout_message(timeout)
        return "connection failed: {}".format(reason)
    if isinstance(exc, socket.timeout):
        return _timeout_message(timeout)
    if isinstance(exc, RuntimeError):
        return str(exc) or "runtime error"
    return "{}: {}".format(type(exc).__name__, exc)


def _emit_non_stream_body(response, output_queue):
    body = response.read().decode("utf-8", "replace")
    try:
        obj = json.loads(body)
    except ValueError:
        raise RuntimeError("unexpected response: " + body[:300])
    api_error = _extract_api_error(obj)
    if api_error:
        raise RuntimeError(api_error)
    content = _extract_content(obj)
    if not content:
        raise RuntimeError("unexpected response: " + body[:300])
    output_queue.put(("delta", content))


def _stream_one_model(config, model, payload, output_queue, cancel_event):
    received_content = False
    response = None
    try:
        request = urllib.request.Request(
            model["api_url"],
            data=json.dumps(payload).encode("utf-8"),
            headers=_build_headers(model),
        )
        response = _open_request(config, model, request)
        content_type = response.headers.get("Content-Type", "") or ""
        if "application/json" in content_type and "text/event-stream" not in content_type:
            _emit_non_stream_body(response, output_queue)
            return True, None

        for data in _iter_sse_data(response, cancel_event):
            if cancel_event.is_set():
                return received_content, None
            if data == "[DONE]":
                return received_content, None
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            api_error = _extract_api_error(obj)
            if api_error:
                raise RuntimeError(api_error)
            content = _extract_content(obj)
            if content:
                output_queue.put(("delta", content))
                received_content = True
        return received_content, None
    except Exception as exc:
        if cancel_event.is_set():
            return received_content, None
        return received_content, _describe_exception(
            exc, config["request_timeout"]
        )
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


def _stream_worker(config, instruction, source_text, output_queue, cancel_event):
    failures = []
    for model in config["models"]:
        if cancel_event.is_set():
            return

        payload = _build_payload(config, model, instruction, source_text)
        received, error = _stream_one_model(
            config, model, payload, output_queue, cancel_event
        )
        if cancel_event.is_set():
            return

        if received:
            if error:
                output_queue.put(("error", _model_label(model) + ": " + error))
            else:
                output_queue.put(("done", None))
            return

        if error is None:
            error = "model returned no content"
        failures.append((_model_label(model), error))
        print("[Inline AI] {}: {}".format(_model_label(model), error))

    if cancel_event.is_set():
        return
    if failures:
        summary = "; ".join("{}: {}".format(label, error) for label, error in failures)
        output_queue.put(("error", "all models failed: " + summary))
    else:
        output_queue.put(("error", "no models configured"))


def _run_write(view, begin, end, text):
    view.run_command("inline_ai_write", {
        "begin": begin,
        "end": end,
        "text": text,
    })
    return _LAST_WRITE_END[0]


class InlineAIWriteCommand(sublime_plugin.TextCommand):
    def run(self, edit, begin=0, end=0, text=""):
        if end > begin:
            new_end = self.view.replace(edit, sublime.Region(begin, end), text)
        else:
            new_end = self.view.insert(edit, begin, text)
        _LAST_WRITE_END[0] = new_end if new_end is not None else begin + len(text)


class _StreamSession:
    def __init__(self, view, region, source_text, instruction, change_count, config):
        self.view = view
        self.view_id = view.id()
        self.region = region
        self.source_text = source_text
        self.instruction = instruction
        self.config = config
        self.cursor = None
        self.started = False
        self.finished = False
        self.expected_change_count = change_count
        self.pump_interval_ms = config["pump_interval_ms"]
        self.cancel_event = threading.Event()
        self.output_queue = queue.Queue()

    def start(self):
        try:
            _SESSIONS[self.view_id] = self
            self.view.settings().set("inline_ai_streaming", True)
            self.view.set_status("inline_ai", "Inline AI: generating...")

            sublime.set_timeout(self._pump, self.pump_interval_ms)

            thread = threading.Thread(
                target=_stream_worker,
                args=(
                    self.config,
                    self.instruction,
                    self.source_text,
                    self.output_queue,
                    self.cancel_event,
                ),
            )
            thread.daemon = True
            thread.start()
        except Exception as exc:
            self._finish(error=_describe_exception(exc))

    def cancel(self):
        self.cancel_event.set()
        self._finish(cancelled=True)

    def _buffer_intact(self):
        return self.view.change_count() == self.expected_change_count

    def _write(self, text):
        if not text:
            return
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not self.started:
            self.cursor = _run_write(
                self.view, self.region.begin(), self.region.end(), text
            )
            self.started = True
        else:
            self.cursor = _run_write(self.view, self.cursor, self.cursor, text)

        self.expected_change_count = self.view.change_count()
        selection = self.view.sel()
        selection.clear()
        selection.add(sublime.Region(self.cursor))
        self.view.show(self.cursor, False)

    def _pump(self):
        if self.finished:
            return
        if not self.view.is_valid():
            self._finish(error="view was closed")
            return

        pending = []
        error = None
        done = False

        while True:
            try:
                kind, value = self.output_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "delta":
                if value:
                    pending.append(value)
            elif kind == "error":
                error = value
            elif kind == "done":
                done = True

        if pending:
            if not self._buffer_intact():
                self._finish(error="buffer changed externally; stream stopped")
                return
            self._write("".join(pending))

        if error is not None:
            self._finish(error=error)
            return
        if done:
            self._finish()
            return

        sublime.set_timeout(self._pump, self.pump_interval_ms)

    def _finish(self, error=None, cancelled=False):
        if self.finished:
            return
        self.finished = True
        self.cancel_event.set()
        _SESSIONS.pop(self.view_id, None)

        if self.view.is_valid():
            self.view.settings().erase("inline_ai_streaming")
            self.view.erase_status("inline_ai")
            if (self.started and self.cursor is not None and
                    self.view.change_count() == self.expected_change_count):
                selection = self.view.sel()
                selection.clear()
                selection.add(sublime.Region(self.region.begin(), self.cursor))
                self.view.show(self.cursor, False)

        if error:
            message = error if len(error) <= 300 else error[:297] + "..."
            sublime.status_message("Inline AI error: " + message)
        elif cancelled:
            sublime.status_message("Inline AI: cancelled")
        elif not self.started:
            sublime.status_message("Inline AI: model returned no content")
        else:
            sublime.status_message("Inline AI: done")


class InlineAICommand(sublime_plugin.TextCommand):
    def run(self, edit):
        if self.view.is_read_only():
            sublime.status_message("Inline AI: view is read-only")
            return

        region = self._pick_region()
        if region is None:
            sublime.status_message("Inline AI: select some text first")
            return

        source_text = self.view.substr(region)
        if not source_text:
            sublime.status_message("Inline AI: selection is empty")
            return

        change_count = self.view.change_count()
        window = self.view.window()
        if window is None:
            sublime.status_message("Inline AI: no active window")
            return

        try:
            config = _load_config(self.view)
        except Exception as exc:
            sublime.status_message("Inline AI error: " + _describe_exception(exc))
            return

        existing = _SESSIONS.get(self.view.id())
        if existing is not None:
            existing.cancel()

        window.show_input_panel(
            "Inline AI instruction:",
            "",
            lambda instruction: self._on_instruction(
                region, source_text, instruction, change_count, config
            ),
            None,
            None,
        )

    def _pick_region(self):
        for region in self.view.sel():
            if not region.empty():
                return sublime.Region(region.begin(), region.end())
        return None

    def _on_instruction(self, region, source_text, instruction, change_count, config):
        instruction = instruction.strip()
        if not instruction:
            sublime.status_message("Inline AI: instruction is empty")
            return
        if not self.view.is_valid():
            return
        if self.view.change_count() != change_count:
            sublime.status_message("Inline AI: buffer changed; try again")
            return

        existing = _SESSIONS.get(self.view.id())
        if existing is not None:
            existing.cancel()

        session = _StreamSession(
            self.view, region, source_text, instruction, change_count, config
        )
        session.start()


class InlineAICancelCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        session = _SESSIONS.get(self.view.id())
        if session is not None:
            session.cancel()
