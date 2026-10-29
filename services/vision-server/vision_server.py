#!/usr/bin/env python3
# GPU desktop desktop GPU vision service (2026-10-27: CLIP tags + phrase search "do them"; face fingerprints clustered into same-person
# groups, "tags only"). Listens on 127.0.0.1:8760 only; the hub reaches it through its user unit GPU desktop-vision-tunnel (:11436).
#   POST /embed   binary: 4-byte big-endian header length, JSON header {"items": [{"hash", "len", "faces": [[10 landmarks], ...]}]},
#                 then the raw image files back to back. Returns JSON per hash: clip (base64 float16 1152), tags [[tag, score]...],
#                 faces [base64 float16 512 ...] (ArcFace w600k_r50 on 112x112 crops aligned from the hub's YuNet landmarks).
#                 Every CLIP embedding is also kept in store/clip-*.npz for /search.
#   GET  /search?q=dog+on+a+beach&k=50   -> [[hash, score], ...] over every stored embedding (counts/hashes only, no content)
#   GET  /health
import base64, glob, io, json, os, struct, threading, time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import cv2, numpy as np, onnxruntime as ort, open_clip, torch
from PIL import Image, ImageFile, ImageOps
import pillow_heif
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "lib"))
from svcenv import env  # noqa: E402  (shared repo .env; see .env.example)

pillow_heif.register_heif_opener()
ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = 400_000_000
BASE = env("VISION_BASE", "/opt/server-services/vision-server")
STORE = f"{BASE}/store"
CLIP_NAME = "hf-hub:timm/ViT-SO400M-14-SigLIP-384"
ARC = f"{BASE}/models/w600k_r50.onnx"
ARC_REF = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366], [41.5493, 92.3655], [70.7299, 92.2041]], np.float32)
TAGS = {   # tag -> prompt; SigLIP scores each independently (sigmoid), so an image can carry several
    "people": "a photo of people", "selfie": "a selfie", "group_photo": "a group photo of several people", "baby": "a photo of a baby",
    "child": "a photo of a child", "dog": "a photo of a dog", "cat": "a photo of a cat", "animal": "a photo of a wild animal or bird",
    "food": "a photo of food", "drink": "a photo of a drink", "landscape": "a landscape photo of nature", "beach": "a photo of a beach",
    "mountains": "a photo of mountains", "snow": "a photo of snow", "sunset": "a photo of a sunset", "flowers": "a photo of flowers",
    "city": "a photo of a city street", "building": "a photo of a building", "indoor_room": "a photo of a room inside a home",
    "car": "a photo of a car", "motorcycle": "a photo of a motorcycle", "boat": "a photo of a boat", "airplane": "a photo of an airplane",
    "document": "a scanned document", "receipt": "a photo of a receipt", "handwriting": "a handwritten note",
    "form": "a filled-in form", "business_card": "a business card", "id_card": "an identity card or driver's license",
    "phone_screenshot": "a screenshot of a phone screen", "computer_screenshot": "a screenshot of a computer screen",
    "chat_screenshot": "a screenshot of a text message conversation", "game_screenshot": "a video game screenshot",
    "product": "a product photo on a white background", "painting": "a painting", "drawing": "a drawing or illustration",
    "cartoon": "a cartoon", "anime": "anime art", "meme": "a meme with text", "logo": "a logo", "icon": "an app icon",
    "clipart": "clip art", "chart": "a chart or graph", "map": "a map", "diagram": "a diagram", "birthday": "a birthday party",
    "wedding": "a wedding", "holiday": "a holiday celebration with decorations", "concert": "a concert", "sports": "people playing sports",
    "christmas": "a christmas tree", "fireworks": "fireworks", "dashboard_car": "a car dashboard", "whiteboard": "a whiteboard",
    "electronics": "a computer or electronic device", "clothing": "clothing", "vehicle_interior": "the inside of a car",
    "pool": "a swimming pool", "restaurant": "a restaurant", "office": "an office", "garden": "a garden or backyard",
    "night": "a photo taken at night", "text": "an image that is mostly text", "blurry": "a blurry photo", "dark": "a very dark photo",
}
dev = "cuda"
model, _, preprocess = open_clip.create_model_and_transforms(CLIP_NAME, device=dev, precision="fp16")
model.eval()
tokenizer = open_clip.get_tokenizer(CLIP_NAME)
with torch.no_grad():
    tf = model.encode_text(tokenizer(list(TAGS.values())).to(dev))
    TAG_EMB = torch.nn.functional.normalize(tf.float(), dim=-1)
