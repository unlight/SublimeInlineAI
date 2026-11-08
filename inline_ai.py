# Inline AI for Sublime Text 4.
# Install: copy this file to Packages/User/inline_ai.py
# Keymap: copy Default.sublime-keymap to Packages/User/

import json
import queue
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request

import sublime
import sublime_plugin


# --- Configuration ---------------------------------------------------------
API_URL = "http://localhost:11434/v1/chat/completions"  # Ollama OpenAI-compatible endpoint
API_KEY = "bearer-token-if-needed"                      # Leave as-is when no auth is required
MODEL_NAME = "llama3"
TEMPERATURE = 0.2
REQUEST_TIMEOUT = 120                                   # Seconds per socket operation
PUMP_INTERVAL_MS = 33                                   # UI update interval (~30 fps)

SYSTEM_PROMPT = (
    "You are an automated inline text and code transformation tool. "
    "Modify the provided text or code based strictly on the user's instructions. "
    "Return ONLY the final modified result. Do NOT wrap the output in markdown code blocks "
    "(like ```python or ```text) unless explicitly asked by the user. "
    "Do NOT include any greetings, explanations, previews, or conversational filler. "
    "Your output must be ready to directly overwrite the original selection."
)
# ---------------------------------------------------------------------------


_SESSIONS = {}
_LAST_WRITE_END = [0]


def _build_headers():
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Cache-Control": "no-cache",
        "User-Agent": "SublimeInlineAI/1.0",
    }
    if API_KEY and API_KEY != "bearer-token-if-needed":
        token = API_KEY.strip()
        if not token.lower().startswith("bearer "):
            token = "Bearer " + token
        headers["Authorization"] = token
    return headers


def _open_request(request):
    host = (urllib.parse.urlparse(API_URL).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(request, timeout=REQUEST_TIMEOUT)
    return urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT)


def _build_payload(instruction, source_text):
    user_content = (
        "INSTRUCTION:\n" + instruction +
        "\n\nTEXT OR CODE TO TRANSFORM:\n" + source_text
    )
    return {
        "model": MODEL_NAME,
        "stream": True,
        "temperature": TEMPERATURE,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
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


def _describe_exception(exc):
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
            return "request timed out after {}s".format(REQUEST_TIMEOUT)
        return "connection failed: {}".format(reason)
    if isinstance(exc, socket.timeout):
        return "request timed out after {}s".format(REQUEST_TIMEOUT)
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


def _stream_worker(payload, headers, output_queue, cancel_event):
    error_message = None
    response = None
    try:
        request = urllib.request.Request(
            API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
        response = _open_request(request)
        content_type = response.headers.get("Content-Type", "") or ""
        if "application/json" in content_type and "text/event-stream" not in content_type:
            _emit_non_stream_body(response, output_queue)
            return

        for data in _iter_sse_data(response, cancel_event):
            if cancel_event.is_set():
                return
            if data == "[DONE]":
                return
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
    except Exception as exc:
        if not cancel_event.is_set():
            error_message = _describe_exception(exc)
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        if error_message:
            output_queue.put(("error", error_message))
        else:
            output_queue.put(("done", None))


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
    def __init__(self, view, region, source_text, instruction, change_count):
        self.view = view
        self.view_id = view.id()
        self.region = region
        self.source_text = source_text
        self.instruction = instruction
        self.cursor = None
        self.started = False
        self.finished = False
        self.expected_change_count = change_count
        self.cancel_event = threading.Event()
        self.output_queue = queue.Queue()

    def start(self):
        _SESSIONS[self.view_id] = self
        self.view.settings().set("inline_ai_streaming", True)
        self.view.set_status("inline_ai", "Inline AI: generating...")

        sublime.set_timeout(self._pump, PUMP_INTERVAL_MS)

        payload = _build_payload(self.instruction, self.source_text)
        headers = _build_headers()
        thread = threading.Thread(
            target=_stream_worker,
            args=(payload, headers, self.output_queue, self.cancel_event),
        )
        thread.daemon = True
        thread.start()

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

        sublime.set_timeout(self._pump, PUMP_INTERVAL_MS)

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

        existing = _SESSIONS.get(self.view.id())
        if existing is not None:
            existing.cancel()

        window.show_input_panel(
            "Inline AI instruction:",
            "",
            lambda instruction: self._on_instruction(
                region, source_text, instruction, change_count
            ),
            None,
            None,
        )

    def _pick_region(self):
        for region in self.view.sel():
            if not region.empty():
                return sublime.Region(region.begin(), region.end())
        return None

    def _on_instruction(self, region, source_text, instruction, change_count):
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
            self.view, region, source_text, instruction, change_count
        )
        try:
            session.start()
        except Exception as exc:
            session._finish(error=_describe_exception(exc))


class InlineAICancelCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        session = _SESSIONS.get(self.view.id())
        if session is not None:
            session.cancel()
