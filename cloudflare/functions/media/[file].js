// Serves images uploaded in Admin -> Media library, e.g. /media/12.jpg (same as server.py's media_file).
const MEDIA_TYPES = { "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif" };

export async function onRequestGet({ params, env }) {
  const m = /^(\d+)\.(jpg|png|webp|gif)$/.exec(String(params.file || ""));
  if (!m || !env.DB) return new Response("Not found", { status: 404 });
  const row = await env.DB.prepare("SELECT mime, data FROM media WHERE id = ?").bind(Number(m[1])).first().catch(() => null);
  if (!row || MEDIA_TYPES[row.mime] !== m[2]) return new Response("Not found", { status: 404 });
  const bytes = Uint8Array.from(atob(row.data), c => c.charCodeAt(0));
  return new Response(bytes, { headers: { "Content-Type": row.mime, "Cache-Control": "public, max-age=31536000, immutable" } });
}
