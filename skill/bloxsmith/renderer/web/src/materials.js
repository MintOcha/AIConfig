// Approximations of Roblox's part materials: a procedural detail texture (tiled in
// studs, multiplied by the part's Color) plus roughness and metalness in the same
// spirit as Roblox's PBR materials. Close enough to read a scene; not Roblox's art.

import * as THREE from 'three';

const SIZE = 256;

function rng(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// Smooth value noise on a periodic grid, so textures tile.
function noiseField(random, cells) {
  const grid = [];
  for (let i = 0; i < cells * cells; i++) grid.push(random());
  const at = (x, y) => grid[(((y % cells) + cells) % cells) * cells + (((x % cells) + cells) % cells)];
  return (u, v) => {
    const x = u * cells;
    const y = v * cells;
    const x0 = Math.floor(x);
    const y0 = Math.floor(y);
    const fx = x - x0;
    const fy = y - y0;
    const sx = fx * fx * (3 - 2 * fx);
    const sy = fy * fy * (3 - 2 * fy);
    const a = at(x0, y0) + (at(x0 + 1, y0) - at(x0, y0)) * sx;
    const b = at(x0, y0 + 1) + (at(x0 + 1, y0 + 1) - at(x0, y0 + 1)) * sx;
    return a + (b - a) * sy;
  };
}

function canvasTexture(draw) {
  const canvas = document.createElement('canvas');
  canvas.width = SIZE;
  canvas.height = SIZE;
  const ctx = canvas.getContext('2d');
  const image = ctx.createImageData(SIZE, SIZE);
  for (let y = 0; y < SIZE; y++) {
    for (let x = 0; x < SIZE; x++) {
      const value = Math.max(0, Math.min(1, draw(x / SIZE, y / SIZE)));
      const i = (y * SIZE + x) * 4;
      image.data[i] = image.data[i + 1] = image.data[i + 2] = Math.round(value * 255);
      image.data[i + 3] = 255;
    }
  }
  ctx.putImageData(image, 0, 0);
  const texture = new THREE.CanvasTexture(canvas);
  texture.wrapS = texture.wrapT = THREE.RepeatWrapping;
  texture.colorSpace = THREE.NoColorSpace;
  texture.anisotropy = 4;
  return texture;
}

// name -> { studs per tile, roughness, metalness, detail(u, v) in 0..1 }
function recipes() {
  const r = rng(7);
  const fine = noiseField(r, 64);
  const mid = noiseField(r, 16);
  const coarse = noiseField(r, 6);
  const grain = noiseField(r, 32);
  return {
    SmoothPlastic: { studs: 8, roughness: 0.42, metalness: 0, detail: () => 1 },
    Plastic: { studs: 4, roughness: 0.55, metalness: 0, detail: (u, v) => 0.94 + 0.06 * fine(u, v) },
    Wood: {
      studs: 6,
      roughness: 0.7,
      metalness: 0,
      detail: (u, v) =>
        0.78 + 0.22 * (0.5 + 0.5 * Math.sin((v * 24 + mid(u, v) * 6) * Math.PI)) * (0.8 + 0.2 * fine(u, v)),
    },
    WoodPlanks: {
      studs: 6,
      roughness: 0.75,
      metalness: 0,
      detail: (u, v) => {
        const planks = 5;
        const row = Math.floor(v * planks);
        const fv = v * planks - row;
        const seamV = fv < 0.035 || fv > 0.965 ? 0.45 : 1;
        const shift = (row * 0.37) % 1;
        const fu = (u + shift) % 1;
        const seamU = fu < 0.012 ? 0.5 : 1;
        const tone = 0.82 + 0.18 * rng(row + 11)();
        const lines = 0.86 + 0.14 * (0.5 + 0.5 * Math.sin((fv * 9 + grain(u * 0.5, v) * 3) * Math.PI));
        return tone * lines * seamV * seamU;
      },
    },
    Slate: {
      studs: 8,
      roughness: 0.85,
      metalness: 0,
      detail: (u, v) => 0.7 + 0.3 * (0.6 * mid(u, v) + 0.4 * fine(u, v)),
    },
    Concrete: {
      studs: 8,
      roughness: 0.92,
      metalness: 0,
      detail: (u, v) => 0.8 + 0.2 * (0.5 * fine(u, v) + 0.5 * mid(u, v)),
    },
    Rock: {
      studs: 10,
      roughness: 0.95,
      metalness: 0,
      detail: (u, v) => 0.6 + 0.4 * (0.6 * coarse(u, v) + 0.4 * fine(u, v)),
    },
    Brick: {
      studs: 8,
      roughness: 0.9,
      metalness: 0,
      detail: (u, v) => {
        const rows = 8;
        const row = Math.floor(v * rows);
        const fv = v * rows - row;
        const fu = (u * 4 + (row % 2) * 0.5) % 1;
        const mortar = fv < 0.08 || fu < 0.04 ? 0.55 : 1;
        return mortar * (0.85 + 0.15 * fine(u, v));
      },
    },
    Metal: { studs: 8, roughness: 0.38, metalness: 0.75, detail: (u, v) => 0.85 + 0.15 * fine(u * 0.2, v) },
    CorrodedMetal: { studs: 8, roughness: 0.8, metalness: 0.4, detail: (u, v) => 0.6 + 0.4 * mid(u, v) },
    DiamondPlate: {
      studs: 4,
      roughness: 0.35,
      metalness: 0.8,
      detail: (u, v) => {
        const a = Math.abs(((u * 8 + v * 8) % 1) - 0.5);
        const b = Math.abs(((u * 8 - v * 8 + 16) % 1) - 0.5);
        return 0.8 + (a < 0.08 && b > 0.3 ? 0.25 : 0);
      },
    },
    Fabric: {
      studs: 3,
      roughness: 0.95,
      metalness: 0,
      detail: (u, v) => 0.82 + 0.1 * Math.sin(u * 160) * Math.sin(v * 160) + 0.08 * fine(u, v),
    },
    Grass: { studs: 8, roughness: 0.95, metalness: 0, detail: (u, v) => 0.7 + 0.3 * fine(u, v) },
    Sand: { studs: 8, roughness: 0.95, metalness: 0, detail: (u, v) => 0.85 + 0.15 * fine(u, v) },
    Pebble: { studs: 6, roughness: 0.9, metalness: 0, detail: (u, v) => 0.65 + 0.35 * mid(u * 2, v * 2) },
    Cobblestone: { studs: 6, roughness: 0.9, metalness: 0, detail: (u, v) => 0.6 + 0.4 * mid(u * 2, v * 2) },
    Marble: { studs: 10, roughness: 0.25, metalness: 0, detail: (u, v) => 0.85 + 0.15 * coarse(u, v) },
    Granite: { studs: 8, roughness: 0.6, metalness: 0, detail: (u, v) => 0.75 + 0.25 * fine(u, v) },
    Ice: { studs: 10, roughness: 0.15, metalness: 0, detail: (u, v) => 0.9 + 0.1 * coarse(u, v) },
    Foil: { studs: 4, roughness: 0.25, metalness: 0.9, detail: (u, v) => 0.8 + 0.2 * fine(u, v) },
    Glass: { studs: 8, roughness: 0.05, metalness: 0, detail: () => 1 },
    Neon: { studs: 8, roughness: 1, metalness: 0, detail: () => 1 },
    ForceField: { studs: 8, roughness: 1, metalness: 0, detail: () => 1 },
  };
}

let cache = null;

function recipe(name) {
  cache ??= recipes();
  return cache[name] ?? cache.Plastic;
}

const textures = new Map();

function detailTexture(name) {
  if (!textures.has(name)) {
    const r = recipe(name);
    textures.set(
      name,
      name === 'SmoothPlastic' || name === 'Glass' || name === 'Neon' ? null : canvasTexture(r.detail),
    );
  }
  return textures.get(name);
}

export function studsPerTile(name) {
  return recipe(name).studs;
}

export function srgb(color) {
  return new THREE.Color().setRGB(color[0], color[1], color[2], THREE.SRGBColorSpace);
}

// A three.js material for a part.
export function partMaterial({ material, color, transparency = 0, reflectance = 0, envMap = null }) {
  const name = material ?? 'Plastic';
  const opacity = 1 - transparency;
  if (name === 'Neon') {
    // Neon ignores lighting and glows; the bloom pass picks up values above 1.
    const glow = srgb(color).multiplyScalar(2.2);
    return new THREE.MeshBasicMaterial({
      color: glow,
      transparent: opacity < 1,
      opacity,
      toneMapped: true,
    });
  }
  const r = recipe(name);
  const map = detailTexture(name);
  const params = {
    color: srgb(color),
    map,
    bumpMap: map,
    bumpScale: map ? 0.6 : 0,
    roughness: Math.max(0.03, r.roughness * (1 - reflectance * 0.8)),
    metalness: Math.min(1, r.metalness + reflectance * 0.5),
    envMap,
    envMapIntensity: 0.6 + reflectance * 2,
    transparent: opacity < 1,
    opacity,
  };
  if (name === 'Glass') {
    return new THREE.MeshPhysicalMaterial({
      ...params,
      transparent: true,
      opacity: Math.min(opacity, 0.75),
      roughness: 0.04,
      metalness: 0,
      clearcoat: 1,
      envMapIntensity: 1.2 + reflectance * 2,
      depthWrite: opacity > 0.5,
    });
  }
  return new THREE.MeshStandardMaterial(params);
}
