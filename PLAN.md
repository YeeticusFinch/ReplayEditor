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

5. **Optional camera preview**
   - Provide a checkbox that opens/closes a separate camera-preview window.
   - Decode entity spawn, movement, teleport, and removal packets and reconstruct nearby block state from chunk and block-change packets in supplied Minecraft 1.21 (protocol 767) ReplayMod recordings.
   - Track entity state while scanning, then retain only entities visible at requested keyframes; render low-resolution block-face frames in a background worker and cache by replay and camera-track identity.
   - Project player and entity markers into the selected camera frame using a 70-degree FOV, and raycast block faces into a flat-shaded image.
   - Ignore common visual clutter, prefer player/custom entity names to type labels, and hide entities blocked by raycasted blocks.
   - Add a preview checkbox for simple projected player, zombie, and skeleton models, with markers remaining for other types.
   - Use Node.js `minecraft-protocol` and `prismarine-chunk` for version-specific packet and chunk decoding. Blocks use simplified colors/cube shapes rather than textures or detailed models.

6. **Verification**
   - Check Python syntax and run core checks against the supplied `timelines.json` example.
   - Confirm edits survive JSON serialization and that archive saving retains all entries other than the replaced `timelines.json`.

## Files

- `replay_fixer.py` — application and timeline editing logic.
- `replay_preview.py` / `replay_preview.js` — cached player event index and Minecraft 1.21 replay packet decoder.
- `package.json` / `package-lock.json` — pinned Node.js decoder dependencies for the optional preview.
- `PLAN.md` — this implementation plan.
