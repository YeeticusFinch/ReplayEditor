"use strict";

const fs = require("node:fs");
const path = require("node:path");
const yauzl = require("yauzl");
const { createDeserializer } = require("minecraft-protocol");

const VERSION = "1.21";
const registry = require("prismarine-registry")(VERSION);
const ChunkColumn = require("prismarine-chunk")(registry);
const PLAYER_TYPE = registry.entitiesByName.player.id;
const IMAGE_WIDTH = 160;
const IMAGE_HEIGHT = 90;
const FOV = 70;
const IGNORED_ENTITY_TYPES = new Set([
  "painting",
  "item",
  "item_frame",
  "glow_item_frame",
  "glowing_item_frame",
  "armor_stand",
  "arrow",
  "snowball",
  "enderpearl",
  "ender_pearl",
  "fireball",
  "wither_skull",
  "eye_of_ender",
  "eye_of_ender_signal",
]);

function replayStream(source) {
  if (fs.statSync(source).isDirectory()) {
    const recording = path.join(source, "recording.tmcpr");
    if (!fs.existsSync(recording)) throw new Error("No recording.tmcpr found in replay folder");
    return fs.createReadStream(recording);
  }

  return new Promise((resolve, reject) => {
    yauzl.open(source, { lazyEntries: true }, (error, archive) => {
      if (error) return reject(error);
      archive.readEntry();
      archive.on("entry", (entry) => {
        if (entry.fileName === "recording.tmcpr" || entry.fileName.endsWith("/recording.tmcpr")) {
          archive.openReadStream(entry, (streamError, stream) => {
            if (streamError) reject(streamError);
            else {
              stream.once("end", () => archive.close());
              resolve(stream);
            }
          });
        } else {
          archive.readEntry();
        }
      });
      archive.on("end", () => reject(new Error("No recording.tmcpr found in replay archive")));
      archive.on("error", reject);
    });
  });
}

function decoder(state) {
  return createDeserializer({ state, isServer: false, version: VERSION, noErrorLogging: true });
}

function parsePacket(buffer, currentState, decoders) {
  try {
    const result = decoders[currentState].parsePacketBuffer(buffer);
    return result.data;
  } catch (_error) {
    return null;
  }
}

function getEntityType(typeId) {
  const entity = registry.entities[typeId];
  return entity ? entity.name : `entity_${typeId}`;
}

function componentText(component) {
  if (component === null || component === undefined) return "";
  if (typeof component === "string" || typeof component === "number") return String(component);
  if (Array.isArray(component)) return component.map(componentText).join("");
  if (typeof component !== "object") return "";
  if (typeof component.type === "string" && Object.prototype.hasOwnProperty.call(component, "value")) {
    return componentText(component.value);
  }
  const ownText = component.text !== undefined
    ? componentText(component.text)
    : component.translate !== undefined
      ? componentText(component.translate)
      : component.key !== undefined
        ? componentText(component.key)
        : "";
  const extra = component.extra !== undefined ? componentText(component.extra) : "";
  if (ownText || extra) return ownText + extra;
  return component.value !== undefined ? componentText(component.value) : "";
}

function updateEntityName(entity) {
  entity.name = entity.customName || entity.profileName || entity.type;
}

function updateEntityMetadata(entity, metadata) {
  const type = registry.entities[entity.typeId];
  const metadataKeys = type && type.metadataKeys;
  if (!metadataKeys) return;
  const nameKey = metadataKeys.indexOf("custom_name");
  if (nameKey < 0) return;
  for (const value of metadata || []) {
    if (value.key === nameKey) {
      entity.customName = componentText(value.value).trim();
      updateEntityName(entity);
      return;
    }
  }
}

function entityYaw(yaw) {
  return ((Number(yaw || 0) & 0xff) * 360) / 256;
}

function blockColor(stateId, faceAxis, faceStep, distance) {
  const block = registry.blocksByStateId[stateId];
  const name = block ? block.name : "stone";
  let color;
  if (/water|bubble_column/.test(name)) color = [45, 105, 185];
  else if (/lava|fire|magma/.test(name)) color = [220, 92, 32];
  else if (/grass|leaves|moss|vine|cactus|sapling|flower|fern/.test(name)) color = [67, 139, 69];
  else if (/sand| sandstone|gravel|terracotta/.test(name)) color = [190, 166, 112];
  else if (/snow|ice|quartz|calcite|diorite/.test(name)) color = [188, 201, 203];
  else if (/log|wood|planks|fence|door|chest|barrel/.test(name)) color = [135, 94, 54];
  else if (/dirt|mud|clay|farmland/.test(name)) color = [125, 91, 63];
  else if (/deepslate|blackstone|obsidian|coal/.test(name)) color = [74, 73, 84];
  else if (/gold|copper|pumpkin|honey/.test(name)) color = [183, 130, 59];
  else if (/redstone|nether_wart|crimson/.test(name)) color = [157, 69, 66];
  else color = [133, 139, 146];

  let shade = faceAxis === 1 ? (faceStep < 0 ? 1.18 : 0.58) : faceAxis === 0 ? 0.82 : 0.72;
  shade *= Math.max(0.36, 1 - distance / 115);
  return color.map((channel) => Math.max(0, Math.min(255, Math.round(channel * shade))));
}

