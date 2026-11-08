import contextlib
import importlib.util
import io
import json
import os
import queue
import sys
import threading
import types
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeRegion:
  def __init__(self, a, b=None):
    self.a = a
    self.b = a if b is None else b

  def begin(self):
    return min(self.a, self.b)

  def end(self):
    return max(self.a, self.b)

  def empty(self):
    return self.a == self.b


class FakePhantom:
  def __init__(self, region, content, layout):
    self.region = region
    self.content = content
    self.layout = layout


class FakePhantomSet:
  def __init__(self, view, key):
    self.view = view
    self.key = key
    self.phantoms = []

  def update(self, phantoms):
    self.phantoms = list(phantoms)


sublime = types.ModuleType("sublime")
sublime.Region = FakeRegion
sublime.Phantom = FakePhantom
sublime.PhantomSet = FakePhantomSet
sublime.LAYOUT_BELOW = 1
sublime.LAYOUT_BLOCK = 2
sublime.DRAW_SQUIGGLY_UNDERLINE = 2
sublime.status_message = lambda message: None
sublime.set_timeout = lambda callback, delay=0: None
sys.modules["sublime"] = sublime

sublime_plugin = types.ModuleType("sublime_plugin")
sublime_plugin.TextCommand = type("TextCommand", (object,), {})
sublime_plugin.EventListener = type("EventListener", (object,), {})
sys.modules["sublime_plugin"] = sublime_plugin

spec = importlib.util.spec_from_file_location(
  "inline_ai", os.path.join(PROJECT_ROOT, "inline_ai.py")
)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class FakeSettings:
  def __init__(self, data):
    self.data = data

  def get(self, key, default=None):
    return self.data.get(key, default)

  def erase(self, key):
    self.data.pop(key, None)


class FakeWindow:
  def __init__(self, project_data=None):
    self._project_data = project_data

  def project_data(self):
    return self._project_data


class FakeView:
  def __init__(self, project_data=None, file_name=None):
    self._window = FakeWindow(project_data)
    self._file_name = file_name

  def window(self):
    return self._window

  def file_name(self):
    return self._file_name


class FakeBufferView:
  def __init__(self, text=""):
    self.text = text

  def size(self):
    return len(self.text)

  def insert(self, edit, point, text):
    self.text = self.text[:point] + text + self.text[point:]
    return len(text)

  def replace(self, edit, region, text):
    begin = region.begin()
    end = region.end()
    self.text = self.text[:begin] + text + self.text[end:]


class FakeSelection:
  def __init__(self):
    self.regions = []

  def clear(self):
    self.regions = []

  def add(self, region):
    self.regions.append(region)


class FakeIndicatorView:
  def __init__(self):
    self.regions = {}
    self.selection = FakeSelection()
    self.change_count_value = 0
    self.valid = True

  def id(self):
    return 1

  def change_count(self):
    return self.change_count_value

  def is_valid(self):
    return self.valid

  def sel(self):
    return self.selection

  def show(self, point, surrounds):
    pass

  def settings(self):
    return FakeSettings({})

  def erase_status(self, key):
    pass

  def add_regions(self, key, regions, scope, icon, flags):
    self.regions[key] = {
      "regions": regions,
      "scope": scope,
      "icon": icon,
      "flags": flags,
    }

  def erase_regions(self, key):
    self.regions.pop(key, None)


class FakeEvent:
  def is_set(self):
    return False


class FakeHTTPResponse:
  def __init__(self, chunks):
    self.chunks = list(chunks)
    self.headers = {"Content-Type": "text/event-stream"}

  def read1(self, size):
    return self.chunks.pop(0) if self.chunks else b""

  def close(self):
    pass


def sse_chunk(content):
  payload = {"choices": [{"delta": {"content": content}}]}
  return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


