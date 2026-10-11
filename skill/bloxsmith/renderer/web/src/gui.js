// Lays out and draws Roblox GUI trees (ScreenGui, SurfaceGui, BillboardGui) from the
// JSON written by tools/preview/Scene.luau, following Roblox's layout rules closely
// enough to judge a HUD: UDim2 sizes and positions, AnchorPoint, UIListLayout,
// UIPadding, AutomaticSize, UIScale, UIAspectRatioConstraint, UISizeConstraint, text
// wrapping and TextScaled, rich text, UIStroke (text outlines and borders), UICorner,
// UIGradient, UIShadow, ScrollingFrame clipping, ZIndex (Sibling), Rotation and
// CanvasGroup.
//
// Images (rbxassetid://...) can't be fetched here; known built-in rbxasset:// textures
// are drawn when a loader provides them, anything else is skipped.

import { canvasFont, fontsIn, loadFont } from './fonts.js';

const GUI_OBJECTS = new Set([
  'Frame',
  'TextLabel',
  'TextButton',
  'TextBox',
  'ImageLabel',
  'ImageButton',
  'ScrollingFrame',
  'CanvasGroup',
  'ViewportFrame',
  'VideoFrame',
]);
const TEXT = new Set(['TextLabel', 'TextButton', 'TextBox']);
const IMAGE = new Set(['ImageLabel', 'ImageButton']);

export const isGuiObject = (node) => GUI_OBJECTS.has(node.class);

function component(node, cls) {
  return node.children.find((c) => c.class === cls && c.props.Enabled !== false) ?? null;
}

function rgba(color, transparency = 0) {
  const [r, g, b] = color ?? [1, 1, 1];
  const a = Math.max(0, Math.min(1, 1 - transparency));
  return `rgba(${Math.round(r * 255)}, ${Math.round(g * 255)}, ${Math.round(b * 255)}, ${a})`;
}

function udim(value, whole, k) {
  return value ? whole * value[0] + value[1] * k : 0;
}

function sortedChildren(node) {
  const list = node.children.filter((c) => isGuiObject(c) && c.props.Visible !== false);
  const layout = component(node, 'UIListLayout') ?? component(node, 'UIGridLayout');
  if (layout) {
    const order = layout.props.SortOrder ?? 'LayoutOrder';
    list.sort((a, b) =>
      order === 'Name' ? a.name.localeCompare(b.name) : (a.props.LayoutOrder ?? 0) - (b.props.LayoutOrder ?? 0),
    );
  }
  return list;
}

// ---------------------------------------------------------------------------
// Text

const ENTITIES = { '&lt;': '<', '&gt;': '>', '&quot;': '"', '&apos;': "'", '&amp;': '&' };

function decode(text) {
  return text.replace(/&(lt|gt|quot|apos|amp);/g, (m) => ENTITIES[m]);
}

function parseColor(value) {
  if (!value) return null;
  const hex = /^#([0-9a-f]{6})$/i.exec(value.trim());
  if (hex) {
    const n = parseInt(hex[1], 16);
    return [(n >> 16) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
  }
  const rgb = /^rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)$/i.exec(value.trim());
  if (rgb) return [rgb[1] / 255, rgb[2] / 255, rgb[3] / 255];
  return null;
}

function attr(tag, name) {
  const m = new RegExp(`${name}\\s*=\\s*"([^"]*)"|${name}\\s*=\\s*'([^']*)'`, 'i').exec(tag);
  return m ? (m[1] ?? m[2]) : null;
}

