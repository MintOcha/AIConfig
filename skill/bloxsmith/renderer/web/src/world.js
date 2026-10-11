// Builds a three.js scene from the workspace and lighting trees written by
// tools/preview/Scene.luau: parts (blocks, balls, cylinders, wedges, the classic head
// mesh), materials, decals, SurfaceGuis, point and spot lights with shadows, terrain
// water, the sky, Atmosphere fog, and particle emitters.

import * as THREE from 'three';
import { drawLayer, loadFontsFor } from './gui.js';
import { partMaterial, srgb, studsPerTile } from './materials.js';
import { buildParticles } from './particles.js';

const BASE_PARTS = new Set([
  'Part',
  'WedgePart',
  'CornerWedgePart',
  'TrussPart',
  'Seat',
  'VehicleSeat',
  'SpawnLocation',
  'MeshPart',
  'UnionOperation',
  'SkateboardPlatform',
]);

// Tuning that maps Roblox brightness values onto three.js light units.
const POINT_SCALE = 1.4;
const SPOT_SCALE = 4.5;

export function cframeMatrix(c) {
  const [x, y, z, r00, r01, r02, r10, r11, r12, r20, r21, r22] = c;
  return new THREE.Matrix4().set(r00, r01, r02, x, r10, r11, r12, y, r20, r21, r22, z, 0, 0, 0, 1);
}

// Box geometry with UVs in studs / tile, so material textures keep their real scale.
function boxGeometry(size, tile) {
  const geometry = new THREE.BoxGeometry(size[0], size[1], size[2]);
  const pos = geometry.attributes.position;
  const normal = geometry.attributes.normal;
  const uv = geometry.attributes.uv;
  for (let i = 0; i < pos.count; i++) {
    const nx = Math.abs(normal.getX(i));
    const ny = Math.abs(normal.getY(i));
    const x = pos.getX(i);
    const y = pos.getY(i);
    const z = pos.getZ(i);
    let u;
    let v;
    if (nx > 0.5) {
      u = z;
      v = y;
    } else if (ny > 0.5) {
      u = x;
      v = z;
    } else {
      u = x;
      v = y;
    }
    uv.setXY(i, u / tile, v / tile);
  }
  return geometry;
}

// Roblox's classic head: a cylinder with rounded top and bottom edges.
function headGeometry(width, height) {
  const r = width / 2;
  const h = height / 2;
  const c = Math.min(r, h) * 0.55;
  const points = [new THREE.Vector2(0, -h)];
  for (let i = 0; i <= 8; i++) {
    const a = -Math.PI / 2 + (i / 8) * (Math.PI / 2);
    points.push(new THREE.Vector2(r - c + Math.cos(a) * c, -h + c + Math.sin(a) * c));
  }
  for (let i = 0; i <= 8; i++) {
    const a = (i / 8) * (Math.PI / 2);
    points.push(new THREE.Vector2(r - c + Math.cos(a) * c, h - c + Math.sin(a) * c));
  }
  points.push(new THREE.Vector2(0, h));
  return new THREE.LatheGeometry(points, 40);
}

// Roblox wedges: full base, tall at the back (+Z), sloping down to the front (-Z).
function wedgeGeometry(size) {
  const [w, h, d] = size.map((s) => s / 2);
  const shape = new THREE.BufferGeometry();
  const v = [
    [-w, -h, -d],
    [w, -h, -d],
    [w, -h, d],
    [-w, -h, d],
    [-w, h, d],
    [w, h, d],
  ];
  // Counter-clockwise seen from outside, so every face's normal points out.
  const faces = [
    [0, 1, 2],
    [0, 2, 3], // bottom (-Y)
    [3, 2, 5],
    [3, 5, 4], // back (+Z)
    [0, 4, 5],
    [0, 5, 1], // slope (up and to the front)
    [0, 3, 4], // left side (-X)
    [1, 5, 2], // right side (+X)
  ];
  const positions = [];
  for (const f of faces) for (const i of f) positions.push(...v[i]);
  shape.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
  shape.computeVertexNormals();
  const uvs = [];
  for (let i = 0; i < positions.length / 3; i++) uvs.push(positions[i * 3] / 4, positions[i * 3 + 1] / 4);
  shape.setAttribute('uv', new THREE.Float32BufferAttribute(uvs, 2));
  return shape;
}

