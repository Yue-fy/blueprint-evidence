/**
 * Phone photos before upload: no bigger IN BYTES than a slow booth network carries. A JPEG or PNG up to 8 MB goes as it
 * is, not even decoded (the server turns it upright from its EXIF tag and makes its own reading copy): Stoa's example
 * PNGs are 10,000-14,000 px but 1-4 MB, and the 4,096 px limit this had until 14 Nov 21:20 turned the DfE sheet's
 * Multi-Purpose Hall from 247.73 m² (traced at the printed 1:200; gold 246.37) into '≈ 298 Rough guess'; decoding a
 * 139 MP image only to measure it can also run an iPhone tab out of memory. Over 8 MB (a 48 MP photo, a huge PNG,
 * WebP / BMP / GIF): re-encoded to JPEG at <= 4096 px (photos) or <= 8192 px (PNG: drawings and scans) on the long
 * side. HEIC (or anything the browser cannot decode) is sent untouched: the server converts it.
 */
const MAX_SIDE = 4096;
const MAX_SIDE_PNG = 8192;
const MAX_BYTES = 8e6;

export interface Prepared {
  file: File;
  /** what was done, in words ("made smaller: 8064 × 6048 → 4096 × 3072"), "" = sent as taken */
  note: string;
  width?: number;
  height?: number;
}

export async function prepImage(f: File): Promise<Prepared> {
  const isImage = /^image\//.test(f.type) || /\.(jpe?g|png|webp|bmp|gif|heic|heif|tiff?)$/i.test(f.name);
  if (!isImage) return { file: f, note: "" };
  if (f.size <= MAX_BYTES && /jpe?g|png/i.test(f.type || f.name)) return { file: f, note: "" };
  let bmp: ImageBitmap;
  try {
    bmp = await createImageBitmap(f, { imageOrientation: "from-image" });
  } catch {
    return { file: f, note: "" };
  }
  const long = Math.max(bmp.width, bmp.height);
  const k = Math.min(1, (/png/i.test(f.type || f.name) ? MAX_SIDE_PNG : MAX_SIDE) / long);
  const w = Math.round(bmp.width * k), h = Math.round(bmp.height * k);
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  if (!ctx) {
    bmp.close();
    return { file: f, note: "" };
  }
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(bmp, 0, 0, w, h);
  const from = `${bmp.width} × ${bmp.height}`;
  bmp.close();
  const blob = await new Promise<Blob | null>((res) => canvas.toBlob(res, "image/jpeg", 0.9));
  if (!blob) return { file: f, note: "" };
  const name = f.name.replace(/\.[^.]+$/, "") + ".jpg";
  return {
    file: new File([blob], name || "photo.jpg", { type: "image/jpeg" }),
    note: k < 1 ? `made smaller for the upload: ${from} → ${w} × ${h} px` : `converted to JPEG (${(f.size / 1e6).toFixed(1)} → ${(blob.size / 1e6).toFixed(1)} MB)`,
    width: w,
    height: h,
  };
}