function visibleBlockColor(chunks, x, y, z, axis, step, distance) {
  if (y < -64 || y >= 320) return null;
  const chunkX = Math.floor(x / 16);
  const chunkZ = Math.floor(z / 16);
  const chunk = chunks.get(`${chunkX},${chunkZ}`);
  if (!chunk) return null;
  const stateId = chunk.getBlockStateId({ x: x - chunkX * 16, y, z: z - chunkZ * 16 });
  if (stateId === 0) return null;
  const state = registry.blocksByStateId[stateId];
  if (!state || state.boundingBox === "empty") return null;
  return blockColor(stateId, axis, step, distance);
}

function raycast(chunks, origin, direction, maxDistance, wantColor = true) {
  let x = Math.floor(origin[0]);
  let y = Math.floor(origin[1]);
  let z = Math.floor(origin[2]);
  const stepX = Math.sign(direction[0]);
  const stepY = Math.sign(direction[1]);
  const stepZ = Math.sign(direction[2]);
  const deltaX = stepX === 0 ? Infinity : Math.abs(1 / direction[0]);
  const deltaY = stepY === 0 ? Infinity : Math.abs(1 / direction[1]);
  const deltaZ = stepZ === 0 ? Infinity : Math.abs(1 / direction[2]);
  let maxX = stepX === 0 ? Infinity : ((stepX > 0 ? x + 1 : x) - origin[0]) / direction[0];
  let maxY = stepY === 0 ? Infinity : ((stepY > 0 ? y + 1 : y) - origin[1]) / direction[1];
  let maxZ = stepZ === 0 ? Infinity : ((stepZ > 0 ? z + 1 : z) - origin[2]) / direction[2];
  let distance = 0;
  let axis = 1;
  let step = -1;

  while (distance < maxDistance) {
    if (maxX <= maxY && maxX <= maxZ) {
      distance = maxX;
      maxX += deltaX;
      x += stepX;
      axis = 0;
      step = stepX;
    } else if (maxY <= maxZ) {
      distance = maxY;
      maxY += deltaY;
      y += stepY;
      axis = 1;
      step = stepY;
    } else {
      distance = maxZ;
      maxZ += deltaZ;
      z += stepZ;
      axis = 2;
      step = stepZ;
    }
    const color = visibleBlockColor(chunks, x, y, z, axis, step, distance);
    if (color) return wantColor ? { color, distance } : { distance };
  }
  return null;
}

function renderBlockFrame(chunks, target) {
  const yaw = target.rotation[0] * Math.PI / 180;
  const pitch = target.rotation[1] * Math.PI / 180;
  const roll = target.rotation[2] * Math.PI / 180;
  const forward = [-Math.sin(yaw) * Math.cos(pitch), -Math.sin(pitch), Math.cos(yaw) * Math.cos(pitch)];
  const right0 = [Math.cos(yaw), 0, Math.sin(yaw)];
  const up0 = [-Math.sin(yaw) * Math.sin(pitch), Math.cos(pitch), Math.cos(yaw) * Math.sin(pitch)];
  const right = right0.map((value, i) => value * Math.cos(roll) + up0[i] * Math.sin(roll));
  const up = up0.map((value, i) => value * Math.cos(roll) - right0[i] * Math.sin(roll));
  const halfFovX = Math.tan(FOV * Math.PI / 360);
  const halfFovY = halfFovX * IMAGE_HEIGHT / IMAGE_WIDTH;
  const image = Buffer.alloc(IMAGE_WIDTH * IMAGE_HEIGHT * 3);

  for (let py = 0; py < IMAGE_HEIGHT; py++) {
    for (let px = 0; px < IMAGE_WIDTH; px++) {
      const nx = (2 * (px + 0.5) / IMAGE_WIDTH - 1) * halfFovX;
      const ny = (1 - 2 * (py + 0.5) / IMAGE_HEIGHT) * halfFovY;
      const direction = forward.map((value, i) => value + nx * right[i] + ny * up[i]);
      const length = Math.hypot(...direction);
      for (let i = 0; i < 3; i++) direction[i] /= length;
      const hit = raycast(chunks, target.position, direction, 64);
      const color = hit ? hit.color : [100, 153, 205];
      const offset = (py * IMAGE_WIDTH + px) * 3;
      image[offset] = color[0];
      image[offset + 1] = color[1];
      image[offset + 2] = color[2];
    }
  }
  return image.toString("base64");
}

