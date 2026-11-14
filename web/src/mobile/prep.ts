/**
 * Phone photos before upload: upright (EXIF orientation applied by the browser's decoder) and no bigger than the
 * readers need. A 12 MP photo (4032 px) goes as it is (the server turns it upright from its EXIF tag); a 48 MP photo,
 * a huge PNG or anything over 8 MB is re-encoded to JPEG at <= 4096 px on the long side, which keeps printed text
 * readable for OCR and cuts the upload on a slow booth network to ~1-2 MB. HEIC (or anything the browser cannot
 * decode) is sent untouched: the server converts it.
 */
const MAX_SIDE = 4096;
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
  let bmp: ImageBitmap;
  try {
    bmp = await createImageBitmap(f, { imageOrientation: "from-image" });
  } catch {
    return { file: f, note: "" };
  }
  const long = Math.max(bmp.width, bmp.height);
  if (long <= MAX_SIDE && f.size <= MAX_BYTES && /jpe?g|png/i.test(f.type || f.name)) {
    const r = { file: f, note: "", width: bmp.width, height: bmp.height };
    bmp.close();
    return r;
  }
  const k = Math.min(1, MAX_SIDE / long);
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
