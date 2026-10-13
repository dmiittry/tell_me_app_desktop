"""Tell Me: Windows text-to-speech notes application."""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import shutil
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import uuid
import webbrowser

from narration import NarrationEngine, format_time
from audio_chunks import SAMPLE_RATE
from speech_text import find_links, prepare_for_speech


APP_NAME = "Tell Me"
DATA_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "TellMe"
DATA_FILE = DATA_DIR / "notes.json"


class TellMeApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME + " — умный рассказчик · v7")
        self.geometry("920x680")
        self.minsize(860, 520)
        self.configure(bg="#f4f6fa")

        self.events: queue.Queue[dict] = queue.Queue()
        try:
            self.engine: NarrationEngine | None = NarrationEngine(self.events, DATA_DIR / "audio")
            engine_error = ""
        except Exception as exc:
            self.engine = None
            engine_error = str(exc)
        self.data = self._load_data()
        self.editors: dict[str, tk.Text] = {}
        self.frames: dict[str, ttk.Frame] = {}
        self.link_jobs: dict[str, str] = {}
        self.save_job: str | None = None
        self.voice_label_to_id: dict[str, str] = {}
        self.playback_state = "stopped"
        self.scrubbing = False
        self.current_job = 0
        self.current_note: str | None = None
        self.prepare_job: str | None = None
        self.restoring = True
        self.karaoke_content = ''
        self.karaoke_word = None
        self.karaoke_spoken = 0

        self._build_ui()
        self._restore_notes()
        self._load_voices()
        self.restoring = False
        self.after_idle(self._on_tab_changed)
        if engine_error:
            self.status_var.set("Звуковой проигрыватель недоступен: " + engine_error)
        self.after(100, self._process_events)
        self.after(250, self._poll_playback)
        self.protocol("WM_DELETE_WINDOW", self._close)

    @staticmethod
    def _load_data() -> dict:
        try:
            data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            if isinstance(data.get("notes"), list):
                return data
        except (OSError, ValueError, AttributeError):
            pass
        return {"version": 1, "notes": [{"id": str(uuid.uuid4()), "title": "Заметка 1", "text": ""}],
                "active_id": None, "voice": "", "rate": 1.0, "volume": 100}

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TNotebook", background="#f4f6fa", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(14, 8))

        header = tk.Frame(self, bg="#1e3454", padx=18, pady=15)
        header.pack(fill="x")
        tk.Label(header, text="Tell Me", bg="#1e3454", fg="white", font=("Segoe UI", 20, "bold")).pack(anchor="w")
        tk.Label(header, text="Пишите, сохраняйте и слушайте свои тексты", bg="#1e3454", fg="#d8e4f4", font=("Segoe UI", 10)).pack(anchor="w")

        tools = ttk.Frame(self, padding=(14, 12, 14, 8))
        tools.pack(fill="x")
        ttk.Button(tools, text="+ Заметка", command=self._add_note).pack(side="left", padx=(0, 6))
        ttk.Button(tools, text="Переименовать", command=self._rename_note).pack(side="left", padx=(0, 6))
        ttk.Button(tools, text="Удалить", command=self._delete_note).pack(side="left")
        self.karaoke_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(tools, text="Караоке", variable=self.karaoke_var,
                        command=self._toggle_karaoke).pack(side="left", padx=(12, 0))
        self.auto_prepare_var = tk.BooleanVar(value=bool(self.data.get("auto_prepare", True)))
        ttk.Checkbutton(tools, text="Готовить звук автоматически", variable=self.auto_prepare_var,
                        command=self._on_auto_prepare).pack(side="right")

        text_tools = ttk.Frame(self, padding=(14, 0, 14, 8))
        text_tools.pack(fill="x")
        ttk.Button(text_tools, text="Очистить", command=self._clear_note).pack(side="left", padx=(0, 6))
        ttk.Button(text_tools, text="Вставить", command=self._paste_note).pack(side="left", padx=(0, 6))
        ttk.Button(text_tools, text="Скопировать", command=self._copy_note).pack(side="left")

        self.notebook = ttk.Notebook(self)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self.karaoke_frame = ttk.Frame(self.notebook, padding=12)
        karaoke_header = ttk.Frame(self.karaoke_frame)
        karaoke_header.pack(fill="x", pady=(0, 8))
        self.karaoke_hint = tk.StringVar(value="Караоке — слова подсвечиваются вслед за голосом")
        ttk.Label(karaoke_header, textvariable=self.karaoke_hint).pack(side="left")
        ttk.Button(karaoke_header, text="К тексту", command=self._show_editor).pack(side="right")
        self.karaoke_text = tk.Text(self.karaoke_frame, wrap="word", state="disabled",
            font=("Segoe UI", 22), bg="#152b47", fg="#acbed5", padx=24, pady=20,
            relief="flat", cursor="arrow", spacing1=8, spacing3=8)
        self.karaoke_text.pack(fill="both", expand=True)
        self.karaoke_text.bind("<<Paste>>", lambda _e: self._paste_note())
        self.karaoke_text.bind("<Control-KeyPress>", self._editor_shortcut)
        self.karaoke_text.tag_configure("spoken", foreground="#f0f4fa")
        self.karaoke_text.tag_configure("current", background="#ffd36a", foreground="#152b47")

        bottom = ttk.Frame(self)
        bottom.pack(side="bottom", fill="x")

        controls = ttk.Frame(bottom, padding=(14, 2, 14, 8))
        controls.pack(fill="x")
        self.play_button = ttk.Button(controls, text="▶ Читать", command=self._play)
        self.play_button.pack(side="left", padx=(0, 6))
        self.pause_button = ttk.Button(controls, text="⏸ Пауза", command=self._pause_or_resume, state="disabled")
        self.pause_button.pack(side="left", padx=(0, 6))
        self.stop_button = ttk.Button(controls, text="⏹ Стоп", command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=(0, 15))
        ttk.Label(controls, text="Голос:").pack(side="left")
        self.voice_var = tk.StringVar()
        self.voice_combo = ttk.Combobox(controls, textvariable=self.voice_var, state="disabled", width=43)
        self.voice_combo.pack(side="left", padx=(6, 0))
        self.voice_combo.bind("<<ComboboxSelected>>", self._on_voice_changed)

        timeline = ttk.Frame(bottom, padding=(14, 0, 14, 6))
        timeline.pack(fill="x")
        self.back_button = ttk.Button(timeline, text="−10 с", width=7, command=lambda: self._skip(-10), state="disabled")
        self.back_button.pack(side="left", padx=(0, 6))
        self.timeline_var = tk.DoubleVar(value=0.0)
        self.time_scale = ttk.Scale(timeline, from_=0, to=1, variable=self.timeline_var,
                                    orient="horizontal", state="disabled")
        self.time_scale.pack(side="left", fill="x", expand=True)
        self.time_scale.bind("<ButtonPress-1>", self._start_scrub)
        self.time_scale.bind("<ButtonRelease-1>", self._finish_scrub)
        self.forward_button = ttk.Button(timeline, text="+10 с", width=7, command=lambda: self._skip(10), state="disabled")
        self.forward_button.pack(side="left", padx=(6, 10))
        self.time_var = tk.StringVar(value="00:00 / 00:00")
        ttk.Label(timeline, textvariable=self.time_var, width=14).pack(side="right")

        settings = ttk.Frame(bottom, padding=(14, 0, 14, 6))
        settings.pack(fill="x")
        ttk.Label(settings, text="Скорость:").pack(side="left")
        self.rate_var = tk.DoubleVar(value=max(0.5, min(2.0, float(self.data.get("rate", 1.0) or 1.0))))
        self.rate_scale = ttk.Scale(settings, from_=0.5, to=2.0, variable=self.rate_var, orient="horizontal", length=150,
                                    command=self._on_rate_change, state="disabled")
        self.rate_scale.pack(side="left", padx=(6, 4))
        self.rate_label = ttk.Label(settings, text=f"{self.rate_var.get():.2f}×", width=6)
        self.rate_label.pack(side="left", padx=(0, 16))
        ttk.Label(settings, text="Громкость:").pack(side="left")
        self.volume_var = tk.IntVar(value=max(0, min(100, int(self.data.get("volume", 100)))))
        ttk.Scale(settings, from_=0, to=100, variable=self.volume_var, orient="horizontal", length=170,
                  command=self._on_volume_change).pack(side="left", padx=(6, 4))
        self.volume_label = ttk.Label(settings, text=f"{self.volume_var.get()}%", width=5)
        self.volume_label.pack(side="left")
        ttk.Button(settings, text="Что будет прочитано", command=self._show_preview).pack(side="right")

        record_tools = ttk.Frame(bottom, padding=(14, 0, 14, 6))
        record_tools.pack(fill="x")
        ttk.Button(record_tools, text="Папка записей", command=self._open_audio_folder).pack(side="left")
        self.export_button = ttk.Button(record_tools, text="Сохранить WAV как…", command=self._export_audio, state="disabled")
        self.export_button.pack(side="left", padx=6)
        self.progress_var = tk.StringVar(value="Скорость и перемотка — после подготовки")
        ttk.Label(record_tools, textvariable=self.progress_var).pack(side="left", padx=6)

        self.status_var = tk.StringVar(value="Вставьте текст — озвучка подготовится автоматически. Заметки сохраняются сами.")
        status = tk.Label(bottom, textvariable=self.status_var, bg="#e7edf5", fg="#33465e",
                          font=("Segoe UI", 9), anchor="w", padx=14, pady=8)
        status.pack(fill="x")
        self.notebook.pack(fill="both", expand=True, padx=14, pady=(0, 8))

    def _restore_notes(self) -> None:
        notes = self.data.get("notes") or []
        if not notes:
            notes = [{"id": str(uuid.uuid4()), "title": "Заметка 1", "text": ""}]
        for note in notes:
            self._create_tab(str(note.get("id") or uuid.uuid4()), str(note.get("title") or "Заметка"),
                             str(note.get("text") or ""))
        active = self.data.get("active_id")
        if active in self.frames:
            self.notebook.select(self.frames[active])

    def _create_tab(self, note_id: str, title: str, text: str = "") -> None:
        frame = ttk.Frame(self.notebook)
        editor = tk.Text(frame, wrap="word", undo=True, exportselection=False, font=("Segoe UI", 12), bg="white", fg="#1e2938",
                         insertbackground="#1e2938", padx=16, pady=14, relief="flat")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=editor.yview)
        editor.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        editor.pack(fill="both", expand=True)
        editor.insert("1.0", text)
        editor.edit_modified(False)
        editor.bind("<<Modified>>", lambda _e, nid=note_id: self._on_edit(nid))
        editor.bind("<<Paste>>", lambda _e, nid=note_id: self._paste_note(nid))
        editor.bind("<Control-KeyPress>", self._editor_shortcut)
        self.notebook.add(frame, text=title)
        self.frames[note_id] = frame
        self.editors[note_id] = editor
        self.after_idle(lambda nid=note_id: self._update_links(nid))

    def _active_id(self) -> str | None:
        selected = self.notebook.select()
        return next((nid for nid, frame in self.frames.items() if str(frame) == selected), None)

    def _editor_shortcut(self, event: tk.Event) -> str | None:
        # Windows virtual key codes stay the same with English and Russian layouts.
        if event.state & 0x20000:  # Do not intercept Ctrl+Alt (AltGr).
            return None
        key = chr(event.keycode).lower() if self.tk.call("tk", "windowingsystem") == "win32" else event.keysym.lower()
        action = {"v": "<<Paste>>", "c": "<<Copy>>", "x": "<<Cut>>",
                  "a": "<<SelectAll>>", "z": "<<Undo>>", "y": "<<Redo>>"}.get(key)
        if action is None:
            return None
        if key == "z" and event.state & 0x1:
            action = "<<Redo>>"
        event.widget.event_generate(action)
        return "break"

    def _paste_note(self, note_id: str | None = None) -> str:
        note_id = note_id or self._active_id()
        editor = self.editors.get(note_id)
        if editor is None:
            return "break"
        try:
            text = self.clipboard_get()
        except tk.TclError:
            self.status_var.set("В буфере нет текста или буфер обмена недоступен.")
            return "break"
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not text:
            self.status_var.set("В буфере обмена пустой текст.")
            return "break"
        self._show_editor()
        editor.edit_separator()
        selection = editor.tag_ranges("sel")
        if selection:
            editor.replace(selection[0], selection[1], text)
        else:
            editor.insert("insert", text)
        editor.tag_remove("sel", "1.0", "end")
        editor.edit_separator()
        editor.see("insert")
        editor.focus_set()
        self._on_edit(note_id)
        self._schedule_prepare(note_id, delay=0)
        self.status_var.set("Текст вставлен в открытую заметку.")
        return "break"

    def _copy_note(self) -> None:
        editor = self.editors.get(self._active_id())
        if editor is None:
            return
        text = editor.get("1.0", "end-1c")
        if not text:
            self.status_var.set("Открытая заметка пустая — копировать нечего.")
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
        except tk.TclError:
            self.status_var.set("Не удалось скопировать текст: буфер обмена недоступен.")
            return
        self.status_var.set("Весь текст открытой заметки скопирован.")

    def _clear_note(self) -> None:
        note_id = self._active_id()
        editor = self.editors.get(note_id)
        if editor is None:
            return
        self._show_editor()
        editor.edit_separator()
        editor.delete("1.0", "end")
        editor.edit_separator()
        editor.focus_set()
        self._on_edit(note_id)
        self._cancel_prepare()
        if self.engine and self.current_note == note_id:
            self.engine.stop()
            self.engine.recording = None
            self.engine.duration = 0
            self.engine.speech_text = ""
            self._refresh_player()
        self.status_var.set("Заметка очищена. Ctrl+Z возвращает текст.")

    def _on_edit(self, note_id: str) -> None:
        editor = self.editors.get(note_id)
        if not editor or not editor.edit_modified():
            return
        editor.edit_modified(False)
        self._schedule_save()
        if note_id in self.link_jobs:
            self.after_cancel(self.link_jobs[note_id])
        self.link_jobs[note_id] = self.after(400, lambda nid=note_id: self._update_links(nid))
        self._schedule_prepare(note_id)

    def _cancel_prepare(self) -> None:
        if self.prepare_job:
            self.after_cancel(self.prepare_job)
            self.prepare_job = None

    def _schedule_prepare(self, note_id: str | None, delay: int = 500) -> None:
        if self.restoring or not self.engine or not self.auto_prepare_var.get():
            return
        if not note_id or note_id != self._active_id() or note_id not in self.editors:
            return
        self._cancel_prepare()
        self.prepare_job = self.after(delay, lambda: self._prepare_active(note_id))

    def _prepare_active(self, note_id: str) -> None:
        self.prepare_job = None
        if not self.engine or not self.auto_prepare_var.get() or note_id != self._active_id():
            return
        editor = self.editors.get(note_id)
        if not editor:
            return
        text = prepare_for_speech(editor.get("1.0", "end-1c"))
        voice = self.voice_label_to_id.get(self.voice_var.get(), "")
        if not text:
            if self.current_note == note_id:
                self.engine.stop()
                self.engine.recording = None
                self.engine.duration = 0
                self.engine.speech_text = ''
            self._refresh_player()
            return
        if voice:
            title = self.notebook.tab(self.frames[note_id], "text")
            self.current_note = note_id
            self.current_job = self.engine.request(text, voice, title, autoplay=False)
            self._refresh_player()

    def _on_tab_changed(self, _event=None) -> None:
        if self.restoring:
            return
        self._cancel_prepare()
        self._show_editor()
        note_id = self._active_id()
        if self.engine and self.current_note and self.current_note != note_id:
            self.engine.stop()
            self.engine.recording = None
            self.engine.duration = 0
            self.engine.speech_text = ''
            self.current_note = None
        self._schedule_save()
        self._schedule_prepare(note_id, delay=0)
        self._refresh_player()

    def _on_voice_changed(self, _event=None) -> None:
        self._schedule_save()
        self._schedule_prepare(self._active_id(), delay=0)

    def _on_auto_prepare(self) -> None:
        self._cancel_prepare()
        if self.auto_prepare_var.get():
            self._schedule_prepare(self._active_id(), delay=0)
        elif self.engine and self.engine.background and not self.engine.recording:
            self.engine.stop()
        self._schedule_save()
        self._refresh_player()

    def _show_editor(self) -> None:
        self.karaoke_var.set(False)
        self.karaoke_frame.place_forget()

    def _toggle_karaoke(self) -> None:
        note_id = self._active_id()
        if self.karaoke_var.get() and note_id:
            self.karaoke_frame.place(in_=self.frames[note_id], x=0, y=0, relwidth=1, relheight=1)
            self.karaoke_frame.lift()
            self._refresh_karaoke()
        else:
            self.karaoke_frame.place_forget()

    def _refresh_karaoke(self) -> None:
        if not self.engine:
            return
        text = self.engine.speech_text
        if text != self.karaoke_content:
            self.karaoke_text.configure(state="normal")
            self.karaoke_text.delete("1.0", "end")
            self.karaoke_text.insert("1.0", text or "Вставьте текст в заметку и нажмите «Читать».")
            self.karaoke_text.configure(state="disabled")
            self.karaoke_text.see("1.0")
            self.karaoke_content = text
            self.karaoke_word = None
            self.karaoke_spoken = 0
        approximate = self.engine.karaoke.mode == 'estimated'
        self.karaoke_hint.set("Караоке · приблизительная подсветка" if approximate else
                             "Караоке · подсветка слов по голосу")
        if not self.karaoke_var.get():
            return
        position = self.engine.position()
        cue = self.engine.karaoke.at(position)
        span = (cue['start'], cue['end']) if cue else None
        spoken = self.engine.karaoke.spoken_end(position)
        if self.engine.state in ('stopped', 'error', 'finished'):
            span = None
            spoken = len(text) if self.engine.state == 'finished' else 0
        if span != self.karaoke_word or spoken != self.karaoke_spoken:
            self.karaoke_text.tag_remove("current", "1.0", "end")
            self.karaoke_text.tag_remove("spoken", "1.0", "end")
            if spoken:
                self.karaoke_text.tag_add("spoken", "1.0", f"1.0+{spoken}c")
            if span:
                start, end = (f"1.0+{offset}c" for offset in span)
                self.karaoke_text.tag_add("current", start, end)
                self.karaoke_text.see(start)
            self.karaoke_word = span
            self.karaoke_spoken = spoken

    def _update_links(self, note_id: str) -> None:
        editor = self.editors.get(note_id)
        if not editor:
            return
        text = editor.get("1.0", "end-1c")
        for tag in editor.tag_names():
            if tag.startswith("web_link_"):
                editor.tag_delete(tag)
        for index, (start, end, url) in enumerate(find_links(text)):
            tag = f"web_link_{index}"
            editor.tag_add(tag, f"1.0+{start}c", f"1.0+{end}c")
            editor.tag_config(tag, foreground="#1765ad", underline=True)
            editor.tag_bind(tag, "<Button-1>", lambda _e, target=url: webbrowser.open(target))
            editor.tag_bind(tag, "<Enter>", lambda _e, widget=editor: widget.configure(cursor="hand2"))
            editor.tag_bind(tag, "<Leave>", lambda _e, widget=editor: widget.configure(cursor="xterm"))

    def _add_note(self) -> None:
        note_id = str(uuid.uuid4())
        number = len(self.editors) + 1
        self._create_tab(note_id, f"Заметка {number}")
        self.notebook.select(self.frames[note_id])
        self.editors[note_id].focus_set()
        self._schedule_save()

    def _rename_note(self) -> None:
        note_id = self._active_id()
        if not note_id:
            return
        old = self.notebook.tab(self.frames[note_id], "text")
        new = simpledialog.askstring("Название заметки", "Новое название:", initialvalue=old, parent=self)
        if new and new.strip():
            self.notebook.tab(self.frames[note_id], text=new.strip()[:60])
            self._schedule_save()

    def _delete_note(self) -> None:
        note_id = self._active_id()
        if not note_id:
            return
        title = self.notebook.tab(self.frames[note_id], "text")
        if not messagebox.askyesno("Удалить заметку", f"Удалить «{title}»?", parent=self):
            return
        self._cancel_prepare()
        self._show_editor()
        if len(self.frames) == 1:
            self._add_note()
        frame = self.frames.pop(note_id)
        self.editors.pop(note_id)
        self.notebook.forget(frame)
        frame.destroy()
        self._schedule_save()

    def _current_speech(self) -> str:
        note_id = self._active_id()
        if not note_id:
            return ""
        editor = self.editors[note_id]
        try:
            selected = editor.get("sel.first", "sel.last")
        except tk.TclError:
            selected = editor.get("1.0", "end-1c")
        return prepare_for_speech(selected)

    def _load_voices(self) -> None:
        if not self.engine:
            self.play_button.configure(state="disabled")
            return
        self.voice_label_to_id = dict(self.engine.voices())
        labels = list(self.voice_label_to_id)
        self.voice_combo.configure(values=labels, state="readonly")
        saved = self.data.get("voice", "")
        preferred = next((label for label, voice_id in self.voice_label_to_id.items() if voice_id == saved), None)
        if not preferred and saved:
            preferred = next((label for label in labels if saved in label), None)
        self.voice_var.set(preferred or labels[0])
        self._on_rate_change(str(self.rate_var.get()))
        self._on_volume_change(str(self.volume_var.get()))

    def _play(self) -> None:
        self._cancel_prepare()
        text = self._current_speech()
        if not text:
            self.status_var.set("Вставьте текст в заметку или выделите фрагмент для чтения.")
            return
        if not self.engine:
            return
        voice_id = self.voice_label_to_id.get(self.voice_var.get(), "")
        if not voice_id:
            self.status_var.set("Выберите голос.")
            return
        note_id = self._active_id()
        title = self.notebook.tab(self.frames[note_id], "text") if note_id else "Заметка"
        self.current_note = note_id
        self.current_job = self.engine.request(text, voice_id, title)
        self.karaoke_var.set(True)
        self._toggle_karaoke()
        self._refresh_player()

    def _pause_or_resume(self) -> None:
        if not self.engine:
            return
        if self.engine.paused:
            self.engine.resume()
        else:
            self.engine.pause()
        self._refresh_player()

    def _stop(self) -> None:
        self._cancel_prepare()
        if self.engine:
            self.engine.stop()
            self.current_job = self.engine.generation
        self._refresh_player()

    def _on_rate_change(self, value: str) -> None:
        rate = round(float(value), 2)
        if hasattr(self, "rate_label"):
            self.rate_label.configure(text=f"{rate:.2f}×")
        if self.engine:
            self.engine.set_rate(rate)
        self._schedule_save()

    def _on_volume_change(self, value: str) -> None:
        volume = round(float(value))
        if hasattr(self, "volume_label"):
            self.volume_label.configure(text=f"{volume}%")
        if self.engine:
            self.engine.set_volume(volume)
        self._schedule_save()

    def _set_timeline(self, position: float, duration: float) -> None:
        self.timeline_var.set(position)
        self.time_var.set(f"{format_time(position)} / {format_time(duration)}")

    def _start_scrub(self, _event: tk.Event) -> None:
        self.scrubbing = True

    def _finish_scrub(self, _event: tk.Event) -> None:
        if self.scrubbing and self.engine and self.engine.recording:
            self.engine.seek(self.timeline_var.get())
        self.scrubbing = False
        self._refresh_player()

    def _skip(self, seconds: float) -> None:
        if self.engine and self.engine.recording:
            self.engine.seek(self.engine.position() + seconds)
            self._refresh_player()

    def _open_audio_folder(self) -> None:
        folder = DATA_DIR / "audio"
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(str(folder))

    def _export_audio(self) -> None:
        if not self.engine or not self.engine.recording:
            return
        target = filedialog.asksaveasfilename(parent=self, title="Сохранить запись",
            initialfile="Озвучка.wav", defaultextension=".wav", filetypes=[("Аудиозапись WAV", "*.wav")])
        if target:
            try:
                if Path(target).resolve() != self.engine.recording.resolve():
                    shutil.copyfile(self.engine.recording, target)
            except OSError as exc:
                messagebox.showerror("Не удалось сохранить запись", str(exc), parent=self)

    def _show_preview(self) -> None:
        text = self._current_speech()
        dialog = tk.Toplevel(self)
        dialog.title("Текст для озвучивания")
        dialog.geometry("620x420")
        dialog.transient(self)
        preview = tk.Text(dialog, wrap="word", font=("Segoe UI", 11), padx=14, pady=14)
        preview.pack(fill="both", expand=True)
        preview.insert("1.0", text or "В заметке пока нет текста.")
        preview.configure(state="disabled")
        ttk.Button(dialog, text="Закрыть", command=dialog.destroy).pack(pady=10)

    def _process_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if self.engine:
                    self.engine.handle_event(event)
                    self._refresh_player()
        except queue.Empty:
            pass
        except Exception as exc:
            if self.engine:
                self.engine.fail(str(exc))
            self._refresh_player()
        self.after(100, self._process_events)

    def _poll_playback(self) -> None:
        if self.engine:
            try:
                self.engine.poll()
                self._refresh_player()
            except Exception as exc:
                self.engine.fail(str(exc))
                self._refresh_player()
        self.after(50, self._poll_playback)

    def _refresh_player(self) -> None:
        if not self.engine:
            return
        engine = self.engine
        state = engine.state
        self.playback_state = state
        active = state in ("playing", "paused", "waiting", "generating", "preparing")
        ready = engine.recording is not None
        can_seek = ready and state in ("playing", "paused", "finished", "ready")
        self.pause_button.configure(state="normal" if active and not engine.background else "disabled",
                                    text="▶ Продолжить" if engine.paused else "⏸ Пауза")
        self.stop_button.configure(state="normal" if active else "disabled")
        self.rate_scale.configure(state="normal" if ready else "disabled")
        self.time_scale.configure(state="normal" if can_seek else "disabled", to=max(engine.duration, 0.1))
        self.back_button.configure(state="normal" if can_seek else "disabled")
        self.forward_button.configure(state="normal" if can_seek else "disabled")
        self.export_button.configure(state="normal" if ready else "disabled")
        if not self.scrubbing:
            if state == "stopped":
                self._set_timeline(0, engine.duration)
            elif ready:
                self._set_timeline(engine.position(), engine.duration)
            else:
                self.timeline_var.set(0)
                self.time_var.set(f"{format_time(engine.position())} / —")
        if ready:
            self.progress_var.set("Запись сохранена на ПК")
            self.rate_label.configure(text=f"{self.rate_var.get():.2f}×")
        elif active:
            if engine.live:
                received = format_time(engine.live.spool.frames / SAMPLE_RATE)
                self.progress_var.set(f"Получено звука: {received} · Сохраняю запись · Скорость 1×")
            else:
                self.progress_var.set(f"Готово фрагментов: {engine.rendered}/{engine.total} · Скорость 1×")
            self.rate_label.configure(text="1.00×")
        else:
            self.progress_var.set("Скорость и перемотка — после подготовки")
        if engine.paused and active:
            status = "Пауза. Подготовка и сохранение записи продолжаются."
            if ready:
                status = "Пауза"
        else:
            status = {
                "preparing": "Готовлю озвучку в фоне. Нажмите «Читать», когда захотите слушать.",
                "ready": "Озвучка готова. Нажмите «Читать» — откроется караоке.",
                "generating": "Подключаю голос, ожидаю звук…" if engine.streaming else "Готовлю первый фрагмент…",
                "waiting": "Ожидаю продолжение звука…" if engine.streaming else "Готовлю следующий фрагмент…",
                "playing": ("Читаю сохранённую запись…" if ready else
                    "Читаю; остальная запись загружается…" if engine.streaming else
                    f"Читаю фрагмент {engine.index + 1}/{engine.total}…"),
                "finished": "Чтение завершено. Запись сохранена; можно перемотать и продолжить.",
                "stopped": "Чтение остановлено.",
                "error": "Ошибка озвучивания: " + engine.error,
            }.get(state, "")
        self.status_var.set(status)
        self._refresh_karaoke()

    def _schedule_save(self) -> None:
        if self.save_job:
            self.after_cancel(self.save_job)
        self.save_job = self.after(500, self._save)

    def _save(self) -> None:
        self.save_job = None
        notes = []
        for note_id, frame in self.frames.items():
            notes.append({"id": note_id, "title": self.notebook.tab(frame, "text"),
                          "text": self.editors[note_id].get("1.0", "end-1c")})
        data = {"version": 1, "notes": notes, "active_id": self._active_id(),
                "voice": self.voice_label_to_id.get(self.voice_var.get(), ""),
                "rate": self.rate_var.get(), "volume": self.volume_var.get(),
                "auto_prepare": self.auto_prepare_var.get()}
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            temp = DATA_FILE.with_suffix(".json.tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(DATA_FILE)
        except OSError as exc:
            self.status_var.set(f"Не удалось сохранить заметки: {exc}")

    def _close(self) -> None:
        self._cancel_prepare()
        if self.save_job:
            self.after_cancel(self.save_job)
        self._save()
        if self.engine:
            self.engine.close()
        self.destroy()


if __name__ == "__main__":
    TellMeApp().mainloop()