def default_settings():
  return {
    "api_url": "http://localhost:11434/v1/chat/completions",
    "api_key": "secret",
    "model": "llama3",
    "temperature": 0.1,
    "request_timeout": 60,
    "pump_interval_ms": 20,
    "stream_mode": "buffer",
    "system_prompt": "prompt",
    "instruction_defaults": {
      "ts,js,mts,mjs,jsx,tsx": "Refactor code.",
      "md,markdown": "Improve docs.",
      "default": "Improve text.",
    },
  }


def drain(q):
  messages = []
  while not q.empty():
    messages.append(q.get())
  return messages


class PluginTestCase(unittest.TestCase):
  def setUp(self):
    self.settings = default_settings()
    sublime.load_settings = lambda name: FakeSettings(self.settings)
    plugin._LAST_INSTRUCTIONS.clear()
    del plugin._LAST_INSTRUCTION_ORDER[:]
    plugin._LAST_WRITE_END[0] = 0
    self.original_open_request = plugin._open_request

  def tearDown(self):
    plugin._open_request = self.original_open_request
    plugin._LAST_INSTRUCTIONS.clear()
    del plugin._LAST_INSTRUCTION_ORDER[:]

  def config(self, project_data=None, file_name=None):
    return plugin._load_config(
      FakeView(project_data=project_data, file_name=file_name)
    )


class TestSseParser(unittest.TestCase):
  def test_fragmented_sse(self):
    payload = (
      sse_chunk("Hel") +
      sse_chunk("lo, ") +
      sse_chunk("Привет") +
      b"data: [DONE]\n\n"
    )
    chunks = [payload[i:i + 7] for i in range(0, len(payload), 7)]
    lines = list(plugin._iter_sse_data(FakeHTTPResponse(chunks), FakeEvent()))
    self.assertEqual(lines[-1], "[DONE]")
    text = "".join(
      json.loads(line)["choices"][0]["delta"]["content"]
      for line in lines[:-1]
    )
    self.assertEqual(text, "Hello, Привет")

  def test_extract_content(self):
    self.assertEqual(
      plugin._extract_content({"choices": [{"delta": {"content": "abc"}}]}),
      "abc",
    )
    self.assertEqual(
      plugin._extract_content(
        {"choices": [{"message": {"content": "non-stream"}}]}
      ),
      "non-stream",
    )
    self.assertEqual(
      plugin._extract_content({
        "choices": [{
          "delta": {
            "content": [{"type": "text", "text": "part"}]
          }
        }]
      }),
      "part",
    )

  def test_extract_api_error(self):
    self.assertEqual(
      plugin._extract_api_error({"error": {"message": "boom"}}),
      "boom",
    )