function partGeometry(node) {
  const p = node.props;
  const size = p.Size ?? [4, 1, 2];
  const tile = studsPerTile(p.Material);
  const mesh = node.children.find(
    (c) => c.class === 'SpecialMesh' || c.class === 'BlockMesh' || c.class === 'CylinderMesh',
  );
  if (mesh) {
    const scale = mesh.props.Scale ?? [1, 1, 1];
    const offset = mesh.props.Offset ?? [0, 0, 0];
    const type =
      mesh.class === 'BlockMesh' ? 'Brick' : mesh.class === 'CylinderMesh' ? 'Cylinder' : mesh.props.MeshType;
    let geometry;
    if (type === 'Head') {
      const width = Math.min(size[0], size[2]) * scale[0];
      geometry = headGeometry(width, size[1] * scale[1]);
      geometry.userData.head = { width, height: size[1] * scale[1] };
    } else if (type === 'Sphere') {
      geometry = new THREE.SphereGeometry(0.5, 32, 20);
      geometry.scale(size[0] * scale[0], size[1] * scale[1], size[2] * scale[2]);
    } else if (type === 'Cylinder') {
      const d = Math.min(size[1] * scale[1], size[2] * scale[2]);
      geometry = new THREE.CylinderGeometry(d / 2, d / 2, size[0] * scale[0], 32);
      geometry.rotateZ(Math.PI / 2);
    } else if (type === 'Wedge') {
      geometry = wedgeGeometry([size[0] * scale[0], size[1] * scale[1], size[2] * scale[2]]);
    } else {
      geometry = boxGeometry([size[0] * scale[0], size[1] * scale[1], size[2] * scale[2]], tile);
    }
    geometry.translate(offset[0], offset[1], offset[2]);
    return geometry;
  }
  if (node.class === 'WedgePart' || p.Shape === 'Wedge') return wedgeGeometry(size);
  if (p.Shape === 'Ball') {
    const d = Math.min(...size);
    return new THREE.SphereGeometry(d / 2, 32, 20);
  }
  if (p.Shape === 'Cylinder') {
    const d = Math.min(size[1], size[2]);
    const geometry = new THREE.CylinderGeometry(d / 2, d / 2, size[0], 40);
    geometry.rotateZ(Math.PI / 2);
    return geometry;
  }
  return boxGeometry(size, tile);
}

const FACE_ROTATION = {
  Front: new THREE.Euler(0, Math.PI, 0),
  Back: new THREE.Euler(0, 0, 0),
  Right: new THREE.Euler(0, Math.PI / 2, 0),
  Left: new THREE.Euler(0, -Math.PI / 2, 0),
  Top: new THREE.Euler(-Math.PI / 2, 0, 0),
  Bottom: new THREE.Euler(Math.PI / 2, 0, 0),
};
const FACE_NORMAL = {
  Front: [0, 0, -1],
  Back: [0, 0, 1],
  Right: [1, 0, 0],
  Left: [-1, 0, 0],
  Top: [0, 1, 0],
  Bottom: [0, -1, 0],
};

function faceSize(face, size) {
  if (face === 'Right' || face === 'Left') return [size[2], size[1]];
  if (face === 'Top' || face === 'Bottom') return [size[0], size[2]];
  return [size[0], size[1]];
}

// A flat quad lying on one face of a part, in the part's space.
function facePlane(face, size, material, lift = 0.012) {
  const [w, h] = faceSize(face, size);
  const plane = new THREE.Mesh(new THREE.PlaneGeometry(w, h), material);
  plane.rotation.copy(FACE_ROTATION[face] ?? FACE_ROTATION.Front);
  const n = FACE_NORMAL[face] ?? FACE_NORMAL.Front;
  plane.position.set(
    (n[0] * size[0]) / 2 + n[0] * lift,
    (n[1] * size[1]) / 2 + n[1] * lift,
    (n[2] * size[2]) / 2 + n[2] * lift,
  );
  return plane;
}

// The face decal wrapped over the front of a classic head, projected straight on.
function headDecal(geometry, texture, color, transparency) {
  const { width, height } = geometry.userData.head;
  const decal = geometry.clone();
  decal.scale(1.012, 1.004, 1.012);
  const pos = decal.attributes.position;
  const uv = decal.attributes.uv;
  for (let i = 0; i < pos.count; i++) {
    const x = pos.getX(i);
    const y = pos.getY(i);
    const z = pos.getZ(i);
    uv.setXY(i, z < 0 ? 0.5 - x / width : -1, 0.5 + y / height);
  }
  texture.wrapS = texture.wrapT = THREE.ClampToEdgeWrapping;
  return new THREE.Mesh(
    decal,
    new THREE.MeshStandardMaterial({
      map: texture,
      color: srgb(color ?? [1, 1, 1]),
      transparent: true,
      opacity: 1 - (transparency ?? 0),
      alphaTest: 0.05,
      roughness: 0.6,
      depthWrite: false,
      polygonOffset: true,
      polygonOffsetFactor: -2,
    }),
  );
}

