/**
 * The calm "product render" look shared by both 3D panes (captain 15:40): ambient occlusion, a light ground with a soft
 * contact shadow under the model, clay-white materials and a soft rim glow on highlighted elements.
 *
 * Performance: while the camera moves (orbit, damping, an eased move) every frame is a plain MSAA render, exactly as
 * before. Once the view has been still for ~150 ms, ONE extra frame is rendered through an EffectComposer
 * (RenderPass → GTAOPass → OutputPass) into a 4× multisampled half-float target, and that frame stays on screen until
 * the next move. So orbiting costs what it cost before, and the AO costs one frame per stop.
 * Everything here is three.js's own example code (MIT); no new dependency.
 */
import * as THREE from "three";
import { EffectComposer } from "three/examples/jsm/postprocessing/EffectComposer.js";
import { RenderPass } from "three/examples/jsm/postprocessing/RenderPass.js";
import { GTAOPass } from "three/examples/jsm/postprocessing/GTAOPass.js";
import { OutputPass } from "three/examples/jsm/postprocessing/OutputPass.js";
import { OutlinePass } from "three/examples/jsm/postprocessing/OutlinePass.js";

/** `?ao=0` in the URL turns the AO frame off (a fallback for a weak GPU during a demo). */
const AO_ON = !/[?&]ao=0\b/.test(location.search);
const IDLE_MS = 150;

/** Mix a material's colours toward clay white (0 = the model's own colours, 1 = all clay); shared by all materials. */
export const CLAY = { value: 0 };
/** Clay for glass (transparent buckets): a little of their own tint stays. */
export const CLAY_GLASS = { value: 0 };

/** Clay-white diffuse (the model's colours mixed toward a warm white; the luminance keeps a hint of contrast). */
export function clayify(mat: THREE.Material, glass = false, furniture = false) {
  // furniture and fixtures: the same clay, a step darker than the walls (captain 16:00)
  const tint = furniture ? "vec3(0.80, 0.795, 0.78)" : "vec3(0.93, 0.925, 0.91)";
  mat.onBeforeCompile = (sh) => {
    sh.uniforms.uClay = glass ? CLAY_GLASS : CLAY;
    sh.fragmentShader = sh.fragmentShader
      .replace("void main() {", "uniform float uClay;\nvoid main() {")
      .replace("#include <color_fragment>", `#include <color_fragment>
      float clayL = dot(diffuseColor.rgb, vec3(0.299, 0.587, 0.114));
      diffuseColor.rgb = mix(diffuseColor.rgb, ${tint} * (0.86 + 0.14 * clayL), uClay);`);
  };
  mat.customProgramCacheKey = () => `clay${glass ? "-glass" : ""}${furniture ? "-furn" : ""}`;
}

/** A soft fresnel rim on a highlight material: its silhouette glows a lighter accent, cheap on every frame. */
export function rimGlow(mat: THREE.MeshLambertMaterial, strength = 0.9) {
  mat.onBeforeCompile = (sh) => {
    sh.uniforms.uRim = { value: strength };
    sh.fragmentShader = sh.fragmentShader
      .replace("void main() {", "uniform float uRim;\nvoid main() {")
      .replace("#include <emissivemap_fragment>", `#include <emissivemap_fragment>
      float rimF = 1.0 - abs(dot(normalize(normal), normalize(vViewPosition)));
      totalEmissiveRadiance += vec3(0.35, 0.62, 1.0) * pow(rimF, 3.0) * uRim;`);
  };
  mat.customProgramCacheKey = () => `rim${strength}`;
}

export class Look3D {
  private composer: EffectComposer;
  private gtao: GTAOPass;
  private glow: OutlinePass;
  private lastFast = 0;
  private refined = true;
  private ground = new THREE.Group();
  private groundMat = new THREE.MeshBasicMaterial({ transparent: true, depthWrite: false, color: 0xffffff, opacity: 0.7 });
  private shadowMat = new THREE.MeshBasicMaterial({ transparent: true, depthWrite: false, color: 0x1b2433, opacity: 0.62 });
  /** frames drawn while moving / AO frames (for the fps note) */
  stats = { fast: 0, refined: 0, lastRefineMs: 0 };

