"""A small Tkinter editor for ReplayMod timelines.json files and .mcpr archives."""

from __future__ import annotations

import json
import math
import queue
import shutil
import threading
import tkinter as tk
import zipfile
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from types import SimpleNamespace
from typing import Any

from replay_preview import load_preview_data, project_camera_point, project_player


DEFAULT_RECORDINGS = Path.home() / "AppData/Roaming/PrismLauncher/instances/1.21/minecraft/replay_recordings"
KEYFRAME_DURATION_MS = 10 * 60 * 1000
MAX_CONNECTORS = 200


@dataclass
class ReplayData:
    source: Path
    source_is_archive: bool
    archive_entries: list[zipfile.ZipInfo]
    timelines: dict[str, list[dict[str, Any]]]
    metadata: dict[str, Any]
    original_text: str


def _read_replay(path: Path) -> ReplayData:
    """Read timelines and metadata from a folder or ZIP-based replay."""
    if path.is_dir():
        timeline_path = path / "timelines.json"
        if not timeline_path.is_file():
            raise ValueError(f"No timelines.json found in {path}")
        text = timeline_path.read_text(encoding="utf-8")
        metadata_path = path / "metaData.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
        return ReplayData(path, False, [], json.loads(text), metadata, text)

    with zipfile.ZipFile(path, "r") as archive:
        names = archive.namelist()
        timeline_name = next((name for name in names if name == "timelines.json" or name.endswith("/timelines.json")), None)
        if timeline_name is None:
            raise ValueError(f"No timelines.json found inside {path.name}")
        timeline_bytes = archive.read(timeline_name)
        metadata_name = next((name for name in names if name == "metaData.json" or name.endswith("/metaData.json")), None)
        metadata = json.loads(archive.read(metadata_name).decode("utf-8")) if metadata_name else {}
        # Keep only member metadata in memory; replay packet streams can be very large.
        entries = list(archive.infolist())
    text = timeline_bytes.decode("utf-8")
    parsed = json.loads(text)
    if timeline_name != "timelines.json":
        # ReplayMod normally stores the file at the archive root. Keep the discovered
        # location as metadata on the ReplayData instance for saving nested archives.
        data = ReplayData(path, True, entries, parsed, metadata, text)
        data.timeline_member = timeline_name
        return data
    data = ReplayData(path, True, entries, parsed, metadata, text)
    data.timeline_member = "timelines.json"
    return data