class TestConfig(PluginTestCase):
  def test_valid_config(self):
    config = self.config()
    self.assertEqual(config["api_url"], self.settings["api_url"])
    self.assertEqual(config["api_key"], "secret")
    self.assertEqual(config["model"], "llama3")
    self.assertEqual(config["temperature"], 0.1)
    self.assertEqual(config["request_timeout"], 60)
    self.assertEqual(config["pump_interval_ms"], 20)
    self.assertEqual(config["stream_mode"], "buffer")
    self.assertEqual(config["system_prompt"], "prompt")
    self.assertEqual(plugin._default_instruction(config, "ts"), "Refactor code.")
    self.assertEqual(plugin._default_instruction(config, "py"), "Improve text.")

  def test_project_overrides(self):
    config = self.config(project_data={
      "settings": {
        "inline_ai": {
          "api_url": "http://localhost:9999/v1/chat/completions",
          "model": "project-model",
          "temperature": 0.3,
        }
      }
    })
    self.assertEqual(
      config["api_url"], "http://localhost:9999/v1/chat/completions"
    )
    self.assertEqual(config["model"], "project-model")
    self.assertEqual(config["api_key"], "secret")
    self.assertEqual(config["temperature"], 0.3)
    self.assertEqual(config["request_timeout"], 60)
    self.assertEqual(plugin._default_instruction(config, "ts"), "Refactor code.")

  def test_stream_mode_default(self):
    del self.settings["stream_mode"]
    config = self.config()
    self.assertEqual(config["stream_mode"], "preview")

  def test_project_stream_mode(self):
    config = self.config(project_data={
      "settings": {"inline_ai": {"stream_mode": "final"}}
    })
    self.assertEqual(config["stream_mode"], "final")

  def test_invalid_stream_mode(self):
    self.settings["stream_mode"] = "weird"
    with self.assertRaises(RuntimeError) as context:
      self.config()
    self.assertIn("stream_mode", str(context.exception))

  def test_project_instruction_defaults(self):
    config = self.config(project_data={
      "settings": {
        "inline_ai": {
          "instruction_defaults": {
            "py": "Refactor Python.",
            "default": "Improve.",
          }
        }
      }
    })
    self.assertEqual(plugin._default_instruction(config, "py"), "Refactor Python.")
    self.assertEqual(plugin._default_instruction(config, "ts"), "Improve.")

  def test_project_array_rejected(self):
    with self.assertRaises(RuntimeError) as context:
      self.config(project_data={"settings": {"inline_ai": []}})
    self.assertIn("must be an object", str(context.exception))

  def test_missing_settings(self):
    self.settings = {}
    with self.assertRaises(RuntimeError) as context:
      self.config()
    self.assertIn("missing settings", str(context.exception))

  def test_invalid_api_url(self):
    self.settings["api_url"] = "ftp://localhost/v1"
    with self.assertRaises(RuntimeError) as context:
      self.config()
    self.assertIn("api_url is invalid", str(context.exception))

  def test_empty_model(self):
    self.settings["model"] = ""
    with self.assertRaises(RuntimeError) as context:
      self.config()
    self.assertIn("model must be non-empty", str(context.exception))

  def test_instruction_defaults_require_default(self):
    self.settings["instruction_defaults"] = {"ts": "Refactor."}
    with self.assertRaises(RuntimeError) as context:
      self.config()
    self.assertIn("default", str(context.exception))

  def test_instruction_defaults_require_strings(self):
    self.settings["instruction_defaults"] = {"default": 123}
    with self.assertRaises(RuntimeError) as context:
      self.config()
    self.assertIn("strings", str(context.exception))

  def test_instruction_defaults_reject_duplicates(self):
    self.settings["instruction_defaults"] = {
      "ts,js": "A",
      "ts": "B",
      "default": "C",
    }
    with self.assertRaises(RuntimeError) as context:
      self.config()
    self.assertIn("duplicate", str(context.exception))


class TestApiKey(PluginTestCase):
  def test_literal_key(self):
    config = self.config()
    self.assertEqual(
      plugin._build_headers(config)["Authorization"],
      "Bearer secret",
    )

  def test_environment_key(self):
    os.environ["INLINE_AI_TEST_KEY"] = "env-secret"
    self.addCleanup(lambda: os.environ.pop("INLINE_AI_TEST_KEY", None))
    config = dict(self.config(), api_key="{env:INLINE_AI_TEST_KEY}")
    self.assertEqual(
      plugin._build_headers(config)["Authorization"],
      "Bearer env-secret",
    )

  def test_missing_environment_key(self):
    config = dict(self.config(), api_key="{env:INLINE_AI_MISSING_KEY}")
    with self.assertRaises(RuntimeError) as context:
      plugin._build_headers(config)
    self.assertIn("INLINE_AI_MISSING_KEY", str(context.exception))

  def test_malformed_environment_key(self):
    config = dict(self.config(), api_key="{env:BROKEN")
    with self.assertRaises(RuntimeError) as context:
      plugin._build_headers(config)
    self.assertIn("invalid api_key environment reference", str(context.exception))


