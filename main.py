"""
The Eminence AI v2 - prawdziwy mózg.

Backendy:
  - anthropic : Claude przez API (wymaga klucza i internetu)
  - ollama    : lokalny model offline (wymaga zainstalowanej Ollamy na localhost)

Komendy w czacie:
  /klucz <klucz_api>        zapisz klucz Anthropic
  /backend anthropic|ollama przełącz backend
  /model <nazwa>            zmień model
  /reset                    wyczyść historię rozmowy
  /pomoc                    lista komend

Klucz można też podać w zmiennej środowiskowej ANTHROPIC_API_KEY.
Na Androidzie w buildozer.spec dodaj: android.permissions = INTERNET
"""
import json
import math
import os
import queue
import ssl
import threading
import urllib.error
import urllib.request

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color, Ellipse, Line, RoundedRectangle
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput

try:
    import pyttsx3
except ImportError:
    pyttsx3 = None

try:
    from plyer import tts as plyer_tts
except Exception:
    plyer_tts = None

try:
    import speech_recognition as sr
except ImportError:
    sr = None

Window.clearcolor = (0.01, 0.01, 0.03, 1)
Window.softinput_mode = "below_target"  # klawiatura nie zasłania pola wpisywania

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = None

USER_TITLE = "Overlordzie Włodarzu Guru Sigma Cid Kagenou Alfo-Omego Musashi Zimny Skurwiel Bóg Ultra"

DEFAULT_CONFIG = {
    "backend": "anthropic",
    "anthropic_model": "claude-sonnet-5-5",
    "ollama_model": "llama3.1",
    "ollama_url": "http://localhost:11434",
    "api_key": "",
    "max_tokens": 800,
    "history_limit": 20,
}

SYSTEM_PROMPT = (
    "Jesteś The Eminence AI, asystentem w aplikacji mobilnej. "
    "Odpowiadasz po polsku, zwięźle i konkretnie (to ekran telefonu i synteza mowy), "
    "w stylu pewnym i lekko mrocznym, ale zawsze rzeczowo i uczciwie. "
    "Użytkownik ma tytuł: \"{title}\". Użyj go najwyżej raz, na początku rozmowy, "
    "potem zwracaj się neutralnie. Nie zmyślaj faktów ani możliwości, "
    "a gdy czegoś nie wiesz, powiedz to wprost."
)


# --------------------------------------------------------------------------
# Konfiguracja
# --------------------------------------------------------------------------
class Config:
    def __init__(self, path):
        self.path = path
        self.data = dict(DEFAULT_CONFIG)
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass
        env_key = os.environ.get("ANTHROPIC_API_KEY")
        if env_key and not self.data["api_key"]:
            self.data["api_key"] = env_key

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v
        self.save()


# --------------------------------------------------------------------------
# Mózg
# --------------------------------------------------------------------------
def _post_json(url, headers, payload, timeout=90):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")[:300]
        raise RuntimeError(f"HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Brak połączenia: {e.reason}")


