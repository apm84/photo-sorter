"""Pluggable image/text embedders. Default: SigLIP 2 (Apache-2.0). Optional: OpenAI CLIP ViT-B/32 (MIT).

Each embedder gives L2-normalised image and text vectors in a shared space, plus zero-shot helpers.
Add a new backend by subclassing Embedder and registering it in EMBEDDERS.
"""
from __future__ import annotations

import json

import numpy as np
from PIL import Image

from .models import ResilientSession, model_path
from .prompts import TAG_VOCAB, TYPE_PROMPTS, tag_prompt


def _norm(x: np.ndarray) -> np.ndarray:
    return x / np.maximum(np.linalg.norm(x, axis=-1, keepdims=True), 1e-8)


class Embedder:
    name = "base"
    files: tuple[str, ...] = ()
    tag_threshold = 0.1

    def __init__(self, use_gpu: bool = True, text_only: bool = False):
        self._text_cache: dict[str, np.ndarray] = {}
        self.vision = None

    @classmethod
    def available(cls) -> bool:
        return all(model_path(f).exists() for f in cls.files)

    @property
    def on_gpu(self) -> bool:
        return bool(self.vision and self.vision.on_gpu)

    # backend hooks
    def preprocess(self, im: Image.Image) -> np.ndarray: ...
    def _run_vision(self, batch: np.ndarray) -> np.ndarray: ...
    def _run_text(self, texts: list[str]) -> np.ndarray: ...

    def encode_images(self, images: list[Image.Image]) -> np.ndarray:
        batch = np.stack([self.preprocess(im) for im in images]).astype(np.float32)
        return _norm(self._run_vision(batch).astype(np.float32))

    def encode_texts(self, texts: list[str]) -> np.ndarray:
        missing = [t for t in dict.fromkeys(texts) if t not in self._text_cache]
        if missing:
            for t, v in zip(missing, _norm(self._run_text(missing).astype(np.float32))):
                self._text_cache[t] = v
        return np.stack([self._text_cache[t] for t in texts])

    def type_matrix(self) -> tuple[list[str], np.ndarray]:
        labels = list(TYPE_PROMPTS)
        return labels, np.stack([_norm(self.encode_texts(TYPE_PROMPTS[k]).mean(axis=0)) for k in labels])

    def tag_matrix(self) -> tuple[list[str], np.ndarray]:
        return TAG_VOCAB, self.encode_texts([tag_prompt(t) for t in TAG_VOCAB])

    def type_probs(self, img_emb: np.ndarray, mat: np.ndarray, scale: float = 100.0) -> np.ndarray:
        logits = scale * img_emb @ mat.T
        logits -= logits.max(axis=-1, keepdims=True)
        e = np.exp(logits)
        return e / e.sum(axis=-1, keepdims=True)

    def pick_tags(self, img_emb: np.ndarray, mat: np.ndarray, top: int = 5) -> list[tuple[int, float]]:
        """Indices + scores of tags that apply (cosine similarity above a threshold)."""
        sims = mat @ img_emb
        return [(int(j), float(sims[j])) for j in np.argsort(-sims)[:top] if sims[j] >= self.tag_threshold]


