// Renders one exported scene: the 3D view from the player's camera with Roblox-like
// post-processing (bloom for Neon, ColorCorrection, Blur), then the player's
// ScreenGuis and an approximation of Roblox's top bar over it.

import * as THREE from 'three';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { ShaderPass } from 'three/addons/postprocessing/ShaderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { drawLayer, drawScreenGuis, loadFontsFor } from './gui.js';
import { buildWorld, cframeMatrix } from './world.js';

const params = new URLSearchParams(location.search);

async function loadImage(url) {
  return new Promise((resolve) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => resolve(null);
    image.src = url;
  });
}

// Built-in rbxasset:// content is served from rbxcontent/ (see fetch-content.mjs);
// uploaded assets (rbxassetid://) aren't available offline.
function localPath(content) {
  if (typeof content !== 'string' || !content.startsWith('rbxasset://')) return null;
  return `rbxcontent/${content.slice('rbxasset://'.length).replace(/\.dds$/i, '.png')}`;
}

function collectContent(node, into) {
  if (!node) return into;
  for (const key of ['Texture', 'Image', 'ColorMap']) {
    const value = node.props?.[key];
    if (typeof value === 'string' && value.startsWith('rbxasset://')) into.add(value);
  }
  for (const child of node.children ?? []) collectContent(child, into);
  return into;
}