class TestInstructionDefaults(PluginTestCase):
  def test_file_extension(self):
    self.assertEqual(
      plugin._file_extension(FakeView(file_name="/tmp/app.ts")), "ts"
    )
    self.assertEqual(
      plugin._file_extension(FakeView(file_name="/tmp/README.md")), "md"
    )
    self.assertEqual(
      plugin._file_extension(FakeView(file_name="/tmp/Makefile")), "default"
    )
    self.assertEqual(plugin._file_extension(FakeView()), "default")

  def test_extension_groups(self):
    config = self.config()
    self.assertEqual(plugin._default_instruction(config, "ts"), "Refactor code.")
    self.assertEqual(plugin._default_instruction(config, "jsx"), "Refactor code.")
    self.assertEqual(plugin._default_instruction(config, "mjs"), "Refactor code.")
    self.assertEqual(plugin._default_instruction(config, "md"), "Improve docs.")
    self.assertEqual(plugin._default_instruction(config, "markdown"), "Improve docs.")
    self.assertEqual(plugin._default_instruction(config, "py"), "Improve text.")
    self.assertEqual(plugin._default_instruction(config, "default"), "Improve text.")


class TestInstructionHistory(PluginTestCase):
  def key(self, path):
    return plugin._file_key(FakeView(file_name=path))

  def test_exact_file_match(self):
    app = self.key("/repo/src/app.ts")
    plugin._save_last_instruction(app, "exact app")
    self.assertEqual(
      plugin._initial_instruction(app, "ts", self.config()),
      "exact app",
    )

  def test_closest_prefix_match(self):
    app = self.key("/repo/src/app.ts")
    other = self.key("/repo/src/other.ts")
    docs = self.key("/repo/docs/readme.md")
    plugin._save_last_instruction(app, "exact app")
    plugin._save_last_instruction(other, "other")
    plugin._save_last_instruction(docs, "docs")

    self.assertEqual(
      plugin._initial_instruction(
        self.key("/repo/src/app2.ts"), "ts", self.config()
      ),
      "exact app",
    )
    self.assertEqual(
      plugin._initial_instruction(
        self.key("/repo/docs/guide.md"), "md", self.config()
      ),
      "docs",
    )

  def test_tie_uses_most_recent(self):
    app = self.key("/repo/src/app.ts")
    other = self.key("/repo/src/other.ts")
    plugin._save_last_instruction(app, "exact app")
    plugin._save_last_instruction(other, "other")

    current = self.key("/repo/src/new.ts")
    self.assertEqual(
      plugin._initial_instruction(current, "ts", self.config()),
      "other",
    )

    plugin._save_last_instruction(app, "exact app")
    self.assertEqual(
      plugin._initial_instruction(current, "ts", self.config()),
      "exact app",
    )

  def test_no_history_uses_default(self):
    current = self.key("/repo/src/app.ts")
    self.assertEqual(
      plugin._initial_instruction(current, "ts", self.config()),
      "Refactor code.",
    )

  def test_untitled_does_not_store(self):
    plugin._save_last_instruction(None, "ignored")
    self.assertEqual(plugin._LAST_INSTRUCTIONS, {})
    self.assertEqual(
      plugin._initial_instruction(None, "default", self.config()),
      "Improve text.",
    )

  def test_common_prefix_length(self):
    self.assertEqual(plugin._common_prefix_length("abc", "abd"), 2)
    self.assertEqual(plugin._common_prefix_length("abc", "abc"), 3)
    self.assertEqual(plugin._common_prefix_length("", "abc"), 0)

  def test_file_key_normalizes(self):
    view = FakeView(file_name="/repo/src/../src/app.ts")
    expected = os.path.normcase(os.path.normpath("/repo/src/../src/app.ts"))
    self.assertEqual(plugin._file_key(view), expected)
    self.assertIsNone(plugin._file_key(FakeView()))