// Rich text to styled runs; "\n" runs break lines.
function parseRich(text, rich) {
  if (!rich) {
    return [{ text, bold: false, italic: false, color: null, size: null, upper: false }];
  }
  const runs = [];
  const stack = [{ bold: false, italic: false, color: null, size: null, upper: false }];
  const re = /<!--[\s\S]*?-->|<\/?([a-z]+)([^>]*)>|([^<]+)/gi;
  let m;
  while ((m = re.exec(text))) {
    const top = stack[stack.length - 1];
    if (m[3] !== undefined) {
      runs.push({ ...top, text: decode(m[3]) });
      continue;
    }
    if (!m[1]) continue;
    const name = m[1].toLowerCase();
    const closing = m[0].startsWith('</');
    if (name === 'br') {
      runs.push({ ...top, text: '\n' });
      continue;
    }
    if (closing) {
      if (stack.length > 1) stack.pop();
      continue;
    }
    const next = { ...top };
    if (name === 'b') next.bold = true;
    if (name === 'i') next.italic = true;
    if (name === 'uc' || name === 'uppercase') next.upper = true;
    if (name === 'font') {
      next.color = parseColor(attr(m[2], 'color')) ?? next.color;
      const size = Number(attr(m[2], 'size'));
      if (size > 0) next.size = size;
      const weight = attr(m[2], 'weight');
      if (weight && /bold|heavy|700|800|900/i.test(weight)) next.bold = true;
    }
    stack.push(next);
  }
  return runs;
}

function graphemes(text) {
  return Array.from(text);
}

// Splits runs into lines of words that fit `width` (Infinity: no wrapping).
function layoutText(ctx, runs, font, px, lineHeight, width, wrap) {
  const lines = [[]];
  let lineWidth = 0;
  const pushLine = () => {
    lines.push([]);
    lineWidth = 0;
  };
  for (const run of runs) {
    const size = run.size ? run.size * (px / (run.baseSize ?? px)) : px;
    ctx.font = canvasFont(font, size, run.bold, run.italic);
    const content = run.upper ? run.text.toUpperCase() : run.text;
    const pieces = content.split(/(\n| +)/);
    for (const piece of pieces) {
      if (piece === '') continue;
      if (piece === '\n') {
        pushLine();
        continue;
      }
      const w = ctx.measureText(piece).width;
      const isSpace = /^ +$/.test(piece);
      if (wrap && !isSpace && lineWidth > 0 && lineWidth + w > width + 0.5) {
        // Drop trailing spaces before wrapping.
        const line = lines[lines.length - 1];
        while (line.length && /^ +$/.test(line[line.length - 1].text)) {
          lineWidth -= line.pop().width;
        }
        pushLine();
      }
      if (isSpace && lineWidth === 0 && lines.length > 1 && wrap) continue;
      if (wrap && !isSpace && w > width + 0.5) {
        // A word wider than a whole line: Roblox breaks it across lines wherever it has to
        // ("should a long unbroken word exceed the width", TextWrapped's docs), as the
        // simulator does. It starts on a line of its own (above).
        let chunk = '';
        for (const g of graphemes(piece)) {
          if (chunk !== '' && ctx.measureText(chunk + g).width > width + 0.5) {
            lines[lines.length - 1].push({ ...run, text: chunk, width: ctx.measureText(chunk).width, size });
            pushLine();
            chunk = '';
          }
          chunk += g;
        }
        const last = ctx.measureText(chunk).width;
        lines[lines.length - 1].push({ ...run, text: chunk, width: last, size });
        lineWidth += last;
        continue;
      }
      lines[lines.length - 1].push({ ...run, text: piece, width: w, size });
      lineWidth += w;
    }
  }
  const measured = lines.map((line) => {
    let w = 0;
    for (const piece of line) w += piece.width;
    const trimmed = line.length && /^ +$/.test(line[line.length - 1].text) ? line[line.length - 1].width : 0;
    return { pieces: line, width: w - trimmed };
  });
  const bounds = {
    width: Math.max(0, ...measured.map((l) => l.width)),
    height: measured.length * px * lineHeight,
  };
  return { lines: measured, bounds };
}

function textSetup(node, kk) {
  const p = node.props;
  let text = p.Text ?? '';
  const runs = parseRich(text, p.RichText);
  const limit = p.MaxVisibleGraphemes ?? -1;
  if (limit >= 0) {
    let left = limit;
    for (const run of runs) {
      const g = graphemes(run.text);
      if (g.length > left) {
        run.text = g.slice(0, left).join('');
        left = 0;
      } else {
        left -= g.length;
      }
    }
  }
  const baseSize = (p.TextSize ?? 14) * kk;
  for (const run of runs) run.baseSize = baseSize / kk;
  return { runs, font: p.FontFace, px: baseSize, lineHeight: p.LineHeight ?? 1 };
}

