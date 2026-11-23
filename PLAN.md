# Replay Recording Timeline Editor — Implementation Plan

## Goal

Create a standalone Python GUI for inspecting, repairing, and saving ReplayMod timeline data from extracted replay folders and `.mcpr` (ZIP) recordings.

## Implementation

1. **GUI and file access**
   - Use Python's standard-library Tkinter for the interface.
   - Open either an extracted replay folder containing `timelines.json` or a `.mcpr`/`.zip` archive containing that file.
   - Save edits with **Save As** so the original recording is left untouched.
   - When saving archives, replace only `timelines.json` and preserve all other archive entries.

2. **ReplayMod timeline parsing**
   - Preserve the original JSON structure, unknown fields, path names, segments, and interpolator metadata.
   - Support named timeline paths. Each path contains time-keyframe and position-keyframe tracks.
   - Time keyframes use `time` for their position on the Keyframe Timeline and `properties.timestamp` for their Replay Timeline location.
   - Position keyframes use `time` for their Keyframe Timeline position and `properties.camera:position` / `properties.camera:rotation` for camera values.

3. **Timeline display and navigation**
   - Draw a Replay Timeline and a Keyframe Timeline with independent horizontal scrolling and zoom.
   - Show the Keyframe Timeline's 10-minute span, with position keyframes (`o`) on the upper half and time keyframes (`I`) on the lower half.
   - Draw thin red connectors from visible keyframes to their Replay Timeline timestamps when no more than 200 keyframes are visible.
   - Provide a force-render checkbox for connector lines above the default 200-keyframe limit.
   - Clicking a keyframe selects it and scrolls the Replay Timeline to its corresponding replay time.

4. **Editing and repair tools**
   - Edit and delete selected Time Keyframes; show the previous Time Keyframe timestamp read-only.
   - Edit and delete selected Position Keyframes, including all three position and rotation values.
   - Delete the selected keyframe with the keyboard Delete key, and delete a bulk-selected range.
   - Find and highlight the first Time Keyframe whose replay timestamp is earlier than its predecessor.
   - Set bulk range endpoints by choosing Select Start / Select End and clicking keyframe markers, then shift, scale, or delete the inclusive range. Scaling supports start, midpoint, or end pivots. Bulk position operations do not change replay timestamps.

5. **Verification**
   - Check Python syntax and run core checks against the supplied `timelines.json` example.
   - Confirm edits survive JSON serialization and that archive saving retains all entries other than the replaced `timelines.json`.

## Files

- `replay_fixer.py` — application and timeline editing logic.
- `PLAN.md` — this implementation plan.