function entityIsInFrame(chunks, target, entity) {
  const yaw = target.rotation[0] * Math.PI / 180;
  const pitch = target.rotation[1] * Math.PI / 180;
  const roll = target.rotation[2] * Math.PI / 180;
  const dx = entity.x - target.position[0];
  const dy = entity.y + (entity.kind === "player" ? 0.9 : 0.45) - target.position[1];
  const dz = entity.z - target.position[2];
  const depth = -dx * Math.sin(yaw) * Math.cos(pitch) - dy * Math.sin(pitch) + dz * Math.cos(yaw) * Math.cos(pitch);
  if (depth <= 0.05) return false;
  const unrolledRight = dx * Math.cos(yaw) + dz * Math.sin(yaw);
  const unrolledUp = -dx * Math.sin(yaw) * Math.sin(pitch) + dy * Math.cos(pitch) + dz * Math.cos(yaw) * Math.sin(pitch);
  const right = unrolledRight * Math.cos(roll) + unrolledUp * Math.sin(roll);
  const up = unrolledUp * Math.cos(roll) - unrolledRight * Math.sin(roll);
  const halfWidth = Math.tan(FOV * Math.PI / 360) * 1.05;
  const halfHeight = halfWidth * IMAGE_HEIGHT / IMAGE_WIDTH * 1.05;
  if (Math.abs(right / depth) > halfWidth || Math.abs(up / depth) > halfHeight) return false;

  const distance = Math.hypot(dx, dy, dz);
  if (distance > 0.3) {
    const direction = [dx / distance, dy / distance, dz / distance];
    if (raycast(chunks, target.position, direction, distance - 0.2, false)) return false;
  }
  return true;
}

function snapshotTargets(chunks, entities, targets, targetIndex, timestamp, scenes, entitiesByTarget) {
  while (targetIndex < targets.length && targets[targetIndex].t < timestamp) {
    const target = targets[targetIndex];
    scenes[target.key] = { width: IMAGE_WIDTH, height: IMAGE_HEIGHT, rgb: renderBlockFrame(chunks, target) };
    entitiesByTarget[target.key] = [...entities.values()]
      .filter((entity) => entityIsInFrame(chunks, target, entity))
      .map((entity) => ({ ...entity }));
    targetIndex++;
  }
  return targetIndex;
}