function fitText(ctx, node, kk, width, height) {
  const setup = textSetup(node, kk);
  const p = node.props;
  if (p.TextScaled) {
    const constraint = component(node, 'UITextSizeConstraint');
    const max = (constraint?.props.MaxTextSize ?? 100) * kk;
    const min = (constraint?.props.MinTextSize ?? 1) * kk;
    let lo = min;
    let hi = max;
    for (let i = 0; i < 18; i++) {
      const mid = (lo + hi) / 2;
      const scaledRuns = setup.runs.map((r) => ({ ...r, baseSize: mid / kk }));
      const t = layoutText(ctx, scaledRuns, setup.font, mid, setup.lineHeight, width, true);
      if (t.bounds.width <= width + 0.5 && t.bounds.height <= height + 0.5) lo = mid;
      else hi = mid;
    }
    setup.px = Math.floor(lo);
    setup.wrap = true;
  } else {
    setup.wrap = p.TextWrapped === true;
  }
  return {
    ...setup,
    ...layoutText(ctx, setup.runs, setup.font, setup.px, setup.lineHeight, width, setup.wrap),
  };
}

// ---------------------------------------------------------------------------
// Layout

function contentExtent(node) {
  const layout = component(node, 'UIListLayout');
  const kids = sortedChildren(node);
  if (layout) {
    const horizontal = layout.props.FillDirection === 'Horizontal';
    const gap = node._gap ?? 0;
    let along = 0;
    let across = 0;
    kids.forEach((c, i) => {
      along += (horizontal ? c._w : c._h) + (i > 0 ? gap : 0);
      across = Math.max(across, horizontal ? c._h : c._w);
    });
    return horizontal ? { w: along, h: across } : { w: across, h: along };
  }
  let w = 0;
  let h = 0;
  for (const c of kids) {
    const pos = c.props.Position ?? [0, 0, 0, 0];
    const anchor = c.props.AnchorPoint ?? [0, 0];
    w = Math.max(w, pos[1] * node._childK - anchor[0] * c._w + c._w);
    h = Math.max(h, pos[3] * node._childK - anchor[1] * c._h + c._h);
  }
  return { w, h };
}

function measure(ctx, node, pw, ph, k) {
  const p = node.props;
  const size = p.Size ?? [0, 0, 0, 0];
  const mode = p.SizeConstraint ?? 'RelativeXY';
  const wBase = mode === 'RelativeYY' ? ph : pw;
  const hBase = mode === 'RelativeXX' ? pw : ph;
  let w = wBase * size[0] + size[1] * k;
  let h = hBase * size[2] + size[3] * k;

  const aspect = component(node, 'UIAspectRatioConstraint');
  if (aspect) {
    const ratio = aspect.props.AspectRatio ?? 1;
    const dominant = aspect.props.DominantAxis ?? 'Width';
    if ((aspect.props.AspectType ?? 'FitWithinMaxSize') === 'FitWithinMaxSize') {
      if (w / Math.max(h, 1e-6) > ratio) w = h * ratio;
      else h = w / ratio;
    } else if (dominant === 'Width') h = w / ratio;
    else w = h * ratio;
  }
  const limits = component(node, 'UISizeConstraint');
  if (limits) {
    const [minX, minY] = limits.props.MinSize ?? [0, 0];
    const [maxX, maxY] = limits.props.MaxSize ?? [Infinity, Infinity];
    w = Math.min(Math.max(w, minX), maxX ?? Infinity);
    h = Math.min(Math.max(h, minY), maxY ?? Infinity);
  }

  const scale = component(node, 'UIScale')?.props.Scale ?? 1;
  w *= scale;
  h *= scale;
  const kk = k * scale;
  node._k = kk;
  node._childK = kk;

  const measureInside = () => {
    const pad = component(node, 'UIPadding');
    node._pad = {
      l: pad ? udim(pad.props.PaddingLeft, w, kk) : 0,
      r: pad ? udim(pad.props.PaddingRight, w, kk) : 0,
      t: pad ? udim(pad.props.PaddingTop, h, kk) : 0,
      b: pad ? udim(pad.props.PaddingBottom, h, kk) : 0,
    };
    const cw = Math.max(0, w - node._pad.l - node._pad.r);
    const ch = Math.max(0, h - node._pad.t - node._pad.b);
    node._cw = cw;
    node._ch = ch;
    const layout = component(node, 'UIListLayout');
    if (layout) {
      const horizontal = layout.props.FillDirection === 'Horizontal';
      node._gap = udim(layout.props.Padding, horizontal ? cw : ch, kk);
    }
    for (const child of sortedChildren(node)) {
      measure(ctx, child, cw, ch, kk);
    }
    if (TEXT.has(node.class)) {
      const autoX = p.AutomaticSize === 'X' || p.AutomaticSize === 'XY';
      node._text = fitText(ctx, node, kk, autoX && !p.TextWrapped ? Infinity : cw, ch);
    }
  };
  measureInside();

  const auto = p.AutomaticSize ?? 'None';
  if (auto !== 'None') {
    const extent = contentExtent(node);
    let ew = extent.w;
    let eh = extent.h;
    if (node._text) {
      ew = Math.max(ew, node._text.bounds.width);
      eh = Math.max(eh, node._text.bounds.height);
    }
    const nw = auto === 'X' || auto === 'XY' ? Math.max(w, ew + node._pad.l + node._pad.r) : w;
    const nh = auto === 'Y' || auto === 'XY' ? Math.max(h, eh + node._pad.t + node._pad.b) : h;
    if (nw !== w || nh !== h) {
      w = nw;
      h = nh;
      measureInside();
    }
  }
  node._w = w;
  node._h = h;
}

