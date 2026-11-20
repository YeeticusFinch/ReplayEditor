# ReplayMod Timeline Fixer

A small interactive Python GUI for viewing and repairing ReplayMod timeline data in extracted replay folders or `.mcpr` ZIP archives.

## Run

Requires Python 3.10+ with Tkinter (included with standard Windows Python installations). No third-party packages are required.

```powershell
python replay_fixer.py
```

Use **Open Replay…** to choose an `.mcpr`/`.zip` file or **Open Folder…** to choose an extracted replay folder. Pick a named camera path from the Path menu when the replay has multiple paths.

## Features

- Independent zoom and horizontal scrolling for Replay and Keyframe Timelines.
- Position keyframes (`o`) and Time Keyframes (`I`) with connectors to replay time when at most 200 keyframes are visible.
- Click a keyframe to select/edit it; the replay view scrolls to the matching replay time.
- Find the first Time Keyframe that travels backwards.
- Select a keyframe-time range and shift or scale those keyframe positions around the start, midpoint, or end. This does not change replay timestamps.
- **Save As…** writes an edited copy and preserves the original. For `.mcpr` files, the archive's other entries are copied unchanged.

The bulk range inputs use milliseconds on the Keyframe Timeline. Time Keyframe replay timestamps are also edited in milliseconds.
When saving an extracted folder, choose a parent directory; the editor creates a new sibling folder with `_edited` appended to the replay folder name.