def _write_replay_as(data: ReplayData, destination: Path) -> None:
    """Write edited timeline data to a new folder or archive, preserving other files."""
    encoded = (json.dumps(data.timelines, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if data.source_is_archive:
        destination.parent.mkdir(parents=True, exist_ok=True)
        member = getattr(data, "timeline_member", "timelines.json")
        with zipfile.ZipFile(data.source, "r") as source_archive, zipfile.ZipFile(destination, "w") as output:
            for info in data.archive_entries:
                if info.filename == member:
                    output.writestr(info, encoded)
                else:
                    with source_archive.open(info, "r") as source, output.open(info, "w") as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
        return

    destination.mkdir(parents=True, exist_ok=True)
    source_dir = data.source
    # Copy the extracted replay's other files/directories, then update its timeline.
    for child in source_dir.iterdir():
        target = destination / child.name
        if child.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(child, target)
        elif child.name != "timelines.json":
            shutil.copy2(child, target)
    (destination / "timelines.json").write_bytes(encoded)


class TimelineEditor(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("ReplayMod Timeline Fixer")
        self.geometry("1180x820")
        self.minsize(820, 620)
        self.data: ReplayData | None = None
        self.path_name = ""
        self.selected: tuple[str, dict[str, Any]] | None = None
        self.problematic: dict[str, Any] | None = None
        self.bulk_selected: set[int] = set()
        self.bulk_pick_mode: str | None = None
        self.show_full_keyframe_timeline = tk.BooleanVar(value=False)
        self.force_connectors = tk.BooleanVar(value=False)
        self.connector_overlay: tk.Toplevel | None = None
        self.connector_canvas: tk.Canvas | None = None
        self.preview_enabled = tk.BooleanVar(value=False)
        self.preview_window: tk.Toplevel | None = None
        self.preview_canvas: tk.Canvas | None = None
        self.render_entity_models = tk.BooleanVar(value=False)
        self.preview_scenes: dict[str, dict[str, Any]] = {}
        self.preview_entities_by_frame: dict[str, list[dict[str, Any]]] = {}
        self.preview_data_loaded = False
        self.preview_photo: tk.PhotoImage | None = None
        self.preview_target_signature = ""
        self.preview_error = ""
        self.preview_loading = False
        self.preview_queue: queue.Queue[tuple[int, dict[str, Any] | None, str]] = queue.Queue()
        self.preview_load_token = 0
        self._suppress_path_event = False
        self._editor_vars: dict[str, tk.StringVar] = {}
        self._build_ui()
        self._create_connector_overlay()
        self.bind("<Delete>", self._delete_key_event, add="+")
        self._show_empty_state()
        self.after(100, self._poll_preview_results)

    def _build_ui(self) -> None:
        filebar = ttk.Frame(self, padding=(8, 8, 8, 4))
        filebar.pack(fill="x")
        ttk.Button(filebar, text="Open Replay…", command=self.open_replay).pack(side="left")
        ttk.Button(filebar, text="Open Folder…", command=self.open_folder).pack(side="left", padx=(5, 0))
        ttk.Button(filebar, text="Save As…", command=self.save_as).pack(side="left", padx=(5, 12))
        ttk.Label(filebar, text="Path:").pack(side="left")
        self.path_combo = ttk.Combobox(filebar, state="readonly", width=24)
        self.path_combo.pack(side="left", padx=5)
        self.path_combo.bind("<<ComboboxSelected>>", self._change_path)
        self.file_label = ttk.Label(filebar, text="No replay loaded", anchor="e")
        self.file_label.pack(side="right", fill="x", expand=True, padx=(8, 0))

        self.validation_bar = ttk.Frame(self, padding=(8, 2, 8, 7))
        self.validation_bar.pack(fill="x")
        ttk.Button(self.validation_bar, text="Find first backwards time keyframe", command=self.find_backwards).pack(side="left")
        self.validation_label = ttk.Label(self.validation_bar, text="")
        self.validation_label.pack(side="left", padx=10)
        ttk.Checkbutton(
            self.validation_bar,
            text="Camera preview",
            variable=self.preview_enabled,
            command=self._toggle_preview,
        ).pack(side="right")

        self.timeline_frame = ttk.Frame(self, padding=(8, 0, 8, 4))
        self.timeline_frame.pack(fill="both", expand=True)
        self._make_timeline(self.timeline_frame, "replay", "Replay Timeline")
        self._make_timeline(self.timeline_frame, "keyframe", "Keyframe Timeline (10 minutes)")

        bulk = ttk.LabelFrame(self, text="Bulk select / shift / scale keyframe positions", padding=7)
        bulk.pack(fill="x", padx=8, pady=(0, 5))
        ttk.Label(bulk, text="Start (ms)").grid(row=0, column=0, sticky="w")
        self.bulk_start = tk.StringVar(value="0")
        ttk.Entry(bulk, textvariable=self.bulk_start, width=11).grid(row=0, column=1, padx=(4, 5))
        self.bulk_start_button = ttk.Button(bulk, text="Select Start", command=lambda: self._begin_bulk_endpoint_pick("start"))
        self.bulk_start_button.grid(row=0, column=2, padx=(0, 8))
        ttk.Label(bulk, text="End (ms)").grid(row=0, column=3, sticky="w")
        self.bulk_end = tk.StringVar(value="0")
        ttk.Entry(bulk, textvariable=self.bulk_end, width=11).grid(row=0, column=4, padx=(4, 5))
        self.bulk_end_button = ttk.Button(bulk, text="Select End", command=lambda: self._begin_bulk_endpoint_pick("end"))
        self.bulk_end_button.grid(row=0, column=5, padx=(0, 8))
        ttk.Button(bulk, text="Select range", command=self.select_bulk_range).grid(row=0, column=6, padx=(0, 6))
        self.bulk_delete_button = ttk.Button(bulk, text="Delete selected", command=self.delete_bulk_selection)
        self.bulk_delete_button.grid(row=0, column=7, padx=(0, 10))
        ttk.Label(bulk, text="Shift by (ms)").grid(row=0, column=8, sticky="w")
        self.shift_amount = tk.StringVar(value="0")
        ttk.Entry(bulk, textvariable=self.shift_amount, width=9).grid(row=0, column=9, padx=(4, 5))
        ttk.Button(bulk, text="Shift", command=self.apply_shift).grid(row=0, column=10, padx=(0, 8))
        ttk.Label(bulk, text="Scale ×").grid(row=0, column=11, sticky="w")
        self.scale_factor = tk.StringVar(value="1.0")
        ttk.Entry(bulk, textvariable=self.scale_factor, width=8).grid(row=0, column=12, padx=(4, 5))
        self.pivot = tk.StringVar(value="midpoint")
        ttk.Combobox(bulk, textvariable=self.pivot, state="readonly", values=("start", "midpoint", "end"), width=9).grid(row=0, column=13, padx=(0, 5))
        ttk.Button(bulk, text="Scale", command=self.apply_scale).grid(row=0, column=14)
        self.bulk_label = ttk.Label(bulk, text="No keyframes selected")
        self.bulk_label.grid(row=1, column=0, columnspan=15, sticky="w", pady=(5, 0))

        self.editor = ttk.LabelFrame(self, text="Selected keyframe", padding=8)
        self.editor.pack(fill="x", padx=8, pady=(0, 8))
        self.editor_content = ttk.Frame(self.editor)
        self.editor_content.pack(fill="x")

    def _make_timeline(self, parent: ttk.Frame, kind: str, title: str) -> None:
        section = ttk.LabelFrame(parent, text=title, padding=5)
        section.pack(fill="x", pady=(0, 6))
        controls = ttk.Frame(section)
        controls.pack(fill="x")
        ttk.Button(controls, text="−", width=3, command=lambda: self.zoom(kind, 0.75)).pack(side="left")
        ttk.Button(controls, text="+", width=3, command=lambda: self.zoom(kind, 1.33)).pack(side="left", padx=(3, 8))
        ttk.Label(controls, text="Drag scrollbar or Shift+mouse wheel to scroll horizontally").pack(side="left")
        if kind == "keyframe":
            ttk.Checkbutton(
                controls,
                text="Show entire keyframe timeline",
                variable=self.show_full_keyframe_timeline,
                command=self._toggle_keyframe_range,
            ).pack(side="right", padx=(8, 2))
            ttk.Checkbutton(
                controls,
                text="Force connectors (>200)",
                variable=self.force_connectors,
                command=self.redraw,
            ).pack(side="right", padx=(8, 2))
        canvas = tk.Canvas(section, height=88 if kind == "replay" else 104, background="#20242b", highlightthickness=0)
        canvas.pack(fill="x", expand=True, pady=(3, 0))
        scrollbar = ttk.Scrollbar(section, orient="horizontal", command=canvas.xview)
        scrollbar.pack(fill="x")
        canvas.configure(xscrollcommand=lambda first, last, k=kind: self._xscroll_changed(k, first, last))
        canvas.bind("<Button-1>", lambda event, k=kind: self._timeline_click(k, event))
        canvas.bind("<MouseWheel>", lambda event, k=kind: self._wheel_zoom(k, event))
        canvas.bind("<Configure>", lambda _event: self.redraw())
        setattr(self, f"{kind}_canvas", canvas)
        setattr(self, f"{kind}_section", section)
        setattr(self, f"{kind}_scrollbar", scrollbar)
        setattr(self, f"{kind}_zoom", 1.0)

    def _show_empty_state(self) -> None:
        self.replay_canvas.create_text(20, 34, anchor="w", fill="#bdc5d2", text="Open an extracted replay folder or .mcpr archive to begin.")
        self.keyframe_canvas.create_text(20, 45, anchor="w", fill="#bdc5d2", text="Keyframe tracks will appear here.")
        self._clear_editor("Select a keyframe to edit its properties.")

    def _create_connector_overlay(self) -> None:
        """Create a transparent, click-through layer for cross-timeline connectors."""
        transparent = "#010203"
        try:
            overlay = tk.Toplevel(self)
            overlay.overrideredirect(True)
            overlay.configure(background=transparent)
            overlay.wm_attributes("-transparentcolor", transparent)
            canvas = tk.Canvas(overlay, background=transparent, highlightthickness=0, borderwidth=0)
            canvas.pack(fill="both", expand=True)
            canvas.bind("<Button-1>", self._connector_overlay_click)
            self.connector_overlay = overlay
            self.connector_canvas = canvas
            self.timeline_frame.bind("<Configure>", lambda _event: self._schedule_connector_update(), add="+")
            self.bind("<Configure>", self._root_configured, add="+")
            self.after_idle(self._update_connector_overlay)
        except tk.TclError:
            # Keep the editor usable on Tk platforms without transparent windows.
            if self.connector_overlay is not None:
                self.connector_overlay.destroy()
            self.connector_overlay = None
            self.connector_canvas = None

    def _root_configured(self, event: tk.Event[Any]) -> None:
        if event.widget is self:
            self._schedule_connector_update()

    def _connector_overlay_click(self, event: tk.Event[Any]) -> str:
        for kind in ("replay", "keyframe"):
            canvas = getattr(self, f"{kind}_canvas")
            left, top = canvas.winfo_rootx(), canvas.winfo_rooty()
            x, y = event.x_root - left, event.y_root - top
            if 0 <= x < canvas.winfo_width() and 0 <= y < canvas.winfo_height():
                self._timeline_click(kind, SimpleNamespace(x=x, y=y))
                return "break"
        return "break"

    def _schedule_connector_update(self) -> None:
        if self.connector_overlay is not None:
            self.after_idle(self._update_connector_overlay)

    def _xscroll_changed(self, kind: str, first: str, last: str) -> None:
        getattr(self, f"{kind}_scrollbar").set(first, last)
        self._schedule_connector_update()

    def _update_connector_overlay(self) -> None:
        overlay = self.connector_overlay
        connector_canvas = self.connector_canvas
        if overlay is None or connector_canvas is None or not overlay.winfo_exists():
            return
        frame = self.timeline_frame
        width = max(1, frame.winfo_width())
        height = max(1, frame.winfo_height())
        x0, y0 = frame.winfo_rootx(), frame.winfo_rooty()
        overlay.geometry(f"{width}x{height}+{x0}+{y0}")
        overlay.lift()
        connector_canvas.configure(width=width, height=height)
        connector_canvas.delete("all")
        if self.data is None:
            return

        low, high = self._visible_keyframe_range()
        visible = [
            (kind, frame_data)
            for kind, frames in (("position", self.position_frames), ("time", self.time_frames))
            for frame_data in frames
            if low <= float(frame_data.get("time", 0)) <= high
        ]
        if len(visible) > MAX_CONNECTORS and not self.force_connectors.get():
            return

        replay_canvas = self.replay_canvas
        keyframe_canvas = self.keyframe_canvas
        replay_origin_x = replay_canvas.winfo_rootx() - x0 - replay_canvas.canvasx(0)
        keyframe_origin_x = keyframe_canvas.winfo_rootx() - x0 - keyframe_canvas.canvasx(0)
        replay_origin_y = replay_canvas.winfo_rooty() - y0
        keyframe_origin_y = keyframe_canvas.winfo_rooty() - y0
        replay_y = replay_origin_y + replay_canvas.winfo_height() // 2
        for kind, frame_data in visible:
            keyframe_x = keyframe_origin_x + self._x_for("keyframe", float(frame_data["time"]))
            keyframe_y = keyframe_origin_y + (32 if kind == "position" else 75)
            replay_x = replay_origin_x + self._x_for("replay", self._frame_replay_time(kind, frame_data))
            connector_canvas.create_line(keyframe_x, keyframe_y, replay_x, replay_y, fill="#e55353", width=1)

    def open_replay(self) -> None:
        choice = filedialog.askopenfilename(
            title="Open ReplayMod recording",
            initialdir=str(DEFAULT_RECORDINGS if DEFAULT_RECORDINGS.exists() else Path.home()),
            filetypes=(("Replay archives", "*.mcpr *.zip"), ("All files", "*.*")),
        )
        if not choice:
            return
        self._load_replay(Path(choice))

    def open_folder(self) -> None:
        folder = filedialog.askdirectory(
            title="Open extracted ReplayMod folder",
            initialdir=str(DEFAULT_RECORDINGS if DEFAULT_RECORDINGS.exists() else Path.home()),
        )
        if folder:
            self._load_replay(Path(folder))

    def _load_replay(self, path: Path) -> None:
        try:
            self.data = _read_replay(path)
            self._validate_schema()
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
            self.data = None
            messagebox.showerror("Could not open replay", str(exc), parent=self)
            return
        names = list(self.data.timelines)
        if not names:
            messagebox.showerror("Empty timelines", "No timeline paths were found in timelines.json.", parent=self)
            self.data = None
            return
        self._suppress_path_event = True
        self.path_combo.configure(values=names)
        self.path_combo.current(0)
        self._suppress_path_event = False
        self.path_name = names[0]
        self.show_full_keyframe_timeline.set(False)
        self.selected = None
        self.problematic = None
        self.bulk_selected.clear()
        self.bulk_pick_mode = None
        self.bulk_start_button.configure(text="Select Start")
        self.bulk_end_button.configure(text="Select End")
        self.file_label.configure(text=str(self.data.source))
        self.validation_label.configure(text="")
        self.bulk_label.configure(text="No keyframes selected")
        self.preview_load_token += 1
        self.preview_scenes = {}
        self.preview_entities_by_frame = {}
        self.preview_data_loaded = False
        self.preview_photo = None
        self.preview_target_signature = ""
        self.preview_error = ""
        self.preview_loading = False
        self._clear_editor("Select a keyframe to edit its properties.")
        for kind in ("replay", "keyframe"):
            setattr(self, f"{kind}_zoom", 1.0)
            getattr(self, f"{kind}_canvas").xview_moveto(0)
        self.redraw()
        if self.preview_enabled.get():
            self._ensure_preview_loaded()

    def _validate_schema(self) -> None:
        assert self.data is not None
        if not isinstance(self.data.timelines, dict):
            raise ValueError("timelines.json must contain a JSON object of named paths")
        for name, tracks in self.data.timelines.items():
            if not isinstance(tracks, list) or len(tracks) < 2:
                raise ValueError(f"Timeline {name!r} must contain time-keyframe and position-keyframe tracks")
            for track in tracks[:2]:
                if not isinstance(track, dict) or not isinstance(track.get("keyframes"), list):
                    raise ValueError(f"Timeline {name!r} contains an invalid keyframe track")
                for frame in track["keyframes"]:
                    if not isinstance(frame, dict) or not isinstance(frame.get("time"), (int, float)):
                        raise ValueError(f"Timeline {name!r} contains a keyframe without a numeric time")

    def _change_path(self, _event: tk.Event[Any] | None = None) -> None:
        if self._suppress_path_event or self.data is None:
            return
        self.path_name = self.path_combo.get()
        self.selected = None
        self.problematic = None
        self.bulk_selected.clear()
        self.bulk_pick_mode = None
        self.bulk_start_button.configure(text="Select Start")
        self.bulk_end_button.configure(text="Select End")
        self._clear_editor("Select a keyframe to edit its properties.")
        self.redraw()

    @property
    def tracks(self) -> list[dict[str, Any]]:
        if self.data is None:
            return []
        return self.data.timelines[self.path_name]

    @property
    def time_frames(self) -> list[dict[str, Any]]:
        return self.tracks[0].get("keyframes", []) if self.tracks else []

    @property
    def position_frames(self) -> list[dict[str, Any]]:
        return self.tracks[1].get("keyframes", []) if self.tracks else []

    def _replay_duration(self) -> float:
        duration = self.data.metadata.get("duration", 0) if self.data else 0
        maximum = max((float(frame.get("properties", {}).get("timestamp", 0)) for frame in self.time_frames), default=0)
        return max(float(duration or 0), maximum, 1000.0)

    def _scale_for(self, kind: str) -> float:
        width = max(500, getattr(self, f"{kind}_canvas").winfo_width())
        zoom = getattr(self, f"{kind}_zoom")
        if kind == "replay":
            span_ms = self._replay_duration()
        else:
            span_ms = self._keyframe_duration()
        return (width / (span_ms / 1000.0)) * zoom

    def _world_width(self, kind: str) -> float:
        span = self._replay_duration() if kind == "replay" else self._keyframe_duration()
        return max(getattr(self, f"{kind}_canvas").winfo_width(), (span / 1000.0) * self._scale_for(kind))

    def _keyframe_duration(self) -> float:
        if not self.show_full_keyframe_timeline.get():
            return float(KEYFRAME_DURATION_MS)
        maximum = max(
            (float(frame.get("time", 0)) for frame in self.time_frames + self.position_frames),
            default=0.0,
        )
        return max(float(KEYFRAME_DURATION_MS), maximum)

    def _toggle_keyframe_range(self) -> None:
        self._update_keyframe_range_title()
        self.redraw()
        if self.selected is not None:
            self._scroll_keyframe_to(float(self.selected[1].get("time", 0)))
            self.redraw()

    def _update_keyframe_range_title(self) -> None:
        if not hasattr(self, "keyframe_section"):
            return
        if self.show_full_keyframe_timeline.get():
            title = f"Keyframe Timeline (full range: {self._format_time(self._keyframe_duration())})"
        else:
            title = "Keyframe Timeline (10 minutes)"
        self.keyframe_section.configure(text=title)

    def _ensure_selected_keyframe_in_range(self) -> None:
        if self.selected is None or self.show_full_keyframe_timeline.get():
            return
        if float(self.selected[1].get("time", 0)) > KEYFRAME_DURATION_MS:
            self.show_full_keyframe_timeline.set(True)
            self._update_keyframe_range_title()
            self.redraw()

    def _x_for(self, kind: str, milliseconds: float) -> float:
        return max(0.0, milliseconds / 1000.0 * self._scale_for(kind))

    def redraw(self) -> None:
        if not hasattr(self, "replay_canvas"):
            return
        for kind in ("replay", "keyframe"):
            canvas = getattr(self, f"{kind}_canvas")
            canvas.delete("all")
            canvas.configure(scrollregion=(0, 0, self._world_width(kind), canvas.winfo_height()))
        if self.data is None:
            self._show_empty_state()
            return
        self._update_keyframe_range_title()
        self._draw_replay()
        self._draw_keyframes()
        self._schedule_connector_update()
        if self.preview_enabled.get():
            self._ensure_preview_loaded()
            self._redraw_camera_preview()

    def _toggle_preview(self) -> None:
        if self.preview_enabled.get():
            if self.preview_window is None or not self.preview_window.winfo_exists():
                self.preview_window = tk.Toplevel(self)
                self.preview_window.title("Camera Preview")
                self.preview_window.geometry("720x440")
                self.preview_window.minsize(420, 280)
                self.preview_window.protocol("WM_DELETE_WINDOW", self._close_preview_window)
                controls = ttk.Frame(self.preview_window, padding=(8, 6, 8, 2))
                controls.pack(fill="x")
                ttk.Checkbutton(
                    controls,
                    text="Render entity models",
                    variable=self.render_entity_models,
                    command=self._redraw_camera_preview,
                ).pack(side="left")
                self.preview_canvas = tk.Canvas(
                    self.preview_window,
                    background="#10151c",
                    highlightthickness=0,
                )
                self.preview_canvas.pack(fill="both", expand=True, padx=8, pady=8)
                self.preview_canvas.bind("<Configure>", lambda _event: self._redraw_camera_preview())
            self.preview_window.deiconify()
            self.preview_window.lift()
            self.preview_error = ""
            self._ensure_preview_loaded()
            self._redraw_camera_preview()
        elif self.preview_window is not None and self.preview_window.winfo_exists():
            self.preview_window.withdraw()

    def _close_preview_window(self) -> None:
        self.preview_enabled.set(False)
        if self.preview_window is not None and self.preview_window.winfo_exists():
            self.preview_window.withdraw()

    def _ensure_preview_loaded(self) -> None:
        if self.data is None:
            return
        targets = self._preview_targets()
        target_signature = json.dumps(targets, separators=(",", ":"), sort_keys=True)
        if target_signature == self.preview_target_signature and (
            self.preview_data_loaded or self.preview_loading or self.preview_error
        ):
            return
        self.preview_load_token += 1
        self.preview_scenes = {}
        self.preview_entities_by_frame = {}
        self.preview_data_loaded = False
        self.preview_loading = False
        self.preview_target_signature = target_signature
        protocol = self.data.metadata.get("protocol", 767)
        if str(protocol) != "767":
            self.preview_error = f"This preview currently supports Minecraft 1.21 only (replay protocol {protocol})."
            self._redraw_camera_preview()
            return
        self.preview_loading = True
        self.preview_error = ""
        token = self.preview_load_token
        source = self.data.source

        def worker() -> None:
            try:
                preview_data = load_preview_data(source, targets)
                self.preview_queue.put((token, preview_data, ""))
            except Exception as exc:
                self.preview_queue.put((token, None, str(exc)))

        threading.Thread(target=worker, name="Replay preview index", daemon=True).start()
        self._redraw_camera_preview()

    def _poll_preview_results(self) -> None:
        try:
            while True:
                token, preview_data, error = self.preview_queue.get_nowait()
                if token != self.preview_load_token:
                    continue
                self.preview_loading = False
                if preview_data is None:
                    self.preview_error = error
                else:
                    self.preview_entities_by_frame = preview_data["entities"]
                    self.preview_scenes = preview_data["scenes"]
                    self.preview_data_loaded = True
                    self.preview_error = ""
                if self.preview_enabled.get():
                    self._redraw_camera_preview()
        except queue.Empty:
            pass
        self.after(100, self._poll_preview_results)

    def _preview_targets(self) -> list[dict[str, Any]]:
        targets: list[dict[str, Any]] = []
        for index, frame in enumerate(self.position_frames):
            props = frame.get("properties", {})
            position = props.get("camera:position", [0, 0, 0])
            rotation = props.get("camera:rotation", [0, 0, 0])
            try:
                camera_position = [float(position[i]) for i in range(3)]
                camera_rotation = [float(rotation[i]) for i in range(3)]
            except (IndexError, TypeError, ValueError):
                continue
            if not all(math.isfinite(value) for value in camera_position + camera_rotation):
                continue
            targets.append(
                {
                    "key": str(index),
                    "t": self._frame_replay_time("position", frame),
                    "position": camera_position,
                    "rotation": camera_rotation,
                }
            )
        return targets

    def _preview_camera(self) -> tuple[tuple[float, float, float], tuple[float, float, float], float, str] | None:
        if self.data is None or not self.position_frames:
            return None
        frame: dict[str, Any] | None = None
        timestamp = 0.0
        if self.selected is not None:
            kind, selected_frame = self.selected
            timestamp = self._frame_replay_time(kind, selected_frame)
            if kind == "position":
                frame = selected_frame
                frame_index = next((i for i, item in enumerate(self.position_frames) if item is frame), 0)
            else:
                frame_index = min(
                    range(len(self.position_frames)),
                    key=lambda i: abs(self._frame_replay_time("position", self.position_frames[i]) - timestamp),
                )
                frame = self.position_frames[frame_index]
        else:
            frame = self.position_frames[0]
            frame_index = 0
            timestamp = self._frame_replay_time("position", frame)
        props = frame.get("properties", {})
        position = props.get("camera:position", [0, 0, 0])
        rotation = props.get("camera:rotation", [0, 0, 0])
        if not isinstance(position, (list, tuple)) or not isinstance(rotation, (list, tuple)):
            return None
        try:
            camera_position = tuple(float(position[index]) for index in range(3))
            camera_rotation = tuple(float(rotation[index]) for index in range(3))
        except (IndexError, TypeError, ValueError):
            return None
        if not all(math.isfinite(value) for value in camera_position + camera_rotation):
            return None
        return camera_position, camera_rotation, timestamp, str(frame_index)

    @staticmethod
    def _humanoid_parts(entity_type: str) -> list[tuple[float, float, float, float, float, float, str]] | None:
        head = entity_type == "player"
        zombie = entity_type in {"zombie", "husk", "drowned", "zombie_villager"}
        skeleton = entity_type in {"skeleton", "stray", "wither_skeleton"}
        if not (head or zombie or skeleton):
            return None
        if skeleton:
            head_color, torso_color, arm_color, leg_color = "#ded7bc", "#c5bea5", "#c5bea5", "#b2ad9a"
            head_size = (0.34, 0.34, 0.34)
            torso_size = (0.26, 0.62, 0.16)
            arm_size = (0.10, 0.68, 0.10)
            leg_size = (0.11, 0.75, 0.11)
        elif zombie:
            head_color, torso_color, arm_color, leg_color = "#7c9b54", "#477c91", "#6e914e", "#4b3e7d"
            head_size = (0.46, 0.46, 0.46)
            torso_size = (0.42, 0.60, 0.22)
            arm_size = (0.16, 0.58, 0.16)
            leg_size = (0.19, 0.75, 0.19)
        else:
            head_color, torso_color, arm_color, leg_color = "#c89d77", "#38679b", "#c89d77", "#34465e"
            head_size = (0.50, 0.50, 0.50)
            torso_size = (0.40, 0.60, 0.22)
            arm_size = (0.15, 0.60, 0.15)
            leg_size = (0.18, 0.75, 0.18)
        return [
            (0, 1.55, 0, *head_size, head_color),
            (0, 1.05, 0, *torso_size, torso_color),
            (-0.285, 1.05, 0, *arm_size, arm_color),
            (0.285, 1.05, 0, *arm_size, arm_color),
            (-0.105, 0.375, 0, *leg_size, leg_color),
            (0.105, 0.375, 0, *leg_size, leg_color),
        ]

    def _draw_humanoid_model(
        self,
        canvas: tk.Canvas,
        camera_position: tuple[float, float, float],
        camera_rotation: tuple[float, float, float],
        entity: dict[str, Any],
        width: int,
        height: int,
    ) -> bool:
        parts = self._humanoid_parts(str(entity.get("type", "")))
        if parts is None:
            return False
        import math

        yaw = math.radians(float(entity.get("yaw", 0)))
        cos_yaw, sin_yaw = math.cos(yaw), math.sin(yaw)
        faces = (
            ((0, 1, 3, 2), 0.78),
            ((4, 6, 7, 5), 0.82),
            ((0, 4, 5, 1), 0.62),
            ((2, 3, 7, 6), 1.14),
            ((0, 2, 6, 4), 0.92),
            ((1, 5, 7, 3), 0.72),
        )
        polygons: list[tuple[float, list[float], str]] = []
        for center_x, center_y, center_z, size_x, size_y, size_z, color in parts:
            vertices: list[tuple[float, float, float] | None] = []
            for sx, sy, sz in (
                (-1, -1, -1), (-1, -1, 1), (-1, 1, -1), (-1, 1, 1),
                (1, -1, -1), (1, -1, 1), (1, 1, -1), (1, 1, 1),
            ):
                local_x = center_x + sx * size_x / 2
                local_y = center_y + sy * size_y / 2
                local_z = center_z + sz * size_z / 2
                world = (
                    float(entity["x"]) + cos_yaw * local_x - sin_yaw * local_z,
                    float(entity["y"]) + local_y,
                    float(entity["z"]) + sin_yaw * local_x + cos_yaw * local_z,
                )
                vertices.append(project_camera_point(camera_position, camera_rotation, world, width, height))
            for indices, shade in faces:
                face_vertices = [vertices[index] for index in indices]
                if any(vertex is None or abs(vertex[0]) > width * 8 or abs(vertex[1]) > height * 8 for vertex in face_vertices):
                    continue
                depth = sum(vertex[2] for vertex in face_vertices if vertex is not None) / 4
                points = [coordinate for vertex in face_vertices for coordinate in vertex[:2]]
                rgb = tuple(int(color[i : i + 2], 16) for i in (1, 3, 5))
                shaded = "#" + "".join(f"{max(0, min(255, round(channel * shade))):02x}" for channel in rgb)
                polygons.append((depth, points, shaded))
        for _depth, points, color in sorted(polygons, key=lambda polygon: polygon[0], reverse=True):
            canvas.create_polygon(*points, fill=color, outline="#20232a", width=1)
        return bool(polygons)

    def _redraw_camera_preview(self) -> None:
        canvas = self.preview_canvas
        if canvas is None or not canvas.winfo_exists() or not self.preview_enabled.get():
            return
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())

        if self.data is None:
            message = "Open a replay to preview its camera frame."
            canvas.create_text(width / 2, height / 2 + 28, text=message, fill="#d0d7e2")
            return
        if self.preview_loading:
            canvas.create_text(width / 2, height / 2 + 28, text="Indexing entities and rendering block frames…", fill="#d0d7e2")
            return
        if self.preview_error:
            canvas.create_text(
                width / 2,
                height / 2 + 28,
                text=f"Preview unavailable: {self.preview_error}",
                fill="#f0a5a5",
                width=max(200, width - 40),
                justify="center",
            )
            return
        camera = self._preview_camera()
        if camera is None:
            canvas.create_text(width / 2, height / 2 + 28, text="Select a position keyframe with camera data.", fill="#d0d7e2")
            return
        if not self.preview_data_loaded:
            canvas.create_text(width / 2, height / 2 + 28, text="Preparing preview…", fill="#d0d7e2")
            return

        camera_position, camera_rotation, timestamp, target_key = camera
        scene = self.preview_scenes.get(target_key)
        if scene is not None:
            try:
                import base64

                raw_rgb = base64.b64decode(scene["rgb"])
                image_width, image_height = int(scene["width"]), int(scene["height"])
                if len(raw_rgb) != image_width * image_height * 3:
                    raise ValueError("Invalid block frame size")
                source_image = tk.PhotoImage(width=image_width, height=image_height)
                for row in range(image_height):
                    start = row * image_width * 3
                    colors = " ".join(
                        f"#{raw_rgb[offset]:02x}{raw_rgb[offset + 1]:02x}{raw_rgb[offset + 2]:02x}"
                        for offset in range(start, start + image_width * 3, 3)
                    )
                    source_image.put("{" + colors + "}", to=(0, row))
                scale = max(1, min(width // int(scene["width"]), height // int(scene["height"])))
                self.preview_photo = source_image.zoom(scale, scale) if scale > 1 else source_image
                canvas.create_image(width / 2, height / 2, image=self.preview_photo)
            except (KeyError, ValueError, tk.TclError):
                self.preview_photo = None

        visible_players = 0
        visible_entities = 0
        entities_to_draw: list[tuple[float, dict[str, Any], tuple[float, float]]] = []
        for entity in self.preview_entities_by_frame.get(target_key, []):
            is_player = entity.get("kind") == "player"
            eye_offset = 0.9 if is_player else 0.45
            world_position = (float(entity["x"]), float(entity["y"]) + eye_offset, float(entity["z"]))
            projected = project_player(
                camera_position,
                camera_rotation,
                world_position,
                width,
                height,
            )
            if projected is None:
                continue
            projected_point = project_camera_point(camera_position, camera_rotation, world_position, width, height)
            depth = projected_point[2] if projected_point is not None else 0
            entities_to_draw.append((depth, entity, projected))
        for _depth, entity, (x, y) in sorted(entities_to_draw, key=lambda item: item[0], reverse=True):
            is_player = entity.get("kind") == "player"
            model_drawn = self.render_entity_models.get() and self._draw_humanoid_model(
                canvas, camera_position, camera_rotation, entity, width, height
            )
            if not model_drawn:
                color = "#68d6ff" if is_player else "#ffbd69"
                canvas.create_oval(x - 5, y - 5, x + 5, y + 5, fill=color, outline="#f7f7f7", width=1)
            name = str(entity.get("name") or entity.get("type") or "Entity")
            canvas.create_text(x + 9, y, anchor="w", text=name, fill="#e3f8ff", font=("Segoe UI", 9, "bold"))
            if is_player:
                visible_players += 1
            else:
                visible_entities += 1
        canvas.create_rectangle(1, 1, width - 1, height - 1, outline="#687484")
        canvas.create_line(width / 2 - 8, height / 2, width / 2 + 8, height / 2, fill="#536171")
        canvas.create_line(width / 2, height / 2 - 8, width / 2, height / 2 + 8, fill="#536171")
        canvas.create_text(
            10,
            10,
            anchor="nw",
            text=f"{self._format_time(timestamp)}  ·  {visible_players} players  ·  {visible_entities} entities  ·  FOV 70°",
            fill="#d0d7e2",
            font=("Segoe UI", 9),
        )

    def _draw_replay(self) -> None:
        canvas = self.replay_canvas
        width = self._world_width("replay")
        height = max(80, canvas.winfo_height())
        y = height // 2
        canvas.create_line(0, y, width, y, fill="#8a929e", width=2)
        duration = self._replay_duration()
        step = 1000 if duration < 120000 else 30000 if duration < 1800000 else 300000
        step = 1000 if duration < 60000 else step
        for tick in range(0, int(duration) + step, step):
            x = self._x_for("replay", tick)
            if x > width:
                continue
            canvas.create_line(x, y - 7, x, y + 7, fill="#9aa4b2")
            canvas.create_text(x + 3, y + 10, anchor="nw", fill="#aeb7c4", text=self._format_time(tick))
        for index, frame in enumerate(self.time_frames):
            timestamp = float(frame.get("properties", {}).get("timestamp", 0))
            x = self._x_for("replay", timestamp)
            if x > width:
                continue
            selected = self.selected is not None and self.selected[1] is frame
            color = "#fff06a" if selected else "#ff6060" if frame is self.problematic else "#88b5ff"
            canvas.create_line(x, y - 12, x, y + 12, fill=color, width=2 if selected or frame is self.problematic else 1)
            canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill=color, outline=color, tags=(f"replay-{index}",))
        if self.selected is not None and self.selected[0] == "position":
            selected_replay_x = self._x_for("replay", self._frame_replay_time("position", self.selected[1]))
            canvas.create_line(selected_replay_x, y - 12, selected_replay_x, y + 12, fill="#fff06a", width=2)
            canvas.create_oval(selected_replay_x - 3, y - 3, selected_replay_x + 3, y + 3, fill="#fff06a", outline="#fff06a")
        canvas.create_text(8, 5, anchor="nw", fill="#d9e0e9", text=f"0 — {self._format_time(duration)}")

    def _visible_keyframe_range(self) -> tuple[float, float]:
        canvas = self.keyframe_canvas
        world = max(1, self._world_width("keyframe"))
        left = canvas.canvasx(0)
        right = left + canvas.winfo_width()
        scale = self._scale_for("keyframe")
        return (left / scale * 1000.0, right / scale * 1000.0)

    def _draw_keyframes(self) -> None:
        canvas = self.keyframe_canvas
        width = self._world_width("keyframe")
        height = max(96, canvas.winfo_height())
        upper = 32
        lower = 75
        canvas.create_line(0, upper, width, upper, fill="#697381")
        canvas.create_line(0, lower, width, lower, fill="#697381")
        canvas.create_text(6, 7, anchor="nw", fill="#bac4d2", text="Position")
        canvas.create_text(6, 51, anchor="nw", fill="#bac4d2", text="Time")
        duration = self._keyframe_duration()
        tick_ms = 30000 if duration <= KEYFRAME_DURATION_MS else 120000
        for tick in range(0, int(duration) + 1, tick_ms):
            x = self._x_for("keyframe", tick)
            canvas.create_line(x, upper - 4, x, lower + 4, fill="#3b424c")
            canvas.create_text(x + 3, height - 16, anchor="nw", fill="#8e99a7", text=self._format_time(tick))

        low, high = self._visible_keyframe_range()
        visible: list[tuple[str, dict[str, Any]]] = []
        for kind, frames in (("position", self.position_frames), ("time", self.time_frames)):
            for frame in frames:
                t = float(frame.get("time", 0))
                if low <= t <= high:
                    visible.append((kind, frame))
        draw_connectors = len(visible) <= MAX_CONNECTORS or self.force_connectors.get()
        selected_ids = {id(frame) for _kind, frame in ([self.selected] if self.selected else [])}
        bulk_ids = self.bulk_selected
        for kind, frames in (("position", self.position_frames), ("time", self.time_frames)):
            y = upper if kind == "position" else lower
            symbol = "o" if kind == "position" else "I"
            for index, frame in enumerate(frames):
                t = float(frame.get("time", 0))
                if t < low - 10000 or t > high + 10000:
                    continue
                x = self._x_for("keyframe", t)
                chosen = id(frame) in selected_ids
                broken = frame is self.problematic
                color = "#fff06a" if chosen else "#ff5555" if broken else "#63dc72"
                if id(frame) in bulk_ids and not chosen and not broken:
                    color = "#54d9d0"
                canvas.create_text(x, y, text=symbol, fill=color, font=("Segoe UI", 17, "bold"), tags=(f"kf-{kind}-{index}",))
        if not draw_connectors:
            canvas.create_text(width - 8, 5, anchor="ne", fill="#d7a66b", text=f"{len(visible)} visible keyframes — connectors hidden")
        elif len(visible) > MAX_CONNECTORS:
            canvas.create_text(width - 8, 5, anchor="ne", fill="#d7a66b", text=f"Force-rendering {len(visible)} connectors")
        canvas.create_text(width - 8, height - 16, anchor="ne", fill="#9ca7b5", text=self._format_time(duration))

    def _frame_replay_time(self, kind: str, frame: dict[str, Any]) -> float:
        if kind == "time":
            return float(frame.get("properties", {}).get("timestamp", 0))
        # A position keyframe is associated with the replay time interpolated from
        # the adjacent Time Keyframes. This is also the rule used by ReplayMod paths.
        times = sorted(self.time_frames, key=lambda item: float(item.get("time", 0)))
        at = float(frame.get("time", 0))
        before = None
        after = None
        for item in times:
            t = float(item.get("time", 0))
            if t <= at:
                before = item
            if t >= at:
                after = item
                break
        if before is None:
            return float(times[0].get("properties", {}).get("timestamp", 0)) if times else 0.0
        t0 = float(before["time"])
        r0 = float(before.get("properties", {}).get("timestamp", 0))
        if after is None or after is before:
            return r0
        t1 = float(after["time"])
        r1 = float(after.get("properties", {}).get("timestamp", r0))
        return r0 if t1 == t0 else r0 + (at - t0) / (t1 - t0) * (r1 - r0)

    def zoom(self, kind: str, factor: float) -> None:
        canvas = getattr(self, f"{kind}_canvas")
        old_width = max(1.0, self._world_width(kind))
        center = canvas.canvasx(canvas.winfo_width() / 2) / old_width
        setattr(self, f"{kind}_zoom", min(100.0, max(1.0, getattr(self, f"{kind}_zoom") * factor)))
        self.redraw()
        self.update_idletasks()
        new_width = max(1.0, self._world_width(kind))
        canvas.xview_moveto(max(0, min(1, (center * new_width - canvas.winfo_width() / 2) / new_width)))
        self.redraw()

    def _wheel_zoom(self, kind: str, event: tk.Event[Any]) -> str:
        self.zoom(kind, 1.15 if event.delta > 0 else 1 / 1.15)
        return "break"

    def _timeline_click(self, kind: str, event: tk.Event[Any]) -> None:
        canvas = getattr(self, f"{kind}_canvas")
        canvas.focus_set()
        x = canvas.canvasx(event.x)
        if self.bulk_pick_mode is not None:
            if kind == "keyframe":
                target_ms = x / self._scale_for("keyframe") * 1000.0
                frames = self.position_frames if event.y < 52 else self.time_frames
                if not frames:
                    return
                frame = min(frames, key=lambda item: abs(float(item.get("time", 0)) - target_ms))
                tolerance_ms = 10 / self._scale_for("keyframe") * 1000
                if abs(float(frame.get("time", 0)) - target_ms) <= tolerance_ms:
                    self._set_bulk_endpoint(frame)
                else:
                    self.bulk_label.configure(text="Click directly on a keyframe marker to set this endpoint.")
                return
            if self.time_frames:
                timestamp = x / self._scale_for("replay") * 1000.0
                frame = min(
                    self.time_frames,
                    key=lambda item: abs(float(item.get("properties", {}).get("timestamp", 0)) - timestamp),
                )
                replay_keyframe_time = float(frame.get("properties", {}).get("timestamp", 0))
                if abs(replay_keyframe_time - timestamp) <= 10 / self._scale_for("replay") * 1000:
                    self._set_bulk_endpoint(frame)
                else:
                    self.bulk_label.configure(text="Click directly on a keyframe marker to set this endpoint.")
            return
        if kind == "keyframe":
            target_ms = x / self._scale_for(kind) * 1000.0
            y = event.y
            frames = self.position_frames if y < 52 else self.time_frames
            kkind = "position" if y < 52 else "time"
            if not frames:
                return
            frame = min(frames, key=lambda item: abs(float(item.get("time", 0)) - target_ms))
            tolerance_ms = 10 / self._scale_for("keyframe") * 1000
            if abs(float(frame.get("time", 0)) - target_ms) <= tolerance_ms:
                self._select_keyframe(kkind, frame)
            return
        timestamp = x / self._scale_for("replay") * 1000.0
        if not self.time_frames:
            return
        frame = min(self.time_frames, key=lambda item: abs(float(item.get("properties", {}).get("timestamp", 0)) - timestamp))
        self._select_keyframe("time", frame)

    def _center_timeline(self, kind: str, timestamp: float) -> None:
        canvas = getattr(self, f"{kind}_canvas")
        span = self._replay_duration() if kind == "replay" else self._keyframe_duration()
        base_width = max(500, canvas.winfo_width())
        viewport_width = canvas.winfo_width()
        fraction = max(0.0, min(1.0, timestamp / span))

        # At the fit-to-window zoom, a marker near either end cannot be centered
        # because there is not enough scrollable space. Zoom in just enough to
        # center it while preserving the current zoom when it is already sufficient.
        if 0.0 < fraction < 1.0 and viewport_width > 0:
            half_view = viewport_width / (2 * base_width)
            required_zoom = max(half_view / fraction, half_view / (1 - fraction)) * 1.02
            zoom = getattr(self, f"{kind}_zoom")
            if zoom < required_zoom:
                setattr(self, f"{kind}_zoom", min(100.0, required_zoom))
                self.redraw()

        width = max(1, self._world_width(kind))
        x = self._x_for(kind, timestamp)
        left = max(0.0, min(width - viewport_width, x - viewport_width / 2))
        canvas.xview_moveto(left / width)

    def _scroll_replay_to(self, timestamp: float) -> None:
        self._center_timeline("replay", timestamp)

    def _scroll_keyframe_to(self, timestamp: float) -> None:
        self._center_timeline("keyframe", timestamp)

    def _select_keyframe(self, kind: str, frame: dict[str, Any]) -> None:
        self.selected = (kind, frame)
        self._ensure_selected_keyframe_in_range()
        self._scroll_replay_to(self._frame_replay_time(kind, frame))
        self._scroll_keyframe_to(float(frame.get("time", 0)))
        self._populate_editor()
        self.redraw()

    def _clear_editor(self, message: str) -> None:
        for child in self.editor_content.winfo_children():
            child.destroy()
        ttk.Label(self.editor_content, text=message).pack(anchor="w")

    def _entry(self, parent: ttk.Frame, label: str, value: Any, key: str, column: int) -> None:
        ttk.Label(parent, text=label).grid(row=0, column=column * 2, padx=(0, 3), sticky="e")
        var = tk.StringVar(value=str(value))
        self._editor_vars[key] = var
        entry = ttk.Entry(parent, textvariable=var, width=12)
        entry.grid(row=0, column=column * 2 + 1, padx=(0, 10), sticky="w")

    def _populate_editor(self) -> None:
        self._clear_editor("")
        self._editor_vars.clear()
        if self.selected is None:
            return
        kind, frame = self.selected
        timeline_position = float(frame.get("time", 0))
        ttk.Label(
            self.editor_content,
            text=f"Keyframe Timeline position: {self._format_time(timeline_position)} ({timeline_position:,.0f} ms)",
        ).pack(anchor="w", pady=(0, 4))
        row = ttk.Frame(self.editor_content)
        row.pack(fill="x")
        self._entry(row, "Position (ms)", frame.get("time", 0), "time", 0)
        props = frame.setdefault("properties", {})
        if kind == "time":
            self._entry(row, "Replay timestamp (ms)", props.get("timestamp", 0), "timestamp", 1)
            prior = self._previous_time_frame(frame)
            previous_value = "—" if prior is None else f"{float(prior.get('properties', {}).get('timestamp', 0)):,.0f}"
            ttk.Label(row, text="Previous Replay timestamp (ms):").grid(row=0, column=4, padx=(0, 3), sticky="e")
            ttk.Label(row, text=previous_value, width=12).grid(row=0, column=5, padx=(0, 10), sticky="w")
        else:
            pos = list(props.get("camera:position", [0, 0, 0]))
            rot = list(props.get("camera:rotation", [0, 0, 0]))
            pos += [0] * (3 - len(pos))
            rot += [0] * (3 - len(rot))
            for i, name in enumerate(("X", "Y", "Z")):
                self._entry(row, name, pos[i], f"position_{i}", 1 + i)
            for i, name in enumerate(("Yaw", "Pitch", "Roll")):
                self._entry(row, name, rot[i], f"rotation_{i}", 4 + i)
        ttk.Button(row, text="Apply", command=self.apply_editor).grid(row=0, column=14, padx=(4, 4))
        ttk.Button(row, text=f"Delete {kind} keyframe", command=self.delete_selected).grid(row=0, column=15)

    def _previous_time_frame(self, frame: dict[str, Any]) -> dict[str, Any] | None:
        ordered = sorted(self.time_frames, key=lambda item: float(item.get("time", 0)))
        try:
            index = next(i for i, item in enumerate(ordered) if item is frame)
        except StopIteration:
            return None
        return ordered[index - 1] if index else None

    def apply_editor(self) -> None:
        if self.selected is None:
            return
        kind, frame = self.selected
        try:
            frame["time"] = self._number(self._editor_vars["time"].get())
            props = frame.setdefault("properties", {})
            if kind == "time":
                props["timestamp"] = self._number(self._editor_vars["timestamp"].get())
            else:
                props["camera:position"] = [self._number(self._editor_vars[f"position_{i}"].get()) for i in range(3)]
                props["camera:rotation"] = [self._number(self._editor_vars[f"rotation_{i}"].get()) for i in range(3)]
        except (KeyError, ValueError) as exc:
            messagebox.showerror("Invalid value", f"Enter a valid number for every field.\n{exc}", parent=self)
            return
        self._ensure_selected_keyframe_in_range()
        self._populate_editor()
        self.redraw()

    @staticmethod
    def _number(value: str) -> int | float:
        result = float(value.strip())
        if not math.isfinite(result):
            raise ValueError("Values must be finite numbers")
        return int(result) if result.is_integer() else result

    def delete_selected(self) -> None:
        if self.selected is None:
            return
        kind, frame = self.selected
        deleted_id = id(frame)
        self._remove_keyframes({deleted_id})
        self.selected = None
        self.problematic = None if self.problematic is frame else self.problematic
        self.bulk_selected.discard(deleted_id)
        self._clear_editor("Select a keyframe to edit its properties.")
        self.redraw()

    def _delete_key_event(self, event: tk.Event[Any]) -> str | None:
        if self.selected is None or isinstance(
            event.widget,
            (tk.Entry, tk.Text, ttk.Entry, ttk.Spinbox, ttk.Combobox),
        ):
            return None
        self.delete_selected()
        return "break"

    def _remove_keyframes(self, frame_ids: set[int]) -> int:
        deleted = 0
        for track in self.tracks[:2]:
            old_frames = track.get("keyframes", [])
            kept_indices = [index for index, frame in enumerate(old_frames) if id(frame) not in frame_ids]
            deleted += len(old_frames) - len(kept_indices)
            segments = track.get("segments")
            if isinstance(segments, list) and len(segments) == max(0, len(old_frames) - 1):
                track["segments"] = [segments[left] for left, _right in zip(kept_indices, kept_indices[1:])]
            track["keyframes"] = [old_frames[index] for index in kept_indices]
        return deleted

    def _begin_bulk_endpoint_pick(self, endpoint: str) -> None:
        if self.data is None:
            return
        self.bulk_pick_mode = endpoint
        self.bulk_selected.clear()
        self.bulk_start_button.configure(text="Click start…" if endpoint == "start" else "Select Start")
        self.bulk_end_button.configure(text="Click end…" if endpoint == "end" else "Select End")
        self.bulk_label.configure(text=f"Click a keyframe marker on the Keyframe Timeline to set the {endpoint}.")
        self.redraw()

    def _set_bulk_endpoint(self, frame: dict[str, Any]) -> None:
        if self.bulk_pick_mode is None:
            return
        endpoint = self.bulk_pick_mode
        value = float(frame.get("time", 0))
        variable = self.bulk_start if endpoint == "start" else self.bulk_end
        variable.set(f"{value:g}")
        self.bulk_pick_mode = None
        self.bulk_start_button.configure(text="Select Start")
        self.bulk_end_button.configure(text="Select End")
        self.bulk_label.configure(
            text=f"{endpoint.title()} set to {value:,.0f} ms. Set the other endpoint, then select the range."
        )

    def delete_bulk_selection(self) -> None:
        frames = self._bulk_frames()
        if not frames:
            self.bulk_label.configure(text="No keyframes selected to delete.")
            return
        deleted_ids = {id(frame) for frame in frames}
        deleted = self._remove_keyframes(deleted_ids)
        if self.selected is not None and id(self.selected[1]) in deleted_ids:
            self.selected = None
            self._clear_editor("Select a keyframe to edit its properties.")
        if self.problematic is not None and id(self.problematic) in deleted_ids:
            self.problematic = None
            self.validation_label.configure(text="")
        self.bulk_selected.clear()
        self.bulk_label.configure(text=f"Deleted {deleted} keyframes.")
        self.redraw()

    def find_backwards(self) -> None:
        ordered = sorted(self.time_frames, key=lambda item: float(item.get("time", 0)))
        previous: float | None = None
        self.problematic = None
        for frame in ordered:
            timestamp = float(frame.get("properties", {}).get("timestamp", 0))
            if previous is not None and timestamp < previous:
                self.problematic = frame
                self.validation_label.configure(
                    text=(
                        f"Backwards at {self._format_time(float(frame.get('time', 0)))}: "
                        f"{timestamp:,.0f} ms < previous {previous:,.0f} ms; "
                        f"set timestamp to at least {previous:,.0f} ms."
                    )
                )
                self._select_keyframe("time", frame)
                return
            previous = timestamp
        self.validation_label.configure(text="No backwards time keyframes found.")
        self.redraw()

    def select_bulk_range(self) -> None:
        self.bulk_pick_mode = None
        self.bulk_start_button.configure(text="Select Start")
        self.bulk_end_button.configure(text="Select End")
        try:
            start, end = sorted((float(self.bulk_start.get()), float(self.bulk_end.get())))
        except ValueError:
            messagebox.showerror("Invalid range", "Start and end must be numeric milliseconds.", parent=self)
            return
        frames = self.time_frames + self.position_frames
        self.bulk_selected = {id(frame) for frame in frames if start <= float(frame.get("time", 0)) <= end}
        self.bulk_label.configure(text=f"Selected {len(self.bulk_selected)} keyframes from {start:g} to {end:g} ms")
        self.redraw()

    def _bulk_frames(self) -> list[dict[str, Any]]:
        return [frame for frame in self.time_frames + self.position_frames if id(frame) in self.bulk_selected]

    def apply_shift(self) -> None:
        frames = self._bulk_frames()
        if not frames:
            return
        try:
            amount = float(self.shift_amount.get())
            if not math.isfinite(amount):
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid shift", "Shift must be a finite number of milliseconds.", parent=self)
            return
        for frame in frames:
            frame["time"] = self._number(str(float(frame["time"]) + amount))
        self._ensure_selected_keyframe_in_range()
        self._refresh_after_bulk()

    def apply_scale(self) -> None:
        frames = self._bulk_frames()
        if not frames:
            return
        try:
            factor = float(self.scale_factor.get())
            if not math.isfinite(factor) or factor < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid scale", "Scale must be a finite, non-negative number.", parent=self)
            return
        values = [float(frame["time"]) for frame in frames]
        start, end = min(values), max(values)
        pivot = {"start": start, "midpoint": (start + end) / 2, "end": end}[self.pivot.get()]
        for frame in frames:
            frame["time"] = self._number(str(pivot + (float(frame["time"]) - pivot) * factor))
        self._ensure_selected_keyframe_in_range()
        self._refresh_after_bulk()

    def _refresh_after_bulk(self) -> None:
        if self.selected is not None:
            self._populate_editor()
        self.redraw()

    def save_as(self) -> None:
        if self.data is None:
            messagebox.showinfo("Nothing to save", "Open a replay first.", parent=self)
            return
        is_archive = self.data.source_is_archive
        if is_archive:
            suggested = self.data.source.with_name(self.data.source.stem + "_edited.mcpr")
            destination = filedialog.asksaveasfilename(
                title="Save edited replay as", initialdir=str(self.data.source.parent), initialfile=suggested.name,
                defaultextension=".mcpr", filetypes=(("Replay recording", "*.mcpr"), ("ZIP archive", "*.zip")),
            )
            if not destination:
                return
            dest = Path(destination)
            if dest.resolve() == self.data.source.resolve():
                messagebox.showerror("Choose another file", "Save As must use a different file from the original.", parent=self)
                return
        else:
            parent = filedialog.askdirectory(title="Choose where to create the edited replay folder")
            if not parent:
                return
            dest = Path(parent) / f"{self.data.source.name}_edited"
            if dest.resolve() == self.data.source.resolve():
                messagebox.showerror("Choose another folder", "Save As must use a different folder from the original.", parent=self)
                return
            if dest.exists():
                messagebox.showerror("Folder already exists", f"Choose another parent folder; this output already exists:\n{dest}", parent=self)
                return
        try:
            _write_replay_as(self.data, dest)
        except (OSError, zipfile.BadZipFile) as exc:
            messagebox.showerror("Save failed", str(exc), parent=self)
            return
        messagebox.showinfo("Saved", f"Edited replay saved to:\n{dest}", parent=self)

    @staticmethod
    def _format_time(milliseconds: float) -> str:
        seconds = max(0, int(milliseconds // 1000))
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def main() -> None:
    app = TimelineEditor()
    app.mainloop()


if __name__ == "__main__":
    main()