function arrange(node, x, y) {
  node._x = x;
  node._y = y;
  const kids = sortedChildren(node);
  let cx = x + node._pad.l;
  let cy = y + node._pad.t;
  if (node.class === 'ScrollingFrame') {
    const canvas = node.props.CanvasPosition ?? [0, 0];
    cx -= canvas[0] * node._childK;
    cy -= canvas[1] * node._childK;
  }
  const layout = component(node, 'UIListLayout');
  if (layout) {
    const horizontal = layout.props.FillDirection === 'Horizontal';
    const hAlign = layout.props.HorizontalAlignment ?? 'Left';
    const vAlign = layout.props.VerticalAlignment ?? 'Top';
    const extent = contentExtent(node);
    let along;
    if (horizontal) {
      along = hAlign === 'Center' ? (node._cw - extent.w) / 2 : hAlign === 'Right' ? node._cw - extent.w : 0;
    } else {
      along = vAlign === 'Center' ? (node._ch - extent.h) / 2 : vAlign === 'Bottom' ? node._ch - extent.h : 0;
    }
    for (const child of kids) {
      if (horizontal) {
        const off = vAlign === 'Center' ? (node._ch - child._h) / 2 : vAlign === 'Bottom' ? node._ch - child._h : 0;
        arrange(child, cx + along, cy + off);
        along += child._w + node._gap;
      } else {
        const off = hAlign === 'Center' ? (node._cw - child._w) / 2 : hAlign === 'Right' ? node._cw - child._w : 0;
        arrange(child, cx + off, cy + along);
        along += child._h + node._gap;
      }
    }
    return;
  }
  for (const child of kids) {
    const pos = child.props.Position ?? [0, 0, 0, 0];
    const anchor = child.props.AnchorPoint ?? [0, 0];
    const px = cx + node._cw * pos[0] + pos[1] * node._childK - anchor[0] * child._w;
    const py = cy + node._ch * pos[2] + pos[3] * node._childK - anchor[1] * child._h;
    arrange(child, px, py);
  }
}

// ---------------------------------------------------------------------------
// Drawing

function roundedRect(ctx, x, y, w, h, r) {
  const radius = Math.max(0, Math.min(r, w / 2, h / 2));
  ctx.beginPath();
  if (radius <= 0.01) {
    ctx.rect(x, y, w, h);
    return;
  }
  ctx.moveTo(x + radius, y);
  ctx.arcTo(x + w, y, x + w, y + h, radius);
  ctx.arcTo(x + w, y + h, x, y + h, radius);
  ctx.arcTo(x, y + h, x, y, radius);
  ctx.arcTo(x, y, x + w, y, radius);
  ctx.closePath();
}

