// Roblox font families (rbxasset://fonts/families/<Name>.json), drawn with Roblox's own
// font files where fetch_content.py has fetched them (the families the game uses), and
// otherwise with the same typefaces from @fontsource (Inter standing in for Builder Sans).
// Gotham is gone from Roblox, which draws it as Montserrat.

// The faces of each family that ship with Roblox, by weight, in rbxcontent/fonts/.
const ROBLOX_FACES = {
  BuilderSans: {
    400: 'BuilderSans-Regular.otf',
    500: 'BuilderSans-Medium.otf',
    700: 'BuilderSans-Bold.otf',
    800: 'BuilderSans-ExtraBold.otf',
  },
  Montserrat: {
    400: 'Montserrat-Regular.ttf',
    500: 'Montserrat-Medium.ttf',
    700: 'Montserrat-Bold.ttf',
    900: 'Montserrat-Black.ttf',
  },
  FredokaOne: { 400: 'FredokaOne-Regular.ttf' },
  Oswald: { 400: 'Oswald-Regular.ttf', 700: 'Oswald-Bold.ttf' },
  SpecialElite: { 400: 'SpecialElite-Regular.ttf' },
};
ROBLOX_FACES.GothamSSm = ROBLOX_FACES.Montserrat;
ROBLOX_FACES.Gotham = ROBLOX_FACES.Montserrat;

const FAMILIES = {
  AmaticSC: 'amatic-sc',
  Arimo: 'arimo',
  Arial: 'arimo',
  LegacyArial: 'arimo',
  Bangers: 'bangers',
  BuilderSans: 'inter',
  Creepster: 'creepster',
  DenkOne: 'denk-one',
  FredokaOne: 'fredoka-one',
  GothamSSm: 'montserrat',
  Gotham: 'montserrat',
  IndieFlower: 'indie-flower',
  JosefinSans: 'josefin-sans',
  Kalam: 'kalam',
  LuckiestGuy: 'luckiest-guy',
  Merriweather: 'merriweather',
  Michroma: 'michroma',
  Montserrat: 'montserrat',
  Nunito: 'nunito',
  Oswald: 'oswald',
  PatrickHand: 'patrick-hand',
  PermanentMarker: 'permanent-marker',
  PressStart2P: 'press-start-2p',
  Roboto: 'roboto',
  RobotoCondensed: 'roboto-condensed',
  RobotoMono: 'roboto-mono',
  Sarpanch: 'sarpanch',
  SourceSansPro: 'source-sans-3',
  SpecialElite: 'special-elite',
  TitilliumWeb: 'titillium-web',
  Ubuntu: 'ubuntu',
};

const WEIGHTS = {
  Thin: 100,
  ExtraLight: 200,
  Light: 300,
  Regular: 400,
  Medium: 500,
  SemiBold: 600,
  Bold: 700,
  ExtraBold: 800,
  Heavy: 900,
};

const loaded = new Map();

function familyName(font) {
  const match = /families\/([A-Za-z0-9_]+)\.json/.exec(font?.family ?? '');
  return match ? match[1] : 'SourceSansPro';
}

// The CSS family and weight for a Roblox Font value ({family, weight, style}).
export function cssFont(font) {
  const pkg = FAMILIES[familyName(font)] ?? 'source-sans-3';
  return {
    pkg,
    family: `rbx-${pkg}`,
    weight: WEIGHTS[font?.weight] ?? 400,
    italic: font?.style === 'Italic',
  };
}

async function tryLoad(pkg, weight, italic) {
  const style = italic ? 'italic' : 'normal';
  const url = `https://cdn.jsdelivr.net/npm/@fontsource/${pkg}@5.3.0/files/${pkg}-latin-${weight}-${style}.woff2`;
  const response = await fetch(url, { method: 'HEAD' });
  if (!response.ok) {
    return false;
  }
  const face = new FontFace(`rbx-${pkg}`, `url(${url})`, { weight: String(weight), style });
  await face.load();
  document.fonts.add(face);
  return true;
}

// Loads Roblox's own face at the closest weight that ships with Roblox, if it's been
// fetched.
async function tryRoblox(font, pkg, weight, italic) {
  const faces = ROBLOX_FACES[familyName(font)];
  if (!faces || italic) {
    return false;
  }
  const nearest = Object.keys(faces)
    .map(Number)
    .sort((a, b) => Math.abs(a - weight) - Math.abs(b - weight))[0];
  const url = `rbxcontent/fonts/${faces[nearest]}`;
  const response = await fetch(url, { method: 'HEAD' });
  if (!response.ok) {
    return false;
  }
  const face = new FontFace(`rbx-${pkg}`, `url(${url})`, { weight: String(weight), style: 'normal' });
  await face.load();
  document.fonts.add(face);
  return true;
}

// Loads the closest available weight of a font; single-weight families (Fredoka One,
// Luckiest Guy...) load their only weight and the browser synthesizes the rest.
export async function loadFont(font) {
  const { pkg, weight, italic } = cssFont(font);
  const key = `${pkg}|${weight}|${italic}`;
  if (loaded.has(key)) {
    return loaded.get(key);
  }
  const attempt = (async () => {
    if (await tryRoblox(font, pkg, weight, italic)) {
      return true;
    }
    const candidates = [weight, 400, 700, 500, 600, 300, 800, 900, 200, 100];
    for (const w of candidates) {
      if (await tryLoad(pkg, w, italic)) {
        return true;
      }
    }
    return italic ? tryLoad(pkg, 400, false) : false;
  })();
  loaded.set(key, attempt);
  return attempt;
}

// The canvas font string for a Roblox Font at a pixel size.
export function canvasFont(font, px, bold = false, italic = false) {
  const css = cssFont(font);
  const weight = bold ? Math.max(css.weight, 700) : css.weight;
  const style = italic || css.italic ? 'italic ' : '';
  return `${style}${weight} ${px}px "${css.family}", "Noto Color Emoji", sans-serif`;
}

// Every Font value anywhere in a GUI tree, so they can load before drawing.
export function fontsIn(node, into = []) {
  if (!node) {
    return into;
  }
  if (node.props?.FontFace) {
    into.push(node.props.FontFace);
  }
  for (const child of node.children ?? []) {
    fontsIn(child, into);
  }
  return into;
}