class TestIndicator(PluginTestCase):
  def make_session(self, region=None, stream_mode="buffer"):
    if region is None:
      region = FakeRegion(0, 5)
    view = FakeIndicatorView()
    config = dict(self.config(), stream_mode=stream_mode)
    session = plugin._StreamSession(
      view, region, "source", "instruction", 0, config
    )
    session.phantom_set = FakePhantomSet(view, "inline_ai")
    return view, session

  def test_pending_indicator(self):
    view, session = self.make_session()
    session._update_indicator()
    self.assertIn("inline_ai_target", view.regions)
    self.assertEqual(len(session.phantom_set.phantoms), 1)
    content = session.phantom_set.phantoms[0].content
    self.assertIn("will be rewritten", content)
    self.assertIn("Press Escape", content)
    phantom_region = session.phantom_set.phantoms[0].region
    self.assertEqual(phantom_region.begin(), 0)
    self.assertEqual(phantom_region.end(), 0)

  def test_streaming_indicator(self):
    view, session = self.make_session(FakeRegion(5, 10))
    session.started = True
    session.cursor = 12
    session._update_indicator()
    content = session.phantom_set.phantoms[0].content
    self.assertIn("rewriting selected text", content)
    self.assertIn("7 chars", content)
    self.assertIn("Press Escape", content)
    phantom_region = session.phantom_set.phantoms[0].region
    self.assertEqual(phantom_region.begin(), 12)
    self.assertEqual(phantom_region.end(), 12)
    highlight = view.regions["inline_ai_target"]["regions"][0]
    self.assertEqual(highlight.begin(), 5)
    self.assertEqual(highlight.end(), 12)
    self.assertEqual(
      view.regions["inline_ai_target"]["scope"], "region.yellowish"
    )

  def test_clear_indicator(self):
    view, session = self.make_session()
    session._update_indicator()
    session._clear_indicator()
    self.assertNotIn("inline_ai_target", view.regions)
    self.assertEqual(session.phantom_set.phantoms, [])

  def test_preview_indicator(self):
    view, session = self.make_session(FakeRegion(0, 5), "preview")
    session._write("hello")
    content = session.phantom_set.phantoms[0].content
    self.assertIn("rewriting selected text", content)
    self.assertIn("5 chars", content)
    self.assertIn("hello", content)
    self.assertEqual(session.generated_text, "hello")
    self.assertIsNone(session.cursor)
    phantom = session.phantom_set.phantoms[0]
    self.assertEqual(phantom.layout, sublime.LAYOUT_BLOCK)
    self.assertEqual(phantom.region.begin(), 0)
    self.assertEqual(phantom.region.end(), 0)
    highlight = view.regions["inline_ai_target"]["regions"][0]
    self.assertEqual(highlight.begin(), 0)
    self.assertEqual(highlight.end(), 5)

  def test_final_indicator(self):
    view, session = self.make_session(FakeRegion(0, 5), "final")
    session._write("hello")
    content = session.phantom_set.phantoms[0].content
    self.assertIn("rewriting selected text", content)
    self.assertIn("5 chars", content)
    self.assertNotIn("hello", content)
    self.assertEqual(session.generated_text, "hello")
    self.assertIsNone(session.cursor)

  def test_atomic_commit(self):
    original = plugin._run_write
    calls = []

    def fake_run_write(view, begin, end, text):
      calls.append((begin, end, text))
      plugin._LAST_WRITE_END[0] = begin + len(text)
      return plugin._LAST_WRITE_END[0]

    plugin._run_write = fake_run_write
    self.addCleanup(lambda: setattr(plugin, "_run_write", original))

    view, session = self.make_session(FakeRegion(0, 5), "preview")
    session._write("hello")
    self.assertTrue(session._commit())
    self.assertEqual(calls, [(0, 5, "hello")])
    self.assertTrue(session.committed)
    self.assertEqual(session.cursor, 5)
    self.assertEqual(view.selection.regions, [])
    session._finish()
    self.assertEqual(view.selection.regions[0].begin(), 5)
    self.assertEqual(view.selection.regions[0].end(), 5)
    self.assertNotIn("inline_ai_target", view.regions)
    self.assertEqual(session.phantom_set.phantoms, [])

  def test_atomic_commit_without_content(self):
    view, session = self.make_session(FakeRegion(0, 5), "preview")
    self.assertFalse(session._commit())
    self.assertFalse(session.committed)

  def test_preview_html_escapes(self):
    content = plugin._preview_html("hint", "<b>&")
    self.assertIn("&lt;b&gt;&amp;", content)

  def test_indicator_html(self):
    html = plugin._indicator_html("hello")
    self.assertIn("hello", html)
    self.assertIn("inline-ai-indicator", html)


