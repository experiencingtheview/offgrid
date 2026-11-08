"""Offline voice output: discovery, cleanup, safe execution and CLI config."""

import subprocess
import unittest
from unittest import mock

from offgrid.cli import build_parser, overrides_from
from offgrid.config import Config
from offgrid.ui import UI
from offgrid.voice import (
    NO_ENGINE_MESSAGE,
    VoiceEngine,
    clean_for_speech,
    find_engine,
    speak,
)


def find_with(mapping):
    return lambda name: mapping.get(name)


class EngineDiscoveryTests(unittest.TestCase):
    def test_macos_uses_say_and_passes_a_voice_without_a_shell(self):
        engine = find_engine("Darwin", find_with({"say": "/usr/bin/say"}))

        self.assertIsNotNone(engine)
        self.assertEqual(engine.command(), ["/usr/bin/say"])
        self.assertEqual(engine.command("Samantha"), ["/usr/bin/say", "-v", "Samantha"])

    def test_linux_prefers_espeak_ng(self):
        engine = find_engine(
            "Linux",
            find_with({"espeak": "/usr/bin/espeak", "espeak-ng": "/usr/bin/espeak-ng"}),
        )

        self.assertEqual(engine.name, "espeak-ng")
        self.assertEqual(engine.command("en-us"), ["/usr/bin/espeak-ng", "-v", "en-us", "--stdin"])

    def test_linux_falls_back_to_espeak_then_festival(self):
        espeak = find_engine("Linux", find_with({"espeak": "/usr/bin/espeak"}))
        festival = find_engine("Linux", find_with({"festival": "/usr/bin/festival"}))

        self.assertEqual(espeak.command(), ["/usr/bin/espeak", "--stdin"])
        self.assertEqual(festival.command("ignored"), ["/usr/bin/festival", "--tts"])

    def test_windows_uses_sapi_and_an_environment_voice(self):
        engine = find_engine("Windows", find_with({"powershell": "C:/Windows/powershell.exe"}))

        self.assertEqual(engine.name, "Windows SAPI")
        self.assertEqual(engine.voice_env, "OFFGRID_SPEAK_VOICE")
        command = engine.command("Microsoft David Desktop")
        self.assertIn("System.Speech", " ".join(command))
        self.assertNotIn("Microsoft David Desktop", command)

    def test_no_engine_is_reported_clearly(self):
        self.assertIsNone(find_engine("Linux", find_with({})))
        self.assertIn("espeak-ng", NO_ENGINE_MESSAGE)


class SpeechCleanupTests(unittest.TestCase):
    def test_markdown_becomes_prose_and_code_is_skipped(self):
        raw = (
            "\x1b[31m## Result\x1b[0m\n\n"
            "- **Done** with [the guide](https://example.test/guide).\n\n"
            "```python\nprint('do not read this')\n```\n\n"
            "Run `pytest` next."
        )

        spoken = clean_for_speech(raw)

        self.assertIn("Result", spoken)
        self.assertIn("Done with the guide", spoken)
        self.assertIn("Code block omitted.", spoken)
        self.assertIn("Run pytest next.", spoken)
        self.assertNotIn("do not read this", spoken)
        self.assertNotIn("`", spoken)
        self.assertNotIn("\x1b", spoken)

    def test_long_reports_end_with_a_spoken_note(self):
        spoken = clean_for_speech("word " * 2000, max_chars=200)

        self.assertLess(len(spoken), 260)
        self.assertTrue(spoken.endswith("The rest was not read aloud."))


class SpeakTests(unittest.TestCase):
    def test_speak_pipes_clean_text_over_stdin(self):
        engine = VoiceEngine("test voice", "/bin/voice", (), ("-v",))
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, "", "")

        ok, message = speak(
            "## Done\n\n```\ncode\n```\n\nAll tests pass.",
            "Test Voice",
            engine=engine,
            runner=runner,
        )

        self.assertTrue(ok, message)
        command, kwargs = calls[0]
        self.assertEqual(command, ["/bin/voice", "-v", "Test Voice"])
        self.assertEqual(kwargs["input"], "Done\n\nCode block omitted.\n\nAll tests pass.")
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["stderr"], subprocess.PIPE)

    def test_speak_uses_the_environment_for_a_windows_voice(self):
        engine = VoiceEngine(
            "Windows SAPI",
            "powershell",
            ("-NoProfile", "-Command", "script"),
            voice_env="OFFGRID_SPEAK_VOICE",
        )
        seen = {}

        def runner(command, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(command, 0, "", "")

        ok, _ = speak("Hello", "Microsoft Zira", engine=engine, runner=runner)

        self.assertTrue(ok)
        self.assertEqual(seen["env"]["OFFGRID_SPEAK_VOICE"], "Microsoft Zira")

    def test_speak_returns_the_engine_error(self):
        engine = VoiceEngine("test voice", "/bin/voice")

        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", "voice not installed")

        ok, message = speak("Hello", engine=engine, runner=runner)

        self.assertFalse(ok)
        self.assertIn("voice not installed", message)

    def test_missing_engine_does_not_raise(self):
        ok, message = speak("Hello", system="Linux", which=find_with({}))

        self.assertFalse(ok)
        self.assertIn("no offline speech engine", message)


class VoiceConfigTests(unittest.TestCase):
    def test_voice_flags_reach_the_config(self):
        args = build_parser().parse_args(["chat", "--speak", "--voice", "Samantha"])
        overrides = overrides_from(args)

        self.assertTrue(overrides["speak"])
        self.assertEqual(overrides["voice"], "Samantha")

    def test_ui_speaks_only_when_enabled(self):
        with mock.patch("offgrid.ui.speak_text", return_value=(True, "")) as speak_text:
            UI(Config(speak=True, color=False, verbose=0)).speak("Finished.")
            UI(Config(speak=False, color=False, verbose=0)).speak("Finished.")

        speak_text.assert_called_once_with("Finished.", "")

    def test_ui_reports_an_unavailable_engine_only_once(self):
        with mock.patch("offgrid.ui.speak_text", return_value=(False, NO_ENGINE_MESSAGE)):
            ui = UI(Config(speak=True, color=False, verbose=2))
            with mock.patch("builtins.print") as output:
                ui.speak("One.")
                ui.speak("Two.")

        lines = [" ".join(str(part) for part in call.args) for call in output.call_args_list]
        self.assertEqual(sum(NO_ENGINE_MESSAGE in line for line in lines), 1)


if __name__ == "__main__":
    unittest.main()