class AIBrain:
    COMMANDS_HELP = (
        "/klucz <klucz> - zapisz klucz Anthropic\n"
        "/backend anthropic|ollama - przełącz silnik\n"
        "/model <nazwa> - zmień model\n"
        "/reset - wyczyść historię\n"
        "/pomoc - ta lista"
    )

    def __init__(self, config: Config):
        self.cfg = config
        self.history = []  # [{"role": "user"/"assistant", "content": str}]

    # --- komendy (szybkie, bez sieci) ---
    def try_command(self, text):
        """Zwraca odpowiedź, jeśli to komenda; w przeciwnym razie None."""
        if not text.startswith("/"):
            return None
        parts = text.split(maxsplit=1)
        cmd, arg = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
        if cmd == "/pomoc":
            return self.COMMANDS_HELP
        if cmd == "/reset":
            self.history.clear()
            return "Historia rozmowy wyczyszczona."
        if cmd == "/klucz":
            if not arg:
                return "Użycie: /klucz <klucz_api>"
            self.cfg["api_key"] = arg
            return "Klucz zapisany (lokalnie, w pliku konfiguracji)."
        if cmd == "/backend":
            if arg not in ("anthropic", "ollama"):
                return "Użycie: /backend anthropic lub /backend ollama"
            self.cfg["backend"] = arg
            return f"Backend: {arg}."
        if cmd == "/model":
            if not arg:
                return "Użycie: /model <nazwa>"
            key = "anthropic_model" if self.cfg["backend"] == "anthropic" else "ollama_model"
            self.cfg[key] = arg
            return f"Model ({self.cfg['backend']}): {arg}."
        return "Nieznana komenda. Wpisz /pomoc."

    # --- zapytanie do modelu (blokujące - wołać w wątku) ---
    def ask(self, user_text):
        self.history.append({"role": "user", "content": user_text})
        limit = self.cfg["history_limit"]
        self.history = self.history[-limit:]
        # Historia musi zaczynać się od wiadomości użytkownika
        while self.history and self.history[0]["role"] != "user":
            self.history.pop(0)
        try:
            if self.cfg["backend"] == "ollama":
                reply = self._ask_ollama()
            else:
                reply = self._ask_anthropic()
        except Exception:
            self.history.pop()  # nie zostawiaj wiadomości bez odpowiedzi
            raise
        self.history.append({"role": "assistant", "content": reply})
        return reply

    def _system(self):
        return SYSTEM_PROMPT.format(title=USER_TITLE)

    def _ask_anthropic(self):
        key = self.cfg["api_key"]
        if not key:
            raise RuntimeError("Brak klucza API. Wpisz: /klucz <twój_klucz> albo przełącz na /backend ollama")
        data = _post_json(
            "https://api.anthropic.com/v1/messages",
            {
                "content-type": "application/json",
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
            },
            {
                "model": self.cfg["anthropic_model"],
                "max_tokens": self.cfg["max_tokens"],
                "system": self._system(),
                "messages": self.history,
            },
        )
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return text.strip() or "(pusta odpowiedź)"

    def _ask_ollama(self):
        msgs = [{"role": "system", "content": self._system()}] + self.history
        data = _post_json(
            self.cfg["ollama_url"].rstrip("/") + "/api/chat",
            {"content-type": "application/json"},
            {"model": self.cfg["ollama_model"], "messages": msgs, "stream": False},
            timeout=180,
        )
        return (data.get("message", {}).get("content", "") or "").strip() or "(pusta odpowiedź)"


# --------------------------------------------------------------------------
# Głos (TTS w osobnym wątku)
# --------------------------------------------------------------------------
FEMALE_HINTS = ("female", "kobiet", "zira", "paulina", "helena", "hazel", "anna", "maria")
MALE_HINTS = ("male", "męsk", "david", "adam", "jakub", "pawel", "mark")


def pick_voice(voices, female):
    def matches(v):
        name = (getattr(v, "name", "") or "").lower()
        if female:
            return any(h in name for h in FEMALE_HINTS)
        return any(h in name for h in MALE_HINTS) and "female" not in name

    def is_polish(v):
        blob = ((getattr(v, "name", "") or "") + str(getattr(v, "languages", ""))
                + (getattr(v, "id", "") or "")).lower()
        return "pl" in blob or "polish" in blob

    cands = sorted((v for v in voices if matches(v)), key=lambda v: not is_polish(v))
    return cands[0].id if cands else None


class EminenceVoiceEngine:
    def __init__(self):
        self.voice_mode = "MALE"
        self.muted = False
        self._q = queue.Queue()
        self._thread = None
        if pyttsx3 is not None:
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()

    def _worker(self):
        try:
            engine = pyttsx3.init()
            voices = engine.getProperty("voices") or []
        except Exception:
            return
        while True:
            item = self._q.get()
            if item is None:
                break
            text, female = item
            try:
                vid = pick_voice(voices, female)
                if vid:
                    engine.setProperty("voice", vid)
                engine.setProperty("rate", 150 if female else 125)
                engine.say(text)
                engine.runAndWait()
            except Exception:
                pass

    def toggle_profile(self):
        if self.voice_mode == "MALE":
            self.voice_mode = "FEMALE"
            return "Kobiecy / Aura"
        self.voice_mode = "MALE"
        return "Męski / Mroczny"

    def speak(self, text):
        if self.muted:
            return
        if self._thread is not None:
            self._q.put((text, self.voice_mode == "FEMALE"))
        elif plyer_tts is not None:
            try:
                plyer_tts.speak(message=text)
            except Exception:
                pass

    def shutdown(self):
        if self._thread is not None:
            self._q.put(None)


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
class GenesisCoreSymbol(FloatLayout):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.pulse = 1.0
        with self.canvas:
            Color(0.0, 0.6, 1.0, 0.2)
            self.glow = Ellipse()
            Color(1.0, 0.78, 0.0, 0.75)
            self.gold_ring = Line(circle=(0, 0, 1), width=2.0)
            Color(0.0, 0.9, 1.0, 0.9)
            self.blue_ring = Line(circle=(0, 0, 1), width=2.8)
            Color(1.0, 1.0, 1.0, 1.0)
            self.core = Ellipse()
        self.bind(pos=self.update_canvas, size=self.update_canvas)
        self.update_canvas()

    def update_canvas(self, *args):
        s = min(self.width, self.height) / 260.0 * self.pulse
        cx, cy = self.center_x, self.center_y
        g = 105 * s
        self.glow.pos = (cx - g, cy - g)
        self.glow.size = (2 * g, 2 * g)
        self.gold_ring.circle = (cx, cy, 75 * s)
        self.blue_ring.circle = (cx, cy, 60 * s)
        c = 17 * s
        self.core.pos = (cx - c, cy - c)
        self.core.size = (2 * c, 2 * c)