SCALE, BIAS = model.logit_scale.exp().float().item(), model.logit_bias.float().item()
arc = ort.InferenceSession(ARC, providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
ARC_IN = arc.get_inputs()[0].name
GPU = threading.Lock()
POOL = ThreadPoolExecutor(24)
os.makedirs(STORE, exist_ok=True)
_index = {"files": None, "hashes": None, "emb": None}


def prep(raw, faces):
    """decode once on the CPU: CLIP tensor + aligned 112x112 face crops"""
    try:
        im = Image.open(io.BytesIO(raw))
        full = im.size
        im.draft("RGB", (1024, 1024))
        im = im.convert("RGB")                      # landmarks are in original pixels; the draft may have shrunk the image
        k = im.size[0] / full[0]
        crops = []
        if faces:
            a = np.asarray(im)
            for lm in faces:
                p = np.array(lm, np.float32).reshape(5, 2) * k
                eyes, mouth = sorted(p[:2].tolist()), sorted(p[3:].tolist())          # image-left first, as ARC_REF
                src = np.array(eyes + [p[2].tolist()] + mouth, np.float32)
                M, _ = cv2.estimateAffinePartial2D(src, ARC_REF)
                crops.append(cv2.warpAffine(a, M, (112, 112)) if M is not None else np.zeros((112, 112, 3), np.uint8))
        return preprocess(im), crops, None
    except Exception as e:
        return None, [], str(e)[:200]


def embed(header, body):
    items, off, raws = header["items"], 0, []
    for it in items:
        raws.append(body[off:off + it["len"]]); off += it["len"]
    preps = list(POOL.map(lambda x: prep(*x), [(r, it.get("faces") or []) for r, it in zip(raws, items)]))
    ok = [i for i, p in enumerate(preps) if p[0] is not None]
    out = {it["hash"]: {"err": preps[i][2]} for i, it in enumerate(items)}
    crops = [(i, c) for i in ok for c in preps[i][1]]
    with GPU, torch.no_grad():
        if ok:
            x = torch.stack([preps[i][0] for i in ok]).to(dev, torch.float16)
            e = torch.nn.functional.normalize(model.encode_image(x).float(), dim=-1)
            prob = torch.sigmoid(e @ TAG_EMB.T * SCALE + BIAS).cpu().numpy()
            e16 = e.half().cpu().numpy()
        if crops:
            b = np.stack([c for _, c in crops]).astype(np.float32)
            b = (b - 127.5) / 127.5
            fe = arc.run(None, {ARC_IN: b.transpose(0, 3, 1, 2)})[0]
            fe = (fe / np.linalg.norm(fe, axis=1, keepdims=True)).astype(np.float16)
    names = list(TAGS)
    for j, i in enumerate(ok):
        h = items[i]["hash"]
        top = np.argsort(-prob[j])
        tags = [[names[t], round(float(prob[j][t]), 4)] for t in top[:8] if prob[j][t] >= 0.01 or t == top[0]]
        out[h] = {"clip": base64.b64encode(e16[j].tobytes()).decode(), "tags": tags, "faces": []}
    for (i, _), f in zip(crops, fe if crops else []):
        out[items[i]["hash"]]["faces"].append(base64.b64encode(f.tobytes()).decode())
    if ok:
        np.savez(f"{STORE}/clip-{time.time_ns()}.npz", hashes=np.array([items[i]["hash"] for i in ok]), emb=e16)
    return out


def index():
    files = sorted(glob.glob(f"{STORE}/clip-*.npz"))
    if files != _index["files"]:
        hs, es = [], []
        for f in files:
            z = np.load(f); hs.append(z["hashes"]); es.append(z["emb"])
        _index.update(files=files, hashes=np.concatenate(hs) if hs else np.array([]),
                      emb=torch.from_numpy(np.concatenate(es)).to(dev) if es else None)
    return _index["hashes"], _index["emb"]


def search(q, k):
    with GPU, torch.no_grad():
        hs, emb = index()
        if emb is None:
            return []
        t = torch.nn.functional.normalize(model.encode_text(tokenizer([q]).to(dev)).float(), dim=-1).half()
        s = (emb @ t.T).squeeze(1).float()
        v, i = torch.topk(s, min(k, len(s)))
        seen, res = set(), []
        for score, j in zip(v.tolist(), i.tolist()):
            if hs[j] not in seen:
                seen.add(hs[j]); res.append([str(hs[j]), round(score, 4)])
        return res


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            return self.send({"ok": True, "tags": len(TAGS), "stored": len(glob.glob(f"{STORE}/clip-*.npz"))})
        if u.path == "/search":
            q = parse_qs(u.query)
            return self.send(search(q.get("q", [""])[0], int(q.get("k", ["50"])[0])))
        self.send({"err": "not found"}, 404)

    def do_POST(self):
        if self.path != "/embed":
            return self.send({"err": "not found"}, 404)
        body = self.rfile.read(int(self.headers["Content-Length"]))
        n = struct.unpack(">I", body[:4])[0]
        try:
            self.send(embed(json.loads(body[4:4 + n]), body[4 + n:]))
        except Exception as e:
            self.send({"err": str(e)[:300]}, 500)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8760), H).serve_forever()