class SigLIP2(Embedder):
    """google/siglip2-base-patch16-224, ONNX exported with tools/export_siglip2.py."""
    name = "siglip2"
    files = ("siglip2/vision_model.onnx", "siglip2/text_model_int8.onnx", "siglip2/tokenizer.json", "siglip2/config.json")

    def __init__(self, use_gpu: bool = True, text_only: bool = False):
        super().__init__(use_gpu, text_only)
        from tokenizers import Tokenizer

        cfg = json.loads(model_path("siglip2/config.json").read_text())
        self.scale, self.bias = float(cfg["logit_scale"]), float(cfg["logit_bias"])
        self.size, self.max_len, self.pad_id = int(cfg.get("image_size", 224)), int(cfg.get("max_length", 64)), int(cfg.get("pad_id", 0))
        self.lower = bool(cfg.get("lowercase", True))
        self.tok = Tokenizer.from_file(str(model_path("siglip2/tokenizer.json")))
        self.text = ResilientSession(model_path("siglip2/text_model_int8.onnx"), use_gpu=False)
        if not text_only:
            self.vision = ResilientSession(model_path("siglip2/vision_model.onnx"), use_gpu=use_gpu)

    def preprocess(self, im: Image.Image) -> np.ndarray:
        a = np.asarray(im.resize((self.size, self.size), Image.BILINEAR), dtype=np.float32) / 255.0
        return ((a - 0.5) / 0.5).transpose(2, 0, 1)

    def _run_vision(self, batch):
        return self.vision.run({"pixel_values": batch})[0]

    def tokenize(self, texts: list[str]) -> np.ndarray:
        arr = np.full((len(texts), self.max_len), self.pad_id, dtype=np.int64)
        for i, t in enumerate(texts):
            ids = self.tok.encode(t.lower() if self.lower else t).ids[: self.max_len]
            arr[i, : len(ids)] = ids
        return arr

    def _run_text(self, texts):
        return self.text.run({"input_ids": self.tokenize(texts)})[0]

    def type_probs(self, img_emb, mat, scale=None):
        return super().type_probs(img_emb, mat, scale or self.scale)

    def pick_tags(self, img_emb, mat, top: int = 3):
        # relative score (softmax over the vocabulary) + an absolute floor (sigmoid) so that
        # images matching nothing in the vocabulary don't get a forced tag
        rel = self.type_probs(img_emb[None, :], mat)[0]
        absp = 1.0 / (1.0 + np.exp(-(self.scale * (mat @ img_emb) + self.bias)))
        return [(int(j), float(rel[j])) for j in np.argsort(-rel)[:top] if rel[j] >= 0.2 and absp[j] >= 5e-4]


class CLIP(Embedder):
    """OpenAI CLIP ViT-B/32 (MIT), Xenova ONNX export. Optional alternative."""
    name = "clip"
    files = ("clip/tokenizer.json", "clip/text_model_quantized.onnx", "clip/vision_model.onnx")
    tag_threshold = 0.25   # cosine similarity
    MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
    STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
    EOT, CTX = 49407, 77

    def __init__(self, use_gpu: bool = True, text_only: bool = False):
        super().__init__(use_gpu, text_only)
        from tokenizers import Tokenizer

        self.tok = Tokenizer.from_file(str(model_path("clip/tokenizer.json")))
        self.text = ResilientSession(model_path("clip/text_model_quantized.onnx"), use_gpu=False)
        if not text_only:
            self.vision = ResilientSession(model_path("clip/vision_model.onnx"), use_gpu=use_gpu)

    def preprocess(self, im: Image.Image, size: int = 224) -> np.ndarray:
        w, h = im.size
        s = size / min(w, h)
        im = im.resize((max(size, round(w * s)), max(size, round(h * s))), Image.BICUBIC)
        w, h = im.size
        left, top = (w - size) // 2, (h - size) // 2
        a = np.asarray(im.crop((left, top, left + size, top + size)), dtype=np.float32) / 255.0
        return ((a - self.MEAN) / self.STD).transpose(2, 0, 1)

    def _run_vision(self, batch):
        return self.vision.run({"pixel_values": batch})[0]

    def _run_text(self, texts):
        ids = []
        for t in texts:
            e = self.tok.encode(t.lower()).ids[: self.CTX]
            if e[-1] != self.EOT:
                e = e[: self.CTX - 1] + [self.EOT]
            ids.append(e)
        L = max(len(e) for e in ids)
        arr = np.full((len(ids), L), self.EOT, dtype=np.int64)  # pad with EOT; pooling uses the first EOT
        for i, e in enumerate(ids):
            arr[i, : len(e)] = e
        return self.text.run({"input_ids": arr})[0]


EMBEDDERS: dict[str, type[Embedder]] = {"siglip2": SigLIP2, "clip": CLIP}


def get_embedder(name: str, use_gpu: bool = True, text_only: bool = False) -> Embedder | None:
    cls = EMBEDDERS.get(name)
    if cls is None or not cls.available():
        return None
    return cls(use_gpu=use_gpu, text_only=text_only)


def embedder_available(name: str) -> bool:
    cls = EMBEDDERS.get(name)
    return bool(cls and cls.available())