class GenesisActionButton(Button):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.background_normal = ""
        self.background_color = (0, 0, 0, 0)
        with self.canvas.before:
            Color(0.04, 0.08, 0.18, 1)
            self.rect = RoundedRectangle(pos=self.pos, size=self.size, radius=[8])
            Color(0.0, 0.7, 1.0, 0.85)
            self.line = Line(rounded_rectangle=[self.x, self.y, self.width, self.height, 8], width=1.3)
        self.bind(pos=self.update_bg, size=self.update_bg)
        self.update_bg(self, None)

    def update_bg(self, instance, value):
        self.rect.pos = self.pos
        self.rect.size = self.size
        self.line.rounded_rectangle = [self.x, self.y, self.width, self.height, 8]


class GenesisBootSplash(FloatLayout):
    def __init__(self, on_finish_callback, **kwargs):
        super().__init__(**kwargs)
        self.on_finish_callback = on_finish_callback
        self.symbol = GenesisCoreSymbol(size_hint=(None, None), size=(260, 260))
        self.symbol.pos_hint = {"center_x": 0.5, "center_y": 0.56}
        self.add_widget(self.symbol)
        self.add_widget(Label(
            text="THE EMINENCE AI", font_size="18sp", bold=True,
            color=(0.0, 0.85, 1.0, 1), size_hint=(1, None), height=30,
            pos_hint={"center_x": 0.5, "center_y": 0.32}))
        self.anim_t = 0
        self._pulse_ev = Clock.schedule_interval(self.pulse_splash, 0.03)
        self._finish_ev = Clock.schedule_once(self.finish_boot, 2.5)

    def pulse_splash(self, dt):
        self.anim_t += 0.1
        self.symbol.pulse = 1.0 + 0.08 * math.sin(self.anim_t * 3.5)
        self.symbol.update_canvas()

    def finish_boot(self, dt):
        self._pulse_ev.cancel()
        self.on_finish_callback()

    def cancel_events(self):
        self._pulse_ev.cancel()
        self._finish_ev.cancel()


