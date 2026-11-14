"""Export google/siglip2-base-patch16-224 (Apache-2.0) to ONNX for Photo Sorter.

Needs (only for this one-time step): torch (CPU), transformers, sentencepiece, onnx, onnxruntime.
Writes into <models>/siglip2/: vision_model.onnx (fp32), text_model_int8.onnx (int8 token-embedding table),
tokenizer.json, config.json (logit scale/bias), LICENSE-NOTICE.txt.
Usage: python tools/export_siglip2.py [--out DIR] [--opset 17]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

MODEL_ID = "google/siglip2-base-patch16-224"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None, help="models dir (default: Photo Sorter model cache)")
    ap.add_argument("--opset", type=int, default=17)
    a = ap.parse_args()
    if a.out:
        out = Path(a.out)
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from photosorter.config import models_dir
        out = models_dir()
    d = out / "siglip2"
    d.mkdir(parents=True, exist_ok=True)

    import torch
    from transformers import AutoModel, AutoTokenizer

    torch.set_grad_enabled(False)
    model = AutoModel.from_pretrained(MODEL_ID, dtype=torch.float32).eval()
    model.requires_grad_(False)
    tok = AutoTokenizer.from_pretrained(MODEL_ID)

    def feats(x):
        return x if isinstance(x, torch.Tensor) else x.pooler_output

    class Vision(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.m = model

        def forward(self, pixel_values):
            return feats(self.m.get_image_features(pixel_values=pixel_values))

    class Text(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.m = model

        def forward(self, input_ids):
            return feats(self.m.get_text_features(input_ids=input_ids))

    size = model.config.vision_config.image_size
    kw = dict(opset_version=a.opset, do_constant_folding=True)
    if "dynamo" in torch.onnx.export.__code__.co_varnames:
        kw["dynamo"] = False
    print("exporting vision...")
    torch.onnx.export(Vision(), (torch.zeros(1, 3, size, size),), str(d / "vision_model.onnx"),
                      input_names=["pixel_values"], output_names=["image_embeds"],
                      dynamic_axes={"pixel_values": {0: "batch"}, "image_embeds": {0: "batch"}}, **kw)
    print("exporting text...")
    tmp = d / "text_model_fp32.onnx"
    torch.onnx.export(Text(), (torch.zeros(1, 64, dtype=torch.long),), str(tmp),
                      input_names=["input_ids"], output_names=["text_embeds"],
                      dynamic_axes={"input_ids": {0: "batch"}, "text_embeds": {0: "batch"}}, **kw)
    from onnxruntime.quantization import QuantType, quantize_dynamic

    print("quantizing text embedding table to int8...")
    # int8 only for the 256k-token embedding table (Gather): ~1 GB -> ~0.5 GB with cosine >= 0.996 vs fp32.
    # Quantizing the MatMuls too drops cosine to ~0.92-0.98, which hurts zero-shot sorting.
    quantize_dynamic(str(tmp), str(d / "text_model_int8.onnx"), weight_type=QuantType.QUInt8,
                     op_types_to_quantize=["Gather"])
    os.remove(tmp)
    tok.backend_tokenizer.save(str(d / "tokenizer.json"))
    pad_id = tok.pad_token_id if tok.pad_token_id is not None else 0
    cfg = {"model_id": MODEL_ID, "logit_scale": float(model.logit_scale.exp()), "logit_bias": float(model.logit_bias),
           "image_size": size, "max_length": 64, "pad_id": int(pad_id), "lowercase": True}
    (d / "config.json").write_text(json.dumps(cfg, indent=2))
    (d / "LICENSE-NOTICE.txt").write_text(
        f"{MODEL_ID} by Google, licensed under the Apache License 2.0 "
        "(https://www.apache.org/licenses/LICENSE-2.0). Converted to ONNX (and the text tower int8-quantized) "
        "by Photo Sorter's tools/export_siglip2.py; weights are otherwise unmodified.\n")
    # self-check against PyTorch
    import numpy as np
    import onnxruntime as ort

    texts = ["a photo of a dog", "a screenshot of a phone screen"]
    enc = tok(texts, padding="max_length", max_length=64, truncation=True, return_tensors="pt")
    ref = feats(model.get_text_features(input_ids=enc["input_ids"])).numpy()
    s = ort.InferenceSession(str(d / "text_model_int8.onnx"), providers=["CPUExecutionProvider"])
    got = s.run(None, {"input_ids": enc["input_ids"].numpy()})[0]
    cos = (ref * got).sum(1) / (np.linalg.norm(ref, axis=1) * np.linalg.norm(got, axis=1))
    print("text int8 vs torch cosine:", cos.round(4).tolist())
    px = torch.rand(1, 3, size, size) * 2 - 1
    refv = feats(model.get_image_features(pixel_values=px)).numpy()
    sv = ort.InferenceSession(str(d / "vision_model.onnx"), providers=["CPUExecutionProvider"])
    gotv = sv.run(None, {"pixel_values": px.numpy()})[0]
    print("vision max abs diff:", float(np.abs(refv - gotv).max()))
    print("token ids (for tokenizer check):", enc["input_ids"][0][:12].tolist())
    print("done ->", d)


if __name__ == "__main__":
    main()