  constructor(private renderer: THREE.WebGLRenderer, private scene: THREE.Scene, private camera: THREE.PerspectiveCamera) {
    const size = renderer.getDrawingBufferSize(new THREE.Vector2());
    const rt = new THREE.WebGLRenderTarget(Math.max(1, size.x), Math.max(1, size.y), { type: THREE.HalfFloatType, samples: 4 });
    this.composer = new EffectComposer(renderer, rt);
    this.composer.addPass(new RenderPass(scene, camera));
    this.gtao = new GTAOPass(scene, camera, Math.max(1, size.x), Math.max(1, size.y));
    this.gtao.blendIntensity = 0.9;
    this.gtao.updateGtaoMaterial({ radius: 0.9, distanceExponent: 1.6, thickness: 1.2, scale: 1.15, samples: 16, distanceFallOff: 1 });
    this.gtao.updatePdMaterial({ lumaPhi: 10, depthPhi: 2, normalPhi: 3, radius: 6, rings: 2, samples: 16 });
    // only OPAQUE meshes cast AO: ghosted/faded storeys, glass, highlights and the ground are left out of the G-buffer
    // (the pass's own override hides points and lines only)
    const g = this.gtao as unknown as { _overrideVisibility: () => void; _visibilityCache: THREE.Object3D[] };
    const own = g._overrideVisibility.bind(this.gtao);
    g._overrideVisibility = () => {
      own();
      this.scene.traverse((o) => {
        const m = o as THREE.Mesh;
        if (!m.isMesh || !m.visible) return;
        const mat = m.material as THREE.Material;
        if (mat && (mat.transparent || mat.opacity < 1 || m.userData.noAO)) {
          m.visible = false;
          g._visibilityCache.push(m);
        }
      });
    };
    this.composer.addPass(this.gtao);
    // a soft accent halo around the answer's elements (still frames only, like the AO)
    this.glow = new OutlinePass(new THREE.Vector2(Math.max(1, size.x), Math.max(1, size.y)), scene, camera);
    this.glow.visibleEdgeColor.set("#2f8cff");
    this.glow.hiddenEdgeColor.set("#2f8cff");
    this.glow.edgeStrength = 2.2;
    this.glow.edgeGlow = 0.9;
    this.glow.edgeThickness = 1.6;
    this.glow.enabled = false;
    this.composer.addPass(this.glow);
    this.composer.addPass(new OutputPass());
    this.ground.renderOrder = -1;
    scene.add(this.ground);
  }

  /** The AO's G-buffer must honour a storey cut too (the override material replaces each mesh's own planes). */
  setClip(planes: THREE.Plane[] | null) {
    this.gtao.normalMaterial.clippingPlanes = planes;
    this.gtao.normalMaterial.needsUpdate = true;
  }

  /** The halo goes around these objects (none = off). */
  setGlow(objects: THREE.Object3D[]) {
    this.glow.selectedObjects = objects;
    this.glow.enabled = AO_ON && objects.length > 0;
    this.refined = false;
  }

  setSize(w: number, h: number) {
    this.composer.setPixelRatio(this.renderer.getPixelRatio());
    this.composer.setSize(w, h);
    this.refined = false;
  }

