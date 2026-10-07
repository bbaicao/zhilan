const MAX_EDGE = 1280;
const QUALITY = 0.82;

/** 条目 38：取剪贴板里**全部**图片（一次粘贴多张全进附件区），保持剪贴板顺序。
 *
 * 截图（Win+Shift+S）与「复制图片」都会以 `kind: "file"` 的形式进来，
 * 这里给它们起能看懂的名字，后面和「+」选文件走同一条压缩链路。
 */
export function imagesFromClipboard(
  items: DataTransferItemList | DataTransferItem[] | null | undefined,
): File[] {
  if (!items) return [];
  const list = items as ArrayLike<DataTransferItem>;
  const files: File[] = [];
  let index = 0;
  let seen = 0;
  while (index < list.length) {
    const item = list[index];
    index += 1;
    if (!item || item.kind !== "file" || !item.type.startsWith("image/")) continue;
    const file = item.getAsFile();
    if (!file) continue;
    seen += 1;
    const ext = (file.type.split("/")[1] || "png").replace("jpeg", "jpg");
    const name = seen > 1 ? `粘贴的图片${seen}.${ext}` : `粘贴的图片.${ext}`;
    files.push(new File([file], name, { type: file.type }));
  }
  return files;
}

/** 条目 38：从文件列表（选择器 / 拖拽）里挑出全部图片，顺序原样。 */
export function imageFilesFromList(files: FileList | File[] | null | undefined): File[] {
  if (!files) return [];
  return Array.from(files).filter((file) => file && file.type.startsWith("image/"));
}

export async function compressImage(file: File): Promise<string> {
  const bitmap = await createImageBitmap(file);
  try {
    const scale = Math.min(1, MAX_EDGE / Math.max(bitmap.width, bitmap.height));
    const width = Math.max(1, Math.round(bitmap.width * scale));
    const height = Math.max(1, Math.round(bitmap.height * scale));
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");
    if (!ctx) {
      throw new Error("图片压缩失败，请换一张再试");
    }
    ctx.drawImage(bitmap, 0, 0, width, height);
    return canvas.toDataURL("image/jpeg", QUALITY);
  } finally {
    bitmap.close();
  }
}
