"""A small Tkinter editor for ReplayMod timelines.json files and .mcpr archives."""

from __future__ import annotations

import json
import math
import shutil
import tkinter as tk
import zipfile
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any


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
        self._suppress_path_event = False
        self._editor_vars: dict[str, tk.StringVar] = {}
        self._build_ui()
        self._show_empty_state()

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

        self.timeline_frame = ttk.Frame(self, padding=(8, 0, 8, 4))
        self.timeline_frame.pack(fill="both", expand=True)
        self._make_timeline(self.timeline_frame, "replay", "Replay Timeline")
        self._make_timeline(self.timeline_frame, "keyframe", "Keyframe Timeline (10 minutes)")

        bulk = ttk.LabelFrame(self, text="Bulk select / shift / scale keyframe positions", padding=7)
        bulk.pack(fill="x", padx=8, pady=(0, 5))
        ttk.Label(bulk, text="Start (ms)").grid(row=0, column=0, sticky="w")
        self.bulk_start = tk.StringVar(value="0")
        ttk.Entry(bulk, textvariable=self.bulk_start, width=11).grid(row=0, column=1, padx=(4, 10))
        ttk.Label(bulk, text="End (ms)").grid(row=0, column=2, sticky="w")
        self.bulk_end = tk.StringVar(value="0")
        ttk.Entry(bulk, textvariable=self.bulk_end, width=11).grid(row=0, column=3, padx=(4, 10))
        ttk.Button(bulk, text="Select range", command=self.select_bulk_range).grid(row=0, column=4, padx=(0, 12))
        ttk.Label(bulk, text="Shift by (ms)").grid(row=0, column=5, sticky="w")
        self.shift_amount = tk.StringVar(value="0")
        ttk.Entry(bulk, textvariable=self.shift_amount, width=9).grid(row=0, column=6, padx=(4, 6))
        ttk.Button(bulk, text="Shift", command=self.apply_shift).grid(row=0, column=7, padx=(0, 10))
        ttk.Label(bulk, text="Scale ×").grid(row=0, column=8, sticky="w")
        self.scale_factor = tk.StringVar(value="1.0")
        ttk.Entry(bulk, textvariable=self.scale_factor, width=8).grid(row=0, column=9, padx=(4, 6))
        self.pivot = tk.StringVar(value="midpoint")
        ttk.Combobox(bulk, textvariable=self.pivot, state="readonly", values=("start", "midpoint", "end"), width=10).grid(row=0, column=10, padx=(0, 6))
        ttk.Button(bulk, text="Scale", command=self.apply_scale).grid(row=0, column=11)
        self.bulk_label = ttk.Label(bulk, text="No keyframes selected")
        self.bulk_label.grid(row=1, column=0, columnspan=12, sticky="w", pady=(5, 0))

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
        canvas = tk.Canvas(section, height=88 if kind == "replay" else 104, background="#20242b", highlightthickness=0)
        canvas.pack(fill="x", expand=True, pady=(3, 0))
        scrollbar = ttk.Scrollbar(section, orient="horizontal", command=canvas.xview)
        scrollbar.pack(fill="x")
        canvas.configure(xscrollcommand=scrollbar.set)
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
        self.selected = None
        self.problematic = None
        self.bulk_selected.clear()
        self.file_label.configure(text=str(self.data.source))
        self.validation_label.configure(text="")
        self.bulk_label.configure(text="No keyframes selected")
        self._clear_editor("Select a keyframe to edit its properties.")
        for kind in ("replay", "keyframe"):
            setattr(self, f"{kind}_zoom", 1.0)
            getattr(self, f"{kind}_canvas").xview_moveto(0)
        self.redraw()

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
            span_ms = KEYFRAME_DURATION_MS
        return (width / (span_ms / 1000.0)) * zoom

    def _world_width(self, kind: str) -> float:
        span = self._replay_duration() if kind == "replay" else KEYFRAME_DURATION_MS
        return max(getattr(self, f"{kind}_canvas").winfo_width(), (span / 1000.0) * self._scale_for(kind))

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
        self._draw_replay()
        self._draw_keyframes()

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
        tick_ms = 30000
        for tick in range(0, KEYFRAME_DURATION_MS + 1, tick_ms):
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
        draw_connectors = len(visible) <= MAX_CONNECTORS
        if draw_connectors:
            for kind, frame in visible:
                replay_time = self._frame_replay_time(kind, frame)
                replay_x = self._x_for("replay", replay_time)
                key_x = self._x_for("keyframe", float(frame["time"]))
                # The connector terminates at the upper edge of this panel, directly
                # beneath the corresponding point in the independently scaled Replay Timeline.
                replay_left = self.replay_canvas.canvasx(0)
                keyframe_left = canvas.canvasx(0)
                replay_aligned_x = replay_x - replay_left + keyframe_left
                canvas.create_line(key_x, upper - 5, replay_aligned_x, 0, fill="#e55353", width=1)

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
        canvas.create_text(width - 8, height - 16, anchor="ne", fill="#9ca7b5", text="10:00")

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
        x = canvas.canvasx(event.x)
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

    def _scroll_replay_to(self, timestamp: float) -> None:
        canvas = self.replay_canvas
        width = max(1, self._world_width("replay"))
        x = self._x_for("replay", timestamp)
        canvas.xview_moveto(max(0, min(1, (x - canvas.winfo_width() / 2) / width)))

    def _scroll_keyframe_to(self, timestamp: float) -> None:
        canvas = self.keyframe_canvas
        width = max(1, self._world_width("keyframe"))
        x = self._x_for("keyframe", timestamp)
        canvas.xview_moveto(max(0, min(1, (x - canvas.winfo_width() / 2) / width)))

    def _select_keyframe(self, kind: str, frame: dict[str, Any]) -> None:
        self.selected = (kind, frame)
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
        row = ttk.Frame(self.editor_content)
        row.pack(fill="x")
        self._entry(row, "Keyframe time (ms)", frame.get("time", 0), "time", 0)
        props = frame.setdefault("properties", {})
        if kind == "time":
            self._entry(row, "Replay timestamp (ms)", props.get("timestamp", 0), "timestamp", 1)
            prior = self._previous_time_frame(frame)
            previous_value = "—" if prior is None else str(prior.get("properties", {}).get("timestamp", "—"))
            ttk.Label(row, text="Previous time (ms):").grid(row=0, column=4, padx=(0, 3), sticky="e")
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
        frames = self.time_frames if kind == "time" else self.position_frames
        frames.remove(frame)
        self.selected = None
        self.problematic = None if self.problematic is frame else self.problematic
        self.bulk_selected.discard(id(frame))
        self._clear_editor("Select a keyframe to edit its properties.")
        self.redraw()

    def find_backwards(self) -> None:
        ordered = sorted(self.time_frames, key=lambda item: float(item.get("time", 0)))
        previous: float | None = None
        self.problematic = None
        for frame in ordered:
            timestamp = float(frame.get("properties", {}).get("timestamp", 0))
            if previous is not None and timestamp < previous:
                self.problematic = frame
                self.validation_label.configure(text=f"First backwards timestamp: keyframe at {self._format_time(float(frame.get('time', 0)))}")
                self._select_keyframe("time", frame)
                return
            previous = timestamp
        self.validation_label.configure(text="No backwards time keyframes found.")
        self.redraw()

    def select_bulk_range(self) -> None:
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