  /** Light or dark page: the ground is a slightly lighter disc than the canvas, the shadow a soft dark; the backdrop a
   * subtle sky gradient from the canvas colour (lighter and a touch cooler at the top). */
  theme(dark: boolean, base?: THREE.Color) {
    if (base) {
      const c = document.createElement("canvas");
      c.width = 2;
      c.height = 256;
      const ctx = c.getContext("2d")!;
      const top = base.clone().lerp(new THREE.Color(dark ? "#3a404a" : "#ffffff"), dark ? 0.35 : 0.6).lerp(new THREE.Color(dark ? "#2b3646" : "#e3ecf7"), 0.18);
      const g = ctx.createLinearGradient(0, 0, 0, 256);
      g.addColorStop(0, `#${top.getHexString()}`);
      g.addColorStop(1, `#${base.getHexString()}`);
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, 2, 256);
      const old = this.scene.background as THREE.Texture | null;
      const tex = new THREE.CanvasTexture(c);
      tex.colorSpace = THREE.SRGBColorSpace;
      this.scene.background = tex;
      if (old && (old as THREE.Texture).isTexture) old.dispose();
    }
    this.groundMat.color.set(dark ? 0x3a3a3c : 0xffffff);
    this.groundMat.opacity = dark ? 0.18 : 0.75;
    this.shadowMat.color.set(dark ? 0x000000 : 0x1b2433);
    this.shadowMat.opacity = dark ? 0.8 : 0.62;
    this.refined = false;
  }

  /** A frame while something moves: the plain MSAA render (as fast as before). */
  fast() {
    this.renderer.render(this.scene, this.camera);
    this.lastFast = performance.now();
    this.refined = false;
    this.stats.fast++;
  }

  /** Called on every animation frame with nothing to redraw: the AO frame once the view has been still for a moment. */
  idle(): boolean {
    if (this.refined || !AO_ON || performance.now() - this.lastFast < IDLE_MS) return false;
    this.refine();
    return true;
  }

  /** The AO frame now (also for snapshots). */
  refine() {
    const t0 = performance.now();
    if (AO_ON) this.composer.render();
    else this.renderer.render(this.scene, this.camera);
    this.refined = true;
    this.stats.refined++;
    this.stats.lastRefineMs = performance.now() - t0;
  }

  get aoOn() { return AO_ON; }

  /**
   * The ground under `target` (a light disc fading out) and its contact shadow: a top-down silhouette of the model's
   * lowest `contactH` metres, rendered once into a small target, blurred on a 2D canvas and laid on the ground.
   */
  setGround(target: THREE.Object3D, contactH = 2.5) {
    for (const o of this.ground.children) {
      const m = o as THREE.Mesh;
      m.geometry.dispose();
      (m.material as THREE.MeshBasicMaterial).map?.dispose();
      (m.material as THREE.Material).dispose();
    }
    this.ground.clear();
    const box = new THREE.Box3().setFromObject(target);
    if (box.isEmpty()) return;
    const size = box.getSize(new THREE.Vector3());
    const c = box.getCenter(new THREE.Vector3());
    const span = Math.max(size.x, size.z);
    const pad = Math.max(4, span * 0.2);
    const w = size.x + 2 * pad, h = size.z + 2 * pad;
    const res = 384;
    const W = Math.max(8, Math.round((res * w) / Math.max(w, h))), H = Math.max(8, Math.round((res * h) / Math.max(w, h)));
    const y0 = box.min.y;
    // the floor disc: radial fade so it melts into the canvas (no hard edge, no horizon line)
    const R = Math.max(w, h) * 1.6;
    const disc = document.createElement("canvas");
    disc.width = disc.height = 256;
    const dctx = disc.getContext("2d")!;
    const grad = dctx.createRadialGradient(128, 128, 0, 128, 128, 128);
    grad.addColorStop(0, "rgba(255,255,255,1)");
    grad.addColorStop(0.45, "rgba(255,255,255,0.85)");
    grad.addColorStop(1, "rgba(255,255,255,0)");
    dctx.fillStyle = grad;
    dctx.fillRect(0, 0, 256, 256);
    const discTex = new THREE.CanvasTexture(disc);
    discTex.colorSpace = THREE.SRGBColorSpace;
    const gm = this.groundMat.clone();
    gm.map = discTex; // white × alpha: the material colour with the texture's alpha
    gm.needsUpdate = true;
    // keep the theme values on the shared material and copy them on theme changes
    this.groundMat = gm;
    const discMesh = new THREE.Mesh(new THREE.PlaneGeometry(2 * R, 2 * R).rotateX(-Math.PI / 2), gm);
    discMesh.position.set(c.x, y0 - 0.04, c.z);
    discMesh.renderOrder = -2;
    discMesh.userData.noAO = true;
    discMesh.raycast = () => {};
    this.ground.add(discMesh);
    // the silhouette: everything but `target` hidden, black override clipped to the lowest contactH metres
    const rt = new THREE.WebGLRenderTarget(W, H);
    const cam = new THREE.OrthographicCamera(-w / 2, w / 2, h / 2, -h / 2, 0.1, size.y + 20);
    cam.up.set(0, 0, -1);
    cam.position.set(c.x, box.max.y + 10, c.z);
    cam.lookAt(c.x, y0, c.z);
    cam.updateMatrixWorld();
    const black = new THREE.MeshBasicMaterial({ color: 0x000000, side: THREE.DoubleSide,
      clippingPlanes: [new THREE.Plane(new THREE.Vector3(0, -1, 0), y0 + contactH)] });
    const hidden: THREE.Object3D[] = [];
    for (const ch of this.scene.children) if (ch !== target && ch.visible) { ch.visible = false; hidden.push(ch); }
    const vis: THREE.Object3D[] = [];
    target.traverse((o) => { if (!o.visible) { o.visible = true; vis.push(o); } });
    const prevRT = this.renderer.getRenderTarget();
    const prevClear = this.renderer.getClearColor(new THREE.Color());
    const prevAlpha = this.renderer.getClearAlpha();
    const prevLocal = this.renderer.localClippingEnabled;
    this.renderer.localClippingEnabled = true;
    this.renderer.setRenderTarget(rt);
    this.renderer.setClearColor(0xffffff, 1);
    this.renderer.clear();
    const bgTex = this.scene.background;
    this.scene.background = null; // the sky gradient must not paint the silhouette target
    this.scene.overrideMaterial = black;
    this.renderer.render(this.scene, cam);
    this.scene.overrideMaterial = null;
    this.scene.background = bgTex;
    const px = new Uint8Array(W * H * 4);
    this.renderer.readRenderTargetPixels(rt, 0, 0, W, H, px);
    this.renderer.setRenderTarget(prevRT);
    this.renderer.setClearColor(prevClear, prevAlpha);
    this.renderer.localClippingEnabled = prevLocal;
    for (const o of hidden) o.visible = true;
    for (const o of vis) o.visible = false;
    rt.dispose();
    black.dispose();
    // alpha = ink; rows flipped so the canvas top is the far (−z) side, as the plane's v = 1 edge
    const sil = document.createElement("canvas");
    sil.width = W;
    sil.height = H;
    const sctx = sil.getContext("2d")!;
    const img = sctx.createImageData(W, H);
    for (let y = 0; y < H; y++) {
      for (let x = 0; x < W; x++) {
        const s = ((H - 1 - y) * W + x) * 4, d = (y * W + x) * 4;
        img.data[d] = img.data[d + 1] = img.data[d + 2] = 255;
        img.data[d + 3] = 255 - px[s];
      }
    }
    sctx.putImageData(img, 0, 0);
    // two blurs: a tight dark contact line and a wide soft halo (canvas filter blur; the small/large rescale is the
    // fallback where ctx.filter is unsupported)
    const out = document.createElement("canvas");
    out.width = W;
    out.height = H;
    const octx = out.getContext("2d")!;
    const pxPerM = W / w;
    const blur = (r: number, alpha: number) => {
      octx.globalAlpha = alpha;
      if (typeof (octx as { filter?: unknown }).filter === "string") {
        octx.filter = `blur(${Math.max(1, r * pxPerM).toFixed(1)}px)`;
        octx.drawImage(sil, 0, 0);
        octx.filter = "none";
      } else {
        const t = document.createElement("canvas");
        const k = Math.max(2, r * pxPerM);
        t.width = Math.max(2, Math.round(W / k));
        t.height = Math.max(2, Math.round(H / k));
        t.getContext("2d")!.drawImage(sil, 0, 0, t.width, t.height);
        octx.imageSmoothingEnabled = true;
        octx.drawImage(t, 0, 0, W, H);
      }
    };
    // the wide halo twice: a blur halves the alpha at the footprint's edge, the second copy brings the fringe back
    blur(Math.max(0.3, span * 0.008), 0.6);
    blur(Math.max(1.5, span * 0.06), 0.55);
    blur(Math.max(1.5, span * 0.06), 0.45);
    octx.globalAlpha = 1;
    const shTex = new THREE.CanvasTexture(out);
    const sm = this.shadowMat.clone();
    sm.map = shTex; // white ink × alpha: the shadow colour where the model stands
    sm.needsUpdate = true;
    this.shadowMat = sm;
    const shMesh = new THREE.Mesh(new THREE.PlaneGeometry(w, h).rotateX(-Math.PI / 2), sm);
    shMesh.position.set(c.x, y0 - 0.02, c.z);
    shMesh.renderOrder = -1;
    shMesh.userData.noAO = true;
    shMesh.raycast = () => {};
    this.ground.add(shMesh);
    this.refined = false;
  }

  /** Show or hide the ground (e.g. a storey filter shows one floor floating: the shadow still marks the footprint). */
  setGroundVisible(on: boolean) {
    this.ground.visible = on;
    this.refined = false;
  }
}