async function scan(source) {
  const targets = process.stdin.isTTY ? [] : JSON.parse(fs.readFileSync(0, "utf8") || "[]");
  targets.sort((a, b) => a.t - b.t);
  const decoders = {
    login: decoder("login"),
    configuration: decoder("configuration"),
    play: decoder("play"),
  };
  const entitiesById = new Map();
  const playersByUuid = new Map();
  const chunks = new Map();
  const scenes = {};
  const entitiesByTarget = {};
  let state = "login";
  let carry = Buffer.alloc(0);
  let packetsRead = 0;
  let targetIndex = 0;

  for await (const chunk of await replayStream(source)) {
    carry = carry.length ? Buffer.concat([carry, chunk]) : chunk;
    let offset = 0;
    while (carry.length - offset >= 8) {
      const timestamp = carry.readInt32BE(offset);
      const length = carry.readInt32BE(offset + 4);
      if (length < 0 || length > 16 * 1024 * 1024) throw new Error(`Invalid replay packet length: ${length}`);
      if (carry.length - offset < 8 + length) break;
      targetIndex = snapshotTargets(chunks, entitiesById, targets, targetIndex, timestamp, scenes, entitiesByTarget);
      const packet = parsePacket(carry.subarray(offset + 8, offset + 8 + length), state, decoders);
      offset += 8 + length;
      packetsRead += 1;
      if (!packet) continue;

      if (state === "login") {
        if (packet.name === "success") state = "configuration";
        continue;
      }
      if (state === "configuration") {
        if (packet.name === "finish_configuration") state = "play";
        continue;
      }

      const data = packet.params || {};
      if (packet.name === "player_info") {
        for (const entry of data.data || []) {
          const uuid = String(entry.uuid).toLowerCase();
          const name = entry.player && entry.player.name;
          if (name) {
            playersByUuid.set(uuid, name);
            for (const entity of entitiesById.values()) {
              if (entity.uuid === uuid) {
                entity.profileName = name;
                updateEntityName(entity);
              }
            }
          }
        }
      } else if (packet.name === "map_chunk") {
        try {
          const chunk = new ChunkColumn({ minY: -64, worldHeight: 384 });
          chunk.load(data.chunkData);
          chunks.set(`${data.x},${data.z}`, chunk);
        } catch (_error) {
          // A malformed or unsupported chunk should not prevent entity previews.
        }
      } else if (packet.name === "unload_chunk") {
        chunks.delete(`${data.chunkX},${data.chunkZ}`);
      } else if (packet.name === "block_change") {
        const location = data.location;
        if (location) {
          const chunkX = Math.floor(location.x / 16);
          const chunkZ = Math.floor(location.z / 16);
          const chunk = chunks.get(`${chunkX},${chunkZ}`);
          if (chunk) chunk.setBlockStateId({ x: location.x - chunkX * 16, y: location.y, z: location.z - chunkZ * 16 }, data.type);
        }
      } else if (packet.name === "multi_block_change") {
        const section = data.chunkCoordinates;
        for (const record of data.records || []) {
          const stateId = record >>> 12;
          const localX = (record >>> 8) & 15;
          const localZ = (record >>> 4) & 15;
          const localY = record & 15;
          const worldX = section.x * 16 + localX;
          const worldY = section.y * 16 + localY;
          const worldZ = section.z * 16 + localZ;
          const chunk = chunks.get(`${section.x},${section.z}`);
          if (chunk) chunk.setBlockStateId({ x: localX, y: worldY, z: localZ }, stateId);
        }
      } else if (packet.name === "respawn") {
        chunks.clear();
        entitiesById.clear();
      } else if (packet.name === "entity_metadata") {
        const entity = entitiesById.get(data.entityId);
        if (entity) updateEntityMetadata(entity, data.metadata);
      } else if (packet.name === "spawn_entity" || packet.name === "spawn_entity_experience_orb") {
        const uuid = data.objectUUID ? String(data.objectUUID).toLowerCase() : "";
        const type = packet.name === "spawn_entity_experience_orb" ? "experience_orb" : getEntityType(data.type);
        if (IGNORED_ENTITY_TYPES.has(type.toLowerCase())) continue;
        const entity = {
          uuid,
          typeId: data.type,
          kind: data.type === PLAYER_TYPE ? "player" : "entity",
          type,
          profileName: data.type === PLAYER_TYPE ? playersByUuid.get(uuid) || "" : "",
          customName: "",
          name: "",
          x: data.x,
          y: data.y,
          z: data.z,
          yaw: entityYaw(data.yaw),
        };
        updateEntityName(entity);
        entitiesById.set(data.entityId, entity);
      } else if (["rel_entity_move", "entity_move_look", "entity_teleport"].includes(packet.name)) {
        const entity = entitiesById.get(data.entityId);
        if (!entity) continue;
        if (packet.name === "entity_teleport") {
          entity.x = data.x;
          entity.y = data.y;
          entity.z = data.z;
        } else {
          entity.x += data.dX / 4096;
          entity.y += data.dY / 4096;
          entity.z += data.dZ / 4096;
        }
        if (packet.name !== "rel_entity_move" && data.yaw !== undefined) entity.yaw = entityYaw(data.yaw);
      } else if (packet.name === "entity_destroy") {
        for (const id of data.entityIds || []) {
          entitiesById.delete(id);
        }
      }
    }
    carry = carry.subarray(offset);
  }

  if (carry.length !== 0) throw new Error("Replay packet stream ended unexpectedly");
  while (targetIndex < targets.length) {
    const target = targets[targetIndex++];
    scenes[target.key] = { width: IMAGE_WIDTH, height: IMAGE_HEIGHT, rgb: renderBlockFrame(chunks, target) };
    entitiesByTarget[target.key] = [...entitiesById.values()]
          .filter((entity) => entityIsInFrame(chunks, target, entity))
      .map((entity) => ({ ...entity }));
  }
  process.stderr.write(`Decoded ${packetsRead.toLocaleString()} replay packets; rendered ${Object.keys(scenes).length} block frames.\n`);
  process.stdout.write(JSON.stringify({ entities: entitiesByTarget, scenes }));
}

scan(process.argv[2]).catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