class TestWriteCommand(PluginTestCase):
  def test_streaming_appends_in_order(self):
    view = FakeBufferView("old text")
    command = plugin.InlineAIWriteCommand()
    command.view = view
    edit = object()

    command.run(edit, 0, len("old text"), "hello ")
    self.assertEqual(plugin._LAST_WRITE_END[0], 6)
    self.assertEqual(view.text, "hello ")

    command.run(
      edit,
      plugin._LAST_WRITE_END[0],
      plugin._LAST_WRITE_END[0],
      "world",
    )
    self.assertEqual(plugin._LAST_WRITE_END[0], 11)
    self.assertEqual(view.text, "hello world")

    command.run(
      edit,
      plugin._LAST_WRITE_END[0],
      plugin._LAST_WRITE_END[0],
      "!",
    )
    self.assertEqual(plugin._LAST_WRITE_END[0], 12)
    self.assertEqual(view.text, "hello world!")

  def test_replace_updates_end(self):
    view = FakeBufferView("abcdef")
    command = plugin.InlineAIWriteCommand()
    command.view = view
    edit = object()

    command.run(edit, 1, 5, "XY")
    self.assertEqual(view.text, "aXYf")
    self.assertEqual(plugin._LAST_WRITE_END[0], 3)


class TestStreamWorker(PluginTestCase):
  def test_success(self):
    config = self.config()
    plugin._open_request = lambda config, request: FakeHTTPResponse(
      [sse_chunk("ok") + b"data: [DONE]\n\n"]
    )
    q = queue.Queue()
    plugin._stream_worker(config, "fix", "text", q, threading.Event())
    messages = drain(q)
    self.assertIn(("delta", "ok"), messages)
    self.assertEqual(messages[-1], ("done", None))

  def test_error(self):
    config = self.config()

    def failing_open(config, request):
      raise RuntimeError("boom")

    plugin._open_request = failing_open
    q = queue.Queue()
    with contextlib.redirect_stdout(io.StringIO()):
      plugin._stream_worker(config, "fix", "text", q, threading.Event())
    messages = drain(q)
    self.assertEqual(messages[-1][0], "error")
    self.assertIn("boom", messages[-1][1])


class TestEventListener(unittest.TestCase):
  def test_undo_clears_target_region(self):
    view = FakeIndicatorView()
    view.regions[plugin.TARGET_REGION_KEY] = {}
    listener = plugin.InlineAIEventListener()
    listener.on_text_command(view, "undo", None)
    self.assertNotIn(plugin.TARGET_REGION_KEY, view.regions)

  def test_other_commands_keep_target_region(self):
    view = FakeIndicatorView()
    view.regions[plugin.TARGET_REGION_KEY] = {}
    listener = plugin.InlineAIEventListener()
    listener.on_text_command(view, "insert", None)
    self.assertIn(plugin.TARGET_REGION_KEY, view.regions)


class TestPackagedFiles(unittest.TestCase):
  def test_settings_json(self):
    path = os.path.join(PROJECT_ROOT, "InlineAI.sublime-settings")
    with open(path, "r", encoding="utf-8") as handle:
      data = json.load(handle)
    self.assertIn("instruction_defaults", data)
    self.assertIn("default", data["instruction_defaults"])

  def test_keymap_json(self):
    path = os.path.join(PROJECT_ROOT, "Default.sublime-keymap")
    with open(path, "r", encoding="utf-8") as handle:
      data = json.load(handle)
    commands = [entry.get("command") for entry in data]
    self.assertIn("inline_ai", commands)
    self.assertIn("inline_ai_cancel", commands)


if __name__ == "__main__":
  unittest.main()