function cornerRadius(node) {
  const corner = component(node, 'UICorner');
  if (!corner) return 0;
  const r = corner.props.CornerRadius ?? corner.props.TopLeftRadius ?? [0, 8];
  return r[0] * Math.min(node._w, node._h) + r[1] * node._k;
}

function sampleSequence(keys, t) {
  if (!keys || keys.length === 0) return null;
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

// A fill style for the element's background (or text), with any UIGradient applied.
function fillStyle(ctx, node, color, transparency) {
  const gradient = component(node, 'UIGradient');
  if (!gradient) return rgba(color, transparency);
  const rot = ((gradient.props.Rotation ?? 0) * Math.PI) / 180;
  const cx = node._x + node._w / 2;
  const cy = node._y + node._h / 2;
  const half = (Math.abs(Math.cos(rot)) * node._w + Math.abs(Math.sin(rot)) * node._h) / 2;
  const offset = gradient.props.Offset ?? [0, 0];
  const ox = offset[0] * node._w;
  const oy = offset[1] * node._h;
  const g = ctx.createLinearGradient(
    cx + ox - Math.cos(rot) * half,
    cy + oy - Math.sin(rot) * half,
    cx + ox + Math.cos(rot) * half,
    cy + oy + Math.sin(rot) * half,
  );
  const colorKeys = gradient.props.Color ?? [
    [0, [1, 1, 1]],
    [1, [1, 1, 1]],
  ];
  const alphaKeys = gradient.props.Transparency ?? [
    [0, 0, 0],
    [1, 0, 0],
  ];
  for (let i = 0; i <= 16; i++) {
    const t = i / 16;
    const c = sampleSequence(colorKeys, t);
    const a = sampleSequence(alphaKeys, t) ?? 0;
    const mixed = [c[0] * color[0], c[1] * color[1], c[2] * color[2]];
    g.addColorStop(t, rgba(mixed, 1 - (1 - transparency) * (1 - a)));
  }
  return g;
}

function borderStroke(node) {
  return node.children.find(
    (c) =>
      c.class === 'UIStroke' &&
      c.props.Enabled !== false &&
      ((c.props.ApplyStrokeMode ?? 'Contextual') === 'Border' || !TEXT.has(node.class)),
  );
}

function textStroke(node) {
  return node.children.find(
    (c) =>
      c.class === 'UIStroke' &&
      c.props.Enabled !== false &&
      (c.props.ApplyStrokeMode ?? 'Contextual') === 'Contextual' &&
      TEXT.has(node.class),
  );
}

function drawText(ctx, node) {
  const t = node._text;
  if (!t) return;
  const p = node.props;
  const transparency = p.TextTransparency ?? 0;
  if (transparency >= 1) return;
  const x0 = node._x + node._pad.l;
  const y0 = node._y + node._pad.t;
  const cw = node._cw;
  const ch = node._ch;
  const lineBox = t.px * t.lineHeight;
  const total = t.lines.length * lineBox;
  const yAlign = p.TextYAlignment ?? 'Center';
  let y = yAlign === 'Top' ? y0 : yAlign === 'Bottom' ? y0 + ch - total : y0 + (ch - total) / 2;
  const xAlign = p.TextXAlignment ?? 'Center';
  const stroke = textStroke(node);
  const legacyStroke = (p.TextStrokeTransparency ?? 1) < 1;
  const baseColor = p.TextColor3 ?? [0, 0, 0];
  ctx.textBaseline = 'middle';
  for (const line of t.lines) {
    let x = xAlign === 'Left' ? x0 : xAlign === 'Right' ? x0 + cw - line.width : x0 + (cw - line.width) / 2;
    const mid = y + lineBox / 2;
    for (const piece of line.pieces) {
      ctx.font = canvasFont(t.font, piece.size, piece.bold, piece.italic);
      const color = piece.color ?? baseColor;
      if (stroke) {
        const thickness = (stroke.props.Thickness ?? 1) * node._k;
        ctx.lineJoin = (stroke.props.LineJoinMode ?? 'Round') === 'Miter' ? 'miter' : 'round';
        ctx.lineWidth = thickness * 2;
        ctx.strokeStyle = rgba(stroke.props.Color ?? [0, 0, 0], Math.max(stroke.props.Transparency ?? 0, transparency));
        ctx.strokeText(piece.text, x, mid);
      } else if (legacyStroke) {
        ctx.lineJoin = 'round';
        ctx.lineWidth = 2 * node._k;
        ctx.strokeStyle = rgba(p.TextStrokeColor3 ?? [0, 0, 0], p.TextStrokeTransparency ?? 1);
        ctx.strokeText(piece.text, x, mid);
      }
      ctx.fillStyle = component(node, 'UIGradient')
        ? fillStyle(ctx, node, color, transparency)
        : rgba(color, transparency);
      ctx.fillText(piece.text, x, mid);
      x += piece.width;
    }
    y += lineBox;
  }
}

function drawNode(ctx, node, images) {
  const p = node.props;
  if (p.Visible === false) return;
  ctx.save();
  if (node.class === 'CanvasGroup') {
    ctx.globalAlpha *= 1 - (p.GroupTransparency ?? 0);
  }
  const rotation = p.Rotation ?? 0;
  if (rotation) {
    const cx = node._x + node._w / 2;
    const cy = node._y + node._h / 2;
    ctx.translate(cx, cy);
    ctx.rotate((rotation * Math.PI) / 180);
    ctx.translate(-cx, -cy);
  }
  const radius = cornerRadius(node);
  // UIShadow: a copy of the element's rounded shape behind it, offset, spread and blurred.
  for (const shadow of node.children.filter((c) => c.class === 'UIShadow' && c.props.Enabled !== false)) {
    const s = shadow.props;
    const offset = s.Offset ?? [0, 0, 0, 0];
    const spread = s.Spread ?? [0, 0, 0, 0];
    const dx = offset[0] * node._w + offset[1] * node._k;
    const dy = offset[2] * node._h + offset[3] * node._k;
    const gx = spread[0] * node._w + spread[1] * node._k;
    const gy = spread[2] * node._h + spread[3] * node._k;
    const blurValue = s.BlurRadius ?? [0, 0];
    const blur = blurValue[0] * Math.min(node._w, node._h) + blurValue[1] * node._k;
    ctx.save();
    if (blur > 0) ctx.filter = `blur(${(blur / 2).toFixed(1)}px)`;
    roundedRect(ctx, node._x + dx - gx / 2, node._y + dy - gy / 2, node._w + gx, node._h + gy, radius);
    ctx.fillStyle = rgba(s.Color ?? [0, 0, 0], s.Transparency ?? 0);
    ctx.fill();
    ctx.restore();
  }
  const bgT = p.BackgroundTransparency ?? 0;
  if (bgT < 1) {
    roundedRect(ctx, node._x, node._y, node._w, node._h, radius);
    ctx.fillStyle = fillStyle(ctx, node, p.BackgroundColor3 ?? [1, 1, 1], bgT);
    ctx.fill();
  }
  if (IMAGE.has(node.class) && images) {
    const image = images(p.Image);
    const it = p.ImageTransparency ?? 0;
    if (image && it < 1) {
      ctx.save();
      roundedRect(ctx, node._x, node._y, node._w, node._h, radius);
      ctx.clip();
      ctx.globalAlpha *= 1 - it;
      ctx.drawImage(image, node._x, node._y, node._w, node._h);
      ctx.restore();
    }
  }
  const border = borderStroke(node);
  if (border) {
    const thickness = (border.props.Thickness ?? 1) * node._k;
    const position = border.props.BorderStrokePosition ?? 'Outer';
    const grow = position === 'Outer' ? thickness / 2 : position === 'Inner' ? -thickness / 2 : 0;
    roundedRect(ctx, node._x - grow, node._y - grow, node._w + 2 * grow, node._h + 2 * grow, radius + grow);
    ctx.lineWidth = thickness;
    ctx.strokeStyle = rgba(border.props.Color ?? [0, 0, 0], border.props.Transparency ?? 0);
    ctx.stroke();
  } else if ((p.BorderSizePixel ?? 0) > 0 && bgT < 1) {
    ctx.lineWidth = p.BorderSizePixel * node._k;
    ctx.strokeStyle = rgba(p.BorderColor3 ?? [0.1, 0.16, 0.2], bgT);
    ctx.strokeRect(node._x, node._y, node._w, node._h);
  }
  if (TEXT.has(node.class)) {
    drawText(ctx, node);
  }

  const clips = p.ClipsDescendants || node.class === 'ScrollingFrame' || node.class === 'CanvasGroup';
  if (clips) {
    ctx.save();
    roundedRect(ctx, node._x, node._y, node._w, node._h, node.class === 'CanvasGroup' ? radius : 0);
    ctx.clip();
  }
  const kids = sortedChildren(node)
    .map((c, i) => ({ c, i }))
    .sort((a, b) => (a.c.props.ZIndex ?? 1) - (b.c.props.ZIndex ?? 1) || a.i - b.i)
    .map((e) => e.c);
  for (const child of kids) drawNode(ctx, child, images);
  if (clips) ctx.restore();

  if (node.class === 'ScrollingFrame') {
    const extent = contentExtent(node);
    const thickness = (p.ScrollBarThickness ?? 12) * node._k;
    if (extent.h > node._ch + 1 && thickness > 0) {
      const share = node._ch / extent.h;
      ctx.fillStyle = rgba(p.ScrollBarImageColor3 ?? [1, 1, 1], p.ScrollBarImageTransparency ?? 0);
      roundedRect(ctx, node._x + node._w - thickness, node._y, thickness, node._h * share, thickness / 2);
      ctx.fill();
    }
  }
  ctx.restore();
}

// Lays out and draws one layer (a ScreenGui, SurfaceGui or BillboardGui) into a rect.
export function drawLayer(ctx, layer, rect, images) {
  const root = {
    class: 'Frame',
    name: layer.name,
    props: { Size: [0, rect.w, 0, rect.h], BackgroundTransparency: 1 },
    children: layer.children,
  };
  measure(ctx, root, rect.w, rect.h, 1);
  arrange(root, rect.x, rect.y);
  const kids = sortedChildren(root)
    .map((c, i) => ({ c, i }))
    .sort((a, b) => (a.c.props.ZIndex ?? 1) - (b.c.props.ZIndex ?? 1) || a.i - b.i)
    .map((e) => e.c);
  for (const child of kids) drawNode(ctx, child, images);
}

export async function loadFontsFor(tree) {
  const fonts = fontsIn(tree);
  const seen = new Set();
  await Promise.all(
    fonts
      .filter((f) => {
        const key = `${f.family}|${f.weight}|${f.style}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      })
      .map((f) => loadFont(f)),
  );
  // Bold runs in rich text use weight 700.
  await Promise.all(
    [...seen].map((key) => {
      const [family, , style] = key.split('|');
      return loadFont({ family, weight: 'Bold', style });
    }),
  );
}

// Every ScreenGui in a PlayerGui, in DisplayOrder, drawn over the screen, in the area its
// ScreenInsets gives it: the whole screen (None); the device safe area, top bar and all
// (DeviceSafeInsets); the free part of the top bar row (TopbarSafeInsets); or, by default,
// the safe area below the top bar (`safe`, CoreUISafeInsets).
export function drawScreenGuis(ctx, playerGui, screen, safe, images, topbar) {
  if (!playerGui) return;
  const layers = playerGui.children
    .filter((c) => c.class === 'ScreenGui' && c.props.Enabled !== false)
    .map((c, i) => ({ c, i }))
    .sort((a, b) => (a.c.props.DisplayOrder ?? 0) - (b.c.props.DisplayOrder ?? 0) || a.i - b.i)
    .map((e) => e.c);
  for (const layer of layers) {
    const insets = layer.props.ScreenInsets ?? 'CoreUISafeInsets';
    const rect =
      insets === 'None'
        ? { x: 0, y: 0, w: screen.w, h: screen.h }
        : insets === 'DeviceSafeInsets'
          ? { x: safe.x, y: 0, w: safe.w, h: screen.h }
          : insets === 'TopbarSafeInsets'
            ? topbar ?? { x: 0, y: 0, w: 0, h: 0 }
            : safe;
    drawLayer(ctx, layer, rect, images);
  }
}