function sunDirection(clockTime) {
  const a = ((clockTime - 6) / 24) * Math.PI * 2;
  return new THREE.Vector3(Math.cos(a) * 0.6, Math.sin(a), 0.45).normalize();
}

function skyDome(top, horizon, stars, moonDir, showMoon) {
  const geometry = new THREE.SphereGeometry(1400, 48, 24);
  const material = new THREE.ShaderMaterial({
    side: THREE.BackSide,
    depthWrite: false,
    fog: false,
    uniforms: {
      top: { value: top },
      horizon: { value: horizon },
      stars: { value: stars },
      moonDir: { value: moonDir },
      showMoon: { value: showMoon ? 1 : 0 },
    },
    vertexShader: `
      varying vec3 vDir;
      void main() {
        vDir = normalize(position);
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }`,
    fragmentShader: `
      uniform vec3 top; uniform vec3 horizon; uniform float stars; uniform vec3 moonDir; uniform float showMoon;
      varying vec3 vDir;
      float hash(vec3 p) { return fract(sin(dot(p, vec3(12.9898, 78.233, 37.719))) * 43758.5453); }
      void main() {
        float h = clamp(vDir.y, 0.0, 1.0);
        vec3 color = mix(horizon, top, pow(h, 0.55));
        vec3 cell = floor(vDir * 380.0);
        float s = step(0.9975, hash(cell)) * stars * smoothstep(0.02, 0.3, vDir.y);
        color += vec3(s) * 0.9;
        float m = dot(normalize(vDir), normalize(moonDir));
        color += showMoon * (smoothstep(0.9993, 0.9996, m) * vec3(0.9, 0.92, 1.0) + smoothstep(0.985, 1.0, m) * 0.12 * horizon);
        gl_FragColor = vec4(color, 1.0);
        #include <colorspace_fragment>
      }`,
  });
  return new THREE.Mesh(geometry, material);
}

function findChild(node, cls) {
  return node.children.find((c) => c.class === cls) ?? null;
}