class GenesisCommandCenter(BoxLayout):
    def __init__(self, voice, config, **kwargs):
        super().__init__(**kwargs)
        self.orientation = "vertical"
        self.padding = 12
        self.spacing = 10

        self.cfg = config
        self.brain = AIBrain(config)
        self.voice = voice
        self.busy = False

        header = BoxLayout(size_hint_y=None, height=48, spacing=10)
        header.add_widget(GenesisCoreSymbol(size_hint=(None, None), size=(38, 38)))

        titles = BoxLayout(orientation="vertical")
        l1 = Label(text="THE EMINENCE AI", font_size="14sp", bold=True,
                   color=(0.0, 0.8, 1.0, 1), halign="left")
        l1.bind(size=l1.setter("text_size"))
        self.status = Label(text="", font_size="9sp", color=(1.0, 0.78, 0.0, 0.9), halign="left")
        self.status.bind(size=self.status.setter("text_size"))
        titles.add_widget(l1)
        titles.add_widget(self.status)
        header.add_widget(titles)

        self.btn_mute = GenesisActionButton(text="🔊", size_hint_x=None, width=44)
        self.btn_mute.bind(on_press=self.toggle_mute)
        header.add_widget(self.btn_mute)

        self.btn_voice_switch = GenesisActionButton(text="Głos: Męski", size_hint_x=None, width=95)
        self.btn_voice_switch.bind(on_press=self.switch_voice_mode)
        header.add_widget(self.btn_voice_switch)
        self.add_widget(header)

        self.chat_logs = Label(
            text="[SYSTEM]: Gotowy. Wpisz /pomoc, aby zobaczyć komendy.",
            color=(0.9, 0.9, 0.95, 1), font_size="13sp",
            halign="left", valign="top", size_hint_y=None, markup=False)
        self.chat_logs.bind(texture_size=lambda i, v: setattr(i, "height", v[1]))
        self.chat_logs.bind(width=lambda i, w: setattr(i, "text_size", (w - 20, None)))

        self.scroll = ScrollView(size_hint=(1, 1))
        self.scroll.add_widget(self.chat_logs)
        self.add_widget(self.scroll)
        self._restore_history()

        ctrl = BoxLayout(orientation="vertical", size_hint_y=None, height=105, spacing=6)
        row_btns = BoxLayout(size_hint_y=None, height=38, spacing=8)
        self.b_mic = GenesisActionButton(text="🎤 Mikrofon")
        b_reset = GenesisActionButton(text="♻ Nowa rozmowa")
        self.b_mic.bind(on_press=self.listen)
        b_reset.bind(on_press=lambda *_: self.run_text("/reset"))
        row_btns.add_widget(self.b_mic)
        row_btns.add_widget(b_reset)
        ctrl.add_widget(row_btns)

        row_input = BoxLayout(size_hint_y=None, height=48, spacing=6)
        self.txt_input = TextInput(
            hint_text="Napisz do The Eminence AI...", multiline=False,
            background_color=(0.03, 0.05, 0.12, 1), foreground_color=(1, 1, 1, 1),
            cursor_color=(0.0, 0.85, 1.0, 1))
        self.txt_input.bind(on_text_validate=self.send_message)
        b_send = GenesisActionButton(text="➤", size_hint_x=None, width=52)
        b_send.bind(on_press=self.send_message)
        row_input.add_widget(self.txt_input)
        row_input.add_widget(b_send)
        ctrl.add_widget(row_input)
        self.add_widget(ctrl)

        self.refresh_status()
        if self.cfg["backend"] == "anthropic" and not self.cfg["api_key"]:
            self.append_log("SYSTEM", "Brak klucza API. Wpisz: /klucz <twój_klucz>\n"
                                      "albo użyj lokalnego modelu: /backend ollama")

    # --- trwała historia rozmowy ---
    def _restore_history(self):
        self.hist_path = os.path.join(App.get_running_app().user_data_dir, "history.json")
        try:
            with open(self.hist_path, "r", encoding="utf-8") as f:
                hist = json.load(f)
            lines = []
            for m in hist:
                who = "TY" if m["role"] == "user" else "THE EMINENCE AI"
                lines.append(f"[{who}]: {m['content']}")
        except (OSError, ValueError, KeyError, TypeError):
            return
        self.brain.history = hist
        if lines:
            self.chat_logs.text += "\n\n" + "\n\n".join(lines)
            Clock.schedule_once(lambda dt: setattr(self.scroll, "scroll_y", 0), 0.3)

    def _save_history(self):
        try:
            with open(self.hist_path, "w", encoding="utf-8") as f:
                json.dump(self.brain.history, f, ensure_ascii=False)
        except OSError:
            pass

    # --- pomocnicze ---
    def refresh_status(self, text=None):
        if text:
            self.status.text = text
            return
        b = self.cfg["backend"]
        model = self.cfg["anthropic_model"] if b == "anthropic" else self.cfg["ollama_model"]
        self.status.text = f"{b.upper()} · {model}"

    def append_log(self, sender, text):
        self.chat_logs.text += f"\n\n[{sender}]: {text}"
        Clock.schedule_once(lambda dt: setattr(self.scroll, "scroll_y", 0), 0.15)

    def toggle_mute(self, instance):
        self.voice.muted = not self.voice.muted
        self.btn_mute.text = "🔇" if self.voice.muted else "🔊"

    def switch_voice_mode(self, instance):
        mode = self.voice.toggle_profile()
        self.btn_voice_switch.text = f"Głos: {mode.split('/')[0].strip()}"
        self.append_log("SYSTEM", f"Profil głosu: {mode}")

    # --- wysyłanie ---
    def send_message(self, instance):
        msg = self.txt_input.text.strip()
        if not msg:
            return
        self.txt_input.text = ""
        self.run_text(msg)
        Clock.schedule_once(lambda dt: setattr(self.txt_input, "focus", True), 0.2)

    def run_text(self, msg):
        if self.busy:
            self.append_log("SYSTEM", "Poczekaj na odpowiedź...")
            return
        # Komendy z kluczem nie logujemy w całości
        shown = "/klucz ***" if msg.lower().startswith("/klucz") else msg
        self.append_log("TY", shown)

        cmd_reply = self.brain.try_command(msg)
        if cmd_reply is not None:
            self.append_log("SYSTEM", cmd_reply)
            self.refresh_status()
            self._save_history()
            return

        self.busy = True
        self.refresh_status("MYŚLĘ...")
        threading.Thread(target=self._worker, args=(msg,), daemon=True).start()

    def _worker(self, msg):
        try:
            reply = self.brain.ask(msg)
            Clock.schedule_once(lambda dt: self._done(reply, None))
        except Exception as e:
            err = str(e)
            Clock.schedule_once(lambda dt: self._done(None, err))

    def _done(self, reply, err):
      
