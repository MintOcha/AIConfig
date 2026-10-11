// A still of every enabled ParticleEmitter: particles are replayed from the emitter's
// own properties (Rate, Lifetime, Speed, SpreadAngle, Acceleration, Drag, and the
// Size / Transparency / Color curves) as if it had been running for a while, then
// drawn as camera-facing sprites.

import * as THREE from 'three';
import { srgb } from './materials.js';

const NORMALS = {
  Top: [0, 1, 0],
  Bottom: [0, -1, 0],
  Front: [0, 0, -1],
  Back: [0, 0, 1],
  Right: [1, 0, 0],
  Left: [-1, 0, 0],
};

function rng(seed) {
  let s = (seed * 2654435761) >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function sample(keys, t, fallback) {
  if (!keys || keys.length === 0) return fallback;
  if (t <= keys[0][0]) return keys[0][1];
  for (let i = 1; i < keys.length; i++) {
    if (t <= keys[i][0]) {
      const [t0, v0] = keys[i - 1];
      const [t1, v1] = keys[i];
      const f = (t - t0) / Math.max(t1 - t0, 1e-6);
      if (Array.isArray(v0)) return v0.map((c, j) => c + (v1[j] - c) * f);
      return v0 + (v1 - v0) * f;
    }
  }
  return keys[keys.length - 1][1];
}

function softDot() {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 64;
  const ctx = canvas.getContext('2d');
  const g = ctx.createRadialGradient(32, 32, 0, 32, 32, 32);
  g.addColorStop(0, 'rgba(255,255,255,1)');
  g.addColorStop(0.5, 'rgba(255,255,255,0.55)');
  g.addColorStop(1, 'rgba(255,255,255,0)');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, 64, 64);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

let fallback = null;

// `emitters`: [{ node, world: THREE.Matrix4 (the attachment or part frame) }]
export function buildParticles(group, emitters, textureFor) {
  fallback ??= softDot();
  for (const { node, world } of emitters) {
    const p = node.props;
    if (p.Enabled === false || !(p.Rate > 0)) continue;
    const random = rng(node.id);
    const [lifeMin, lifeMax] = p.Lifetime ?? [5, 10];
    const [speedMin, speedMax] = p.Speed ?? [5, 5];
    const spread = p.SpreadAngle ?? [0, 0];
    const accel = new THREE.Vector3(...(p.Acceleration ?? [0, 0, 0]));
    const drag = p.Drag ?? 0;
    const texture = textureFor(p.Texture) ?? fallback;
    const additive = (p.LightEmission ?? 0) >= 0.5;
    const normal = new THREE.Vector3(...(NORMALS[p.EmissionDirection] ?? NORMALS.Top)).transformDirection(world);
    const origin = new THREE.Vector3().setFromMatrixPosition(world);
    const count = Math.min(240, Math.floor(p.Rate * lifeMax));
    for (let i = 0; i < count; i++) {
      const age = (i + random()) / p.Rate;
      const life = lifeMin + (lifeMax - lifeMin) * random();
      if (age > life) continue;
      const t = age / life;
      // Direction within the spread cone around the emission normal.
      const yaw = ((random() * 2 - 1) * spread[0] * Math.PI) / 180;
      const pitch = ((random() * 2 - 1) * spread[1] * Math.PI) / 180;
      const tangent = Math.abs(normal.y) > 0.9 ? new THREE.Vector3(1, 0, 0) : new THREE.Vector3(0, 1, 0);
      const side = new THREE.Vector3().crossVectors(normal, tangent).normalize();
      const up = new THREE.Vector3().crossVectors(side, normal).normalize();
      const dir = normal.clone().applyAxisAngle(up, yaw).applyAxisAngle(side, pitch).normalize();
      const speed = speedMin + (speedMax - speedMin) * random();
      const travel = drag > 0 ? (speed * (1 - Math.exp(-drag * age))) / drag : speed * age;
      const position = origin
        .clone()
        .addScaledVector(dir, travel)
        .addScaledVector(accel, 0.5 * age * age);
      const size = sample(p.Size, t, 1);
      const transparency = sample(p.Transparency, t, 0);
      const color = sample(p.Color, t, [1, 1, 1]);
      const material = new THREE.SpriteMaterial({
        map: texture,
        color: srgb(color).multiplyScalar(1 + (p.LightEmission ?? 0) * 0.6),
        transparent: true,
        opacity: Math.max(0, 1 - transparency),
        depthWrite: false,
        blending: additive ? THREE.AdditiveBlending : THREE.NormalBlending,
      });
      const sprite = new THREE.Sprite(material);
      sprite.position.copy(position);
      sprite.scale.set(size, size, 1);
      group.add(sprite);
    }
  }
}