export async function buildWorld(data, { renderer, textureFor, imageFor }) {
  const scene = new THREE.Scene();
  const lightingNode = data.lighting;
  const L = lightingNode.props;
  const atmosphere = findChild(lightingNode, 'Atmosphere');
  const clock = L.ClockTime ?? 14;
  const sun = sunDirection(clock);
  const night = sun.y < 0;
  const skyLight = night ? 0.18 : 1;

  // Sky and fog.
  const A = atmosphere?.props;
  const atmoColor = srgb(A?.Color ?? [0.78, 0.78, 0.78]);
  const decay = srgb(A?.Decay ?? [0.36, 0.33, 0.29]);
  const horizon = atmoColor
    .clone()
    .lerp(decay, 0.35)
    .multiplyScalar(skyLight * 1.2);
  const top = night
    ? decay
        .clone()
        .multiplyScalar(0.12)
        .add(new THREE.Color(0.004, 0.006, 0.016))
    : new THREE.Color(0.25, 0.45, 0.85);
  const haze = A?.Haze ?? 0;
  const starCount = findChild(lightingNode, 'Sky')?.props.StarCount ?? 3000;
  const moonDir = sun.clone().negate();
  const sky = skyDome(
    top,
    horizon,
    night ? Math.max(0, 1 - haze / 3) * Math.min(1, starCount / 3000) : 0,
    moonDir,
    night,
  );
  scene.add(sky);
  if (A && !new URLSearchParams(location.search).has('nofog')) {
    const density = (A.Density ?? 0.3) * 0.024 * (1 + (A.Haze ?? 0) * 0.08);
    scene.fog = new THREE.FogExp2(horizon.clone(), density);
  }

  // Ambient light: outdoor ambient from the sky, indoor ambient from below.
  const outdoor = srgb(L.OutdoorAmbient ?? [0.5, 0.5, 0.5]);
  const indoor = srgb(L.Ambient ?? [0, 0, 0]);
  scene.add(new THREE.HemisphereLight(outdoor, indoor, 2.2 + (L.EnvironmentDiffuseScale ?? 0) * 1.5));
  const brightness = L.Brightness ?? 2;
  const celestial = new THREE.DirectionalLight(
    night ? new THREE.Color(0.62, 0.7, 0.95) : new THREE.Color(1, 0.96, 0.88),
    night ? brightness * 0.35 : brightness * 1.4,
  );
  celestial.position.copy((night ? moonDir : sun).clone().multiplyScalar(200));
  celestial.target.position.set(0, 0, -20);
  celestial.castShadow = L.GlobalShadows !== false;
  celestial.shadow.mapSize.set(2048, 2048);
  Object.assign(celestial.shadow.camera, { left: -80, right: 80, top: 80, bottom: -80, near: 1, far: 500 });
  celestial.shadow.bias = -0.0006;
  celestial.shadow.normalBias = 0.06;
  scene.add(celestial, celestial.target);

  // Environment for reflections: the sky itself.
  const envScene = new THREE.Scene();
  envScene.add(skyDome(top, horizon, 0, moonDir, false));
  const pmrem = new THREE.PMREMGenerator(renderer);
  const envMap = pmrem.fromScene(envScene, 0.04).texture;
  scene.environment = null;

  const emitters = [];
  const billboards = [];
  const surfaceGuis = [];
  const partsById = new Map();

  function visit(node, parentWorld) {
    let world = parentWorld;
    if (BASE_PARTS.has(node.class)) {
      const p = node.props;
      world = cframeMatrix(p.CFrame ?? [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]);
      partsById.set(node.id, { node, world });
      const group = new THREE.Group();
      group.matrixAutoUpdate = false;
      group.matrix.copy(world);
      scene.add(group);
      // Roblox's effective transparency: LocalTransparencyModifier is what the first-person
      // camera uses to hide your own character from you.
      const transparency = 1 - (1 - (p.Transparency ?? 0)) * (1 - (p.LocalTransparencyModifier ?? 0));
      const geometry = partGeometry(node);
      if (transparency < 0.999) {
        const mesh = new THREE.Mesh(
          geometry,
          partMaterial({
            material: p.Material,
            color: p.Color ?? [0.64, 0.64, 0.64],
            transparency,
            reflectance: p.Reflectance ?? 0,
            envMap,
          }),
        );
        mesh.castShadow = p.CastShadow !== false && transparency < 0.5 && p.Material !== 'Neon';
        mesh.receiveShadow = p.Material !== 'Neon';
        group.add(mesh);
      }
      for (const child of node.children) {
        if (child.class === 'Decal' || child.class === 'Texture') {
          const texture = textureFor(child.props.Texture ?? child.props.ColorMap);
          const decalTransparency =
            1 - (1 - (child.props.Transparency ?? 0)) * (1 - (child.props.LocalTransparencyModifier ?? 0));
          if (!texture || decalTransparency >= 0.999) continue;
          if (geometry.userData.head && (child.props.Face ?? 'Front') === 'Front') {
            group.add(headDecal(geometry, texture, child.props.Color3, decalTransparency));
          } else {
            const material = new THREE.MeshStandardMaterial({
              map: texture,
              color: srgb(child.props.Color3 ?? [1, 1, 1]),
              transparent: true,
              opacity: 1 - decalTransparency,
              alphaTest: 0.05,
              depthWrite: false,
            });
            group.add(facePlane(child.props.Face ?? 'Front', p.Size, material));
          }
        } else if (child.class === 'SurfaceGui') {
          surfaceGuis.push({ gui: child, part: node, group });
        } else if (child.class === 'PointLight' || child.class === 'SpotLight' || child.class === 'SurfaceLight') {
          addLight(child, group, p.Size);
        } else if (child.class === 'ParticleEmitter') {
          emitters.push({ node: child, world });
        }
      }
    } else if (node.class === 'Attachment') {
      const p = node.props;
      const local = cframeMatrix(p.CFrame ?? [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]);
      const position = p.Position ?? [0, 0, 0];
      if (local.elements[12] === 0 && local.elements[13] === 0 && local.elements[14] === 0) {
        local.setPosition(position[0], position[1], position[2]);
      }
      world = parentWorld ? parentWorld.clone().multiply(local) : local;
      for (const child of node.children) {
        if (child.class === 'ParticleEmitter') emitters.push({ node: child, world });
      }
    } else if (node.class === 'BillboardGui') {
      billboards.push({ gui: node, world: parentWorld });
    } else if (node.class === 'SurfaceGui' && node.props.Adornee) {
      surfaceGuis.push({ gui: node, adornee: node.props.Adornee });
    }
    for (const child of node.children) {
      if (
        !BASE_PARTS.has(node.class) ||
        child.class === 'Attachment' ||
        BASE_PARTS.has(child.class) ||
        child.class === 'BillboardGui' ||
        child.class === 'Model' ||
        child.class === 'Folder'
      ) {
        visit(child, world);
      }
    }
  }

  function addLight(node, group, size) {
    const p = node.props;
    if (p.Enabled === false) return;
    const color = srgb(p.Color ?? [1, 1, 1]);
    const brightness = p.Brightness ?? 1;
    const range = p.Range ?? 16;
    if (node.class === 'PointLight') {
      const light = new THREE.PointLight(color, brightness * POINT_SCALE, range, 1);
      light.castShadow = p.Shadows === true;
      group.add(light);
      return;
    }
    const face = p.Face ?? 'Front';
    const n = FACE_NORMAL[face];
    const angle = Math.min(89, (p.Angle ?? 90) / 2) * (Math.PI / 180);
    const light = new THREE.SpotLight(color, brightness * SPOT_SCALE, range, angle, 0.35, 1);
    const offset = node.class === 'SurfaceLight' ? n.map((v, i) => (v * size[i]) / 2) : [0, 0, 0];
    light.position.set(...offset);
    light.target.position.set(offset[0] + n[0], offset[1] + n[1], offset[2] + n[2]);
    group.add(light, light.target);
    if (p.Shadows) {
      light.castShadow = true;
      light.shadow.mapSize.set(2048, 2048);
      light.shadow.bias = -0.0006;
      light.shadow.normalBias = 0.02;
      light.shadow.camera.near = 0.2;
    }
  }

  visit(data.workspace, null);

  // Terrain water: the top surface of each Water fill.
  const terrainNode = data.workspace.children.find((c) => c.class === 'Terrain');
  for (const fill of data.terrain ?? []) {
    if (fill.material !== 'Water') continue;
    const T = terrainNode?.props ?? {};
    const [cx, cy, cz] = fill.cframe;
    const [sx, sy, sz] = fill.size;
    const water = new THREE.Mesh(
      new THREE.PlaneGeometry(sx, sz, 1, 1),
      new THREE.MeshPhysicalMaterial({
        color: srgb(T.WaterColor ?? [0.05, 0.33, 0.36]),
        roughness: 0.08,
        metalness: 0,
        envMap,
        envMapIntensity: 0.6 + (T.WaterReflectance ?? 1) * 1.2,
        clearcoat: 1,
        transparent: false,
      }),
    );
    water.rotation.x = -Math.PI / 2;
    water.position.set(cx, cy + sy / 2, cz);
    water.receiveShadow = true;
    scene.add(water);
  }

  // Surface GUIs, drawn with the same GUI engine as the HUD.
  for (const entry of surfaceGuis) {
    const gui = entry.gui;
    const part = entry.part ?? partsById.get(entry.adornee)?.node;
    const group = entry.group;
    if (!part || !group || gui.props.Enabled === false) continue;
    await loadFontsFor(gui);
    const face = gui.props.Face ?? 'Front';
    const [fw, fh] = faceSize(face, part.props.Size);
    const ppu = gui.props.SizingMode === 'FixedSize' ? null : (gui.props.PixelsPerStud ?? 50);
    const canvasSize = ppu ? [fw * ppu, fh * ppu] : (gui.props.CanvasSize ?? [800, 600]);
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(canvasSize[0]));
    canvas.height = Math.max(1, Math.round(canvasSize[1]));
    drawLayer(canvas.getContext('2d'), gui, { x: 0, y: 0, w: canvas.width, h: canvas.height }, imageFor);
    const texture = new THREE.CanvasTexture(canvas);
    texture.colorSpace = THREE.SRGBColorSpace;
    const influence = gui.props.LightInfluence ?? 1;
    const material = new THREE.MeshStandardMaterial({
      map: texture,
      emissiveMap: texture,
      emissive: new THREE.Color(1, 1, 1),
      emissiveIntensity: (1 - influence) * (gui.props.Brightness ?? 1),
      transparent: true,
      depthWrite: false,
      roughness: 0.9,
    });
    group.add(facePlane(face, part.props.Size, material, 0.02));
  }

  const particles = new THREE.Group();
  buildParticles(particles, emitters, textureFor);
  scene.add(particles);

  return { scene, sky, billboards, partsById, exposure: Math.pow(2, L.ExposureCompensation ?? 0) };
}