const ColorCorrectionShader = {
  uniforms: {
    tDiffuse: { value: null },
    brightness: { value: 0 },
    contrast: { value: 0 },
    saturation: { value: 0 },
    tint: { value: new THREE.Color(1, 1, 1) },
  },
  vertexShader: `varying vec2 vUv; void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
  fragmentShader: `
    uniform sampler2D tDiffuse; uniform float brightness; uniform float contrast; uniform float saturation; uniform vec3 tint;
    varying vec2 vUv;
    void main() {
      vec4 c = texture2D(tDiffuse, vUv);
      vec3 rgb = c.rgb + brightness;
      rgb = (rgb - 0.18) * (1.0 + contrast) + 0.18;
      float l = dot(rgb, vec3(0.2126, 0.7152, 0.0722));
      rgb = mix(vec3(l), rgb, 1.0 + saturation);
      gl_FragColor = vec4(max(rgb * tint, 0.0), c.a);
    }`,
};

function drawTopBar(ctx, screen, safe) {
  // Roblox's own buttons at the left of the top bar row, as its client draws them: the
  // Roblox button (x 16 to 60 from the safe area's edge, y 12 to 56), then the unibar
  // with the menu and chat (x 68 to 164), near-black at 8% see-through. Decorative: it
  // shows what the game's status pills sit beside.
  const x0 = safe.x + 16;
  const y = 12;
  const size = 44;
  const fill = 'rgba(18, 18, 21, 0.92)';
  ctx.save();
  ctx.fillStyle = fill;
  ctx.beginPath();
  ctx.roundRect(x0, y, size, size, size / 2);
  ctx.fill();
  ctx.translate(x0 + size / 2, y + size / 2);
  ctx.rotate((15 * Math.PI) / 180);
  ctx.fillStyle = '#f7f7f8';
  ctx.fillRect(-9, -9, 18, 18);
  ctx.fillStyle = 'rgba(18, 18, 21, 1)';
  ctx.fillRect(-3, -3, 6, 6);
  ctx.restore();
  ctx.save();
  const x = x0 + size + 8;
  ctx.fillStyle = fill;
  ctx.beginPath();
  ctx.roundRect(x, y, 96, size, size / 2);
  ctx.fill();
  // The menu (three bars) and chat (a speech balloon) icons.
  ctx.strokeStyle = '#f7f7f8';
  ctx.lineWidth = 2;
  for (let i = 0; i < 3; i++) {
    ctx.beginPath();
    ctx.moveTo(x + 15, y + 15 + i * 7);
    ctx.lineTo(x + 33, y + 15 + i * 7);
    ctx.stroke();
  }
  ctx.beginPath();
  ctx.roundRect(x + 58, y + 13, 20, 15, 4);
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(x + 63, y + 28);
  ctx.lineTo(x + 61, y + 33);
  ctx.lineTo(x + 68, y + 28);
  ctx.stroke();
  ctx.restore();
  return screen;
}

// Lua's empty tables encode as {} whatever they held, so make every node's children
// an array and its props an object before anything walks the tree.
function normalize(node) {
  if (!node) return;
  node.children = Array.isArray(node.children) ? node.children : [];
  node.props = node.props && !Array.isArray(node.props) ? node.props : {};
  for (const child of node.children) normalize(child);
}

function indexNodes(node, into, parent = null) {
  if (!node) return into;
  node.parent = parent;
  into.set(node.id, node);
  for (const child of node.children) indexNodes(child, into, node);
  return into;
}

// Roblox's default ProximityPrompt (CoreScripts/ProximityPrompt.lua), for prompts the
// player's character is close enough to: a 72 px dark pill, the key in a grey circle,
// the object text above the action text. Always on top, centred on its attachment.
async function drawPrompts(ctx, data, partsById, nodesById, camera, W, H) {
  const character = nodesById.get(data.localCharacter);
  if (!character) return;
  const rootNode = character.children.find((c) => c.name === 'HumanoidRootPart');
  const root = rootNode && partsById.get(rootNode.id);
  if (!root) return;
  const rootPosition = new THREE.Vector3().setFromMatrixPosition(root.world);
  const font = { family: 'rbxasset://fonts/families/BuilderSans.json', weight: 'Medium', style: 'Normal' };
  await loadFontsFor({ props: { FontFace: font }, children: [] });
  const { canvasFont } = await import('./fonts.js');
  for (const node of nodesById.values()) {
    if (node.class !== 'ProximityPrompt' || node.props.Enabled === false) continue;
    const holder = node.parent;
    let position = null;
    if (holder?.class === 'Attachment' && partsById.get(holder.parent?.id)) {
      const local = cframeMatrix(holder.props.CFrame ?? [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]);
      position = new THREE.Vector3().setFromMatrixPosition(
        partsById.get(holder.parent.id).world.clone().multiply(local),
      );
    } else if (holder && partsById.get(holder.id)) {
      position = new THREE.Vector3().setFromMatrixPosition(partsById.get(holder.id).world);
    }
    if (!position || position.distanceTo(rootPosition) > (node.props.MaxActivationDistance ?? 10)) continue;
    const screen = position.clone().project(camera);
    if (screen.z > 1) continue;
    const cx = ((screen.x + 1) / 2) * W;
    const cy = ((1 - screen.y) / 2) * H;
    const action = node.props.ActionText ?? '';
    const object = node.props.ObjectText ?? '';
    ctx.save();
    ctx.font = canvasFont(font, 19);
    const actionWidth = ctx.measureText(action).width;
    ctx.font = canvasFont(font, 14);
    const objectWidth = ctx.measureText(object).width;
    const width = 72 + Math.max(actionWidth, objectWidth) + (object ? 15 : 24);
    const left = cx - width / 2;
    const top = cy - 36;
    ctx.fillStyle = 'rgba(18, 18, 18, 0.8)';
    ctx.beginPath();
    ctx.roundRect(left, top, width, 72, 8);
    ctx.fill();
    ctx.fillStyle = 'rgba(163, 162, 165, 0.5)';
    ctx.beginPath();
    ctx.arc(left + 36, top + 36, 24, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = '#ffffff';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.roundRect(left + 36 - 12, top + 36 - 14, 24, 26, 5);
    ctx.stroke();
    ctx.fillStyle = '#ffffff';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.font = canvasFont(font, 16);
    ctx.fillText(node.props.KeyboardKeyCode ?? 'E', left + 36, top + 35);
    ctx.textAlign = 'left';
    if (object) {
      ctx.font = canvasFont(font, 14);
      ctx.fillStyle = 'rgb(179, 179, 179)';
      ctx.fillText(object, left + 72, top + 26);
    }
    ctx.font = canvasFont(font, 19);
    ctx.fillStyle = '#ffffff';
    ctx.fillText(action, left + 72, top + (object ? 45 : 36));
    ctx.restore();
  }
}

// Where Roblox anchors a bubble: over a part, its top; over a model, the top of the
// model's bounding box above its HumanoidRootPart (so it clears hats), as ExpChat's
// BubbleChatBillboard:getVerticalOffset does. Then 1 stud up (its StudsOffset).
function bubbleAnchor(id, partsById, nodesById) {
  const part = partsById.get(id);
  if (part) {
    const size = part.node.props.Size ?? [1, 1, 1];
    return new THREE.Vector3(0, size[1] / 2 + 1, 0).applyMatrix4(part.world);
  }
  const model = nodesById.get(id);
  if (!model || model.class !== 'Model') return null;
  const box = new THREE.Box3();
  let root = null;
  const walk = (node) => {
    const entry = partsById.get(node.id);
    if (entry) {
      const [x, y, z] = entry.node.props.Size ?? [1, 1, 1];
      const local = new THREE.Box3(new THREE.Vector3(-x / 2, -y / 2, -z / 2), new THREE.Vector3(x / 2, y / 2, z / 2));
      box.union(local.applyMatrix4(entry.world));
      if (node.name === 'HumanoidRootPart' || (!root && node.id === model.props.PrimaryPart)) root = entry;
    }
    for (const child of node.children) walk(child);
  };
  walk(model);
  if (box.isEmpty()) return null;
  const centre = root ? new THREE.Vector3().setFromMatrixPosition(root.world) : box.getCenter(new THREE.Vector3());
  return new THREE.Vector3(centre.x, box.max.y + 1, centre.z);
}

// Roblox's chat bubbles (TextChatService:DisplayBubble) in their default style: a white
// rounded bubble with dark text and a tail, above what it belongs to, newest at the
// bottom. They're screen-sized, not world-sized, like Roblox's.
async function drawBubbles(ctx, bubbles, partsById, nodesById, camera, W, H) {
  if (bubbles.length === 0) return;
  const font = { family: 'rbxasset://fonts/families/BuilderSans.json', weight: 'Medium', style: 'Normal' };
  await loadFontsFor({ props: { FontFace: font }, children: [] });
  const byPart = new Map();
  for (const bubble of bubbles) {
    if (!byPart.has(bubble.adornee)) byPart.set(bubble.adornee, []);
    byPart.get(bubble.adornee).push(bubble);
  }
  const { canvasFont } = await import('./fonts.js');
  for (const [id, list] of byPart) {
    const world = bubbleAnchor(id, partsById, nodesById);
    if (!world) continue;
    const anchor = world.project(camera);
    if (anchor.z > 1) continue;
    let bottom = ((1 - anchor.y) / 2) * H;
    const x = ((anchor.x + 1) / 2) * W;
    list.sort((a, b) => a.age - b.age);
    for (const [index, bubble] of list.entries()) {
      ctx.font = canvasFont(font, 16);
      const maxWidth = 260;
      const words = bubble.message.split(' ');
      const lines = [''];
      for (const word of words) {
        const trial = lines[lines.length - 1] ? `${lines[lines.length - 1]} ${word}` : word;
        if (ctx.measureText(trial).width > maxWidth && lines[lines.length - 1]) lines.push(word);
        else lines[lines.length - 1] = trial;
      }
      const width = Math.max(...lines.map((l) => ctx.measureText(l).width)) + 24;
      const height = lines.length * 20 + 16;
      const tail = index === 0 ? 8 : 0;
      const top = bottom - tail - height;
      ctx.save();
      ctx.globalAlpha = index === 0 ? 1 : 0.85;
      ctx.fillStyle = 'rgba(250, 250, 250, 0.95)';
      ctx.beginPath();
      ctx.roundRect(x - width / 2, top, width, height, 12);
      ctx.fill();
      if (tail) {
        ctx.beginPath();
        ctx.moveTo(x - 8, top + height - 1);
        ctx.lineTo(x, top + height + tail);
        ctx.lineTo(x + 8, top + height - 1);
        ctx.fill();
      }
      ctx.fillStyle = 'rgb(57, 59, 61)';
      ctx.textBaseline = 'middle';
      ctx.textAlign = 'center';
      lines.forEach((line, i) => ctx.fillText(line, x, top + 8 + i * 20 + 10));
      ctx.restore();
      bottom = top - 6;
    }
  }
}

// BillboardGuis in the PlayerGui with an Adornee (like the passengers' speech bubbles),
// drawn over the scene where the adornee is on screen: StudsOffsetWorldSpace in world
// axes, StudsOffset in the camera's (x right, y up, z towards the camera), then moved by
// SizeOffset in units of their own size (y up). Their Size is in pixels.
function drawBillboards(ctx, playerGui, partsById, camera, W, H, images) {
  if (!playerGui) return;
  const right = new THREE.Vector3().setFromMatrixColumn(camera.matrixWorld, 0);
  const up = new THREE.Vector3().setFromMatrixColumn(camera.matrixWorld, 1);
  const back = new THREE.Vector3().setFromMatrixColumn(camera.matrixWorld, 2);
  for (const gui of playerGui.children) {
    const p = gui.props;
    if (gui.class !== 'BillboardGui' || p.Enabled === false || p.Adornee == null) continue;
    const part = partsById.get(p.Adornee);
    if (!part) continue;
    const [wx, wy, wz] = p.StudsOffsetWorldSpace ?? [0, 0, 0];
    const [sx, sy, sz] = p.StudsOffset ?? [0, 0, 0];
    const point = new THREE.Vector3()
      .setFromMatrixPosition(part.world)
      .add(new THREE.Vector3(wx, wy, wz))
      .addScaledVector(right, sx)
      .addScaledVector(up, sy)
      .addScaledVector(back, sz);
    const ndc = point.project(camera);
    if (ndc.z > 1) continue;
    const [, width, , height] = p.Size ?? [0, 0, 0, 0];
    const [ox, oy] = p.SizeOffset ?? [0, 0];
    const cx = ((ndc.x + 1) / 2) * W + ox * width;
    const cy = ((1 - ndc.y) / 2) * H - oy * height;
    drawLayer(ctx, gui, { x: cx - width / 2, y: cy - height / 2, w: width, h: height }, images);
  }
}

async function main() {
  const data = await (await fetch(params.get('scene'))).json();
  normalize(data.workspace);
  normalize(data.lighting);
  normalize(data.playerGui);
  data.terrain = Array.isArray(data.terrain) ? data.terrain : [];
  data.bubbles = Array.isArray(data.bubbles) ? data.bubbles : [];
  const [W, H] = data.screen;
  const safe = {
    x: (W - data.safeArea[0]) / 2,
    y: H - data.safeArea[1],
    w: data.safeArea[0],
    h: data.safeArea[1],
  };
  // Where TopbarSafeInsets puts a ScreenGui: the part of Roblox's top bar row left free.
  // GuiService.TopbarInset is measured from the safe area's left edge and the screen's top.
  const [tx0, ty0, tx1, ty1] = data.topbarInset ?? [0, 0, 0, 0];
  const topbar = { x: safe.x + tx0, y: ty0, w: tx1 - tx0, h: ty1 - ty0 };

  const content = collectContent(data.workspace, collectContent(data.playerGui, new Set()));
  const textures = new Map();
  const images = new Map();
  await Promise.all(
    [...content].map(async (id) => {
      const path = localPath(id);
      if (!path) return;
      const image = await loadImage(path);
      if (!image) return;
      images.set(id, image);
      const texture = new THREE.Texture(image);
      texture.colorSpace = THREE.SRGBColorSpace;
      texture.needsUpdate = true;
      textures.set(id, texture);
    }),
  );
  const textureFor = (id) => textures.get(id) ?? null;
  const imageFor = (id) => images.get(id) ?? null;

  const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
  renderer.setPixelRatio(1);
  renderer.setSize(W, H);
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFShadowMap;
  const activeEffect = (cls) => data.lighting.children.find((c) => c.class === cls && c.props.Enabled !== false);
  // ColorGradingEffect.TonemapperPreset = Retro: pre-2019 Roblox look, less saturated and less contrast than ACES
  const retro = activeEffect('ColorGradingEffect')?.props.TonemapperPreset === 'Retro';
  renderer.toneMapping = retro ? THREE.ReinhardToneMapping : THREE.ACESFilmicToneMapping;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  document.getElementById('world').appendChild(renderer.domElement);

  const world = await buildWorld(data, { renderer, textureFor, imageFor });
  renderer.toneMappingExposure = world.exposure * 1.05;

  const camera = new THREE.PerspectiveCamera(data.camera.fov ?? 70, W / H, 0.1, 3000);
  camera.matrixAutoUpdate = false;
  camera.matrix.copy(cframeMatrix(data.camera.cframe));
  camera.matrixWorld.copy(camera.matrix);
  camera.matrixWorldInverse.copy(camera.matrix).invert();
  world.sky.position.setFromMatrixPosition(camera.matrix);

  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(world.scene, camera));
  // A BloomEffect in Lighting sets the glow (Neon); without one the renderer's default glow applies
  const { Intensity = 0.55, Size = 25, Threshold = 0.92 } = activeEffect('BloomEffect')?.props ?? {};
  composer.addPass(new UnrealBloomPass(new THREE.Vector2(W, H), Intensity, Math.min(1, Size / 56), Threshold));
  const lighting = data.lighting;
  const cc = lighting.children.find((c) => c.class === 'ColorCorrectionEffect' && c.props.Enabled !== false);
  if (cc) {
    const pass = new ShaderPass(ColorCorrectionShader);
    pass.uniforms.brightness.value = cc.props.Brightness ?? 0;
    pass.uniforms.contrast.value = cc.props.Contrast ?? 0;
    pass.uniforms.saturation.value = cc.props.Saturation ?? 0;
    const tint = cc.props.TintColor ?? [1, 1, 1];
    pass.uniforms.tint.value = new THREE.Color().setRGB(tint[0], tint[1], tint[2], THREE.SRGBColorSpace);
    composer.addPass(pass);
  }
  composer.addPass(new OutputPass());
  composer.render();

  const blur = lighting.children.find((c) => c.class === 'BlurEffect' && c.props.Enabled !== false);
  if (blur && (blur.props.Size ?? 0) > 0) {
    renderer.domElement.style.filter = `blur(${(blur.props.Size * 0.5).toFixed(1)}px)`;
  }

  const overlay = document.getElementById('gui');
  const ctx = overlay.getContext('2d');
  const nodesById = indexNodes(data.workspace, new Map());
  async function drawOverlay() {
    overlay.width = W; // also clears it
    overlay.height = H;
    await drawBubbles(ctx, data.bubbles ?? [], world.partsById, nodesById, camera, W, H);
    await drawPrompts(ctx, data, world.partsById, nodesById, camera, W, H);
    if (data.playerGui) {
      await loadFontsFor(data.playerGui);
      drawBillboards(ctx, data.playerGui, world.partsById, camera, W, H, imageFor);
      drawScreenGuis(ctx, data.playerGui, { w: W, h: H }, safe, imageFor, topbar);
    }
    if (params.get('topbar') !== '0') drawTopBar(ctx, { w: W, h: H }, safe);
  }
  await drawOverlay();
  // render another camera from the same loaded scene; returns a PNG data url (world + overlay)
  window.__renderView = async (view) => {
    camera.fov = view.fov ?? camera.fov;
    camera.updateProjectionMatrix();
    camera.matrix.copy(cframeMatrix(view.cframe));
    camera.matrixWorld.copy(camera.matrix);
    camera.matrixWorldInverse.copy(camera.matrix).invert();
    world.sky.position.setFromMatrixPosition(camera.matrix);
    composer.render();
    await drawOverlay();
    const out = document.createElement('canvas');
    out.width = W;
    out.height = H;
    const c2 = out.getContext('2d');
    c2.drawImage(renderer.domElement, 0, 0);
    c2.drawImage(overlay, 0, 0);
    return out.toDataURL('image/png');
  };
  window.__previewDone = true;
}

main().catch((error) => {
  window.__previewError = String(error?.stack ?? error);
});
