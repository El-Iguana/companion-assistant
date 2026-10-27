"""
ComfyUI client — uses the /prompt workflow API.

Flow:
  1. POST /prompt   → {prompt_id}
  2. Poll GET /history/{prompt_id}  until outputs appear
  3. GET /view?filename=...&type=output  → raw PNG bytes
  4. Return base64-encoded PNG

Supports SD 1.5, SDXL, and FLUX.1 (dev/schnell) checkpoints.
Model type is auto-detected from the checkpoint filename.
"""
import asyncio
import base64
import random
import uuid
from typing import Optional

import httpx


# ---------------------------------------------------------------------------
# Model type detection
# ---------------------------------------------------------------------------

def is_flux(checkpoint: str) -> bool:
    return "flux" in checkpoint.lower()


def is_gguf(checkpoint: str) -> bool:
    return checkpoint.lower().endswith(".gguf")


# FLUX from a GGUF diffusion model (models/unet, e.g. city96's flux1-schnell-Q4_K_S.gguf)
# loads split: the model, its two text encoders and the VAE from separate files. An fp8
# all-in-one FLUX checkpoint is ~12 GB of diffusion weights alone — the whole 12 GB card —
# while a Q4 GGUF runs beside KoboldCPP (measured in Iguana Studio: 1024² in 24 s with
# KoboldCPP holding 9.5 GB). Same support files as Iguana's Flux and Kontext graphs.
FLUX_CLIP_L = "clip_l.safetensors"
FLUX_T5 = "t5xxl_fp8_e4m3fn_scaled.safetensors"
FLUX_VAE = "ae.safetensors"


# Flux Kontext: instruction editing ("change his expression to angry"). The source is bound
# to the conditioning (ReferenceLatent) instead of being re-noised, so everything the
# instruction doesn't mention stays put — which is exactly what an expression sprite needs.
# Measured on the 4070 for a sprite: 12 steps ≈ 52 s and reads as well as 20 (≈ 85 s).
KONTEXT_UNET = "flux1-kontext-dev-Q4_K_M.gguf"
KONTEXT_STEPS = 12
KONTEXT_GUIDANCE = 2.5


def _flux_loaders(checkpoint: str) -> tuple[dict, list, list, list]:
    """Loader nodes plus the (model, clip, vae) edges for a FLUX graph."""
    if is_gguf(checkpoint):
        return ({
            "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": checkpoint}},
            "1c": {"class_type": "DualCLIPLoader",
                   "inputs": {"clip_name1": FLUX_CLIP_L, "clip_name2": FLUX_T5, "type": "flux"}},
            "1v": {"class_type": "VAELoader", "inputs": {"vae_name": FLUX_VAE}},
        }, ["1", 0], ["1c", 0], ["1v", 0])
    return ({"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}}},
            ["1", 0], ["1", 1], ["1", 2])


def _is_flux_schnell(checkpoint: str) -> bool:
    return "schnell" in checkpoint.lower()


def is_sdxl(checkpoint: str) -> bool:
    if is_flux(checkpoint):
        return False
    name = checkpoint.lower()
    _SDXL_NAMES = {"crealism", "mirabilis", "juggernaut", "dreamshaper", "realvis", "pony"}
    return "xl" in name or "sdxl" in name or any(k in name for k in _SDXL_NAMES)


def get_checkpoint_defaults(checkpoint: str) -> dict:
    """Return model-appropriate generation defaults for a checkpoint filename."""
    if is_flux(checkpoint):
        if _is_flux_schnell(checkpoint):
            return {
                "steps": 4, "cfg": 1.0,
                "width": 1024, "height": 1024,
                "sampler": "euler", "scheduler": "simple",
                "is_flux": True, "is_sdxl": False,
            }
        return {
            "steps": 20, "cfg": 3.5,
            "width": 1024, "height": 1024,
            "sampler": "euler", "scheduler": "simple",
            "is_flux": True, "is_sdxl": False,
        }
    elif is_sdxl(checkpoint):
        return {
            "steps": 25, "cfg": 7.0,
            "width": 1024, "height": 1024,
            "sampler": "euler", "scheduler": "normal",
            "is_flux": False, "is_sdxl": True,
        }
    else:
        return {
            "steps": 20, "cfg": 7.0,
            "width": 512, "height": 512,
            "sampler": "euler", "scheduler": "normal",
            "is_flux": False, "is_sdxl": False,
        }


# ---------------------------------------------------------------------------
# Style presets
# Each entry adds text to the positive / negative prompts.
# ---------------------------------------------------------------------------
STYLE_PRESETS: dict[str, dict] = {
    "none": {
        "label": "None",
        "positive_suffix": "",
        "negative_prefix": "",
    },
    "photorealistic": {
        "label": "Photorealistic",
        "positive_suffix": (
            "photorealistic, highly detailed, professional photography, "
            "sharp focus, 8k uhd, film grain, natural lighting"
        ),
        "negative_prefix": (
            "cartoon, anime, painting, illustration, drawing, sketch, "
            "low quality, deformed, blurry"
        ),
    },
    "anime": {
        "label": "Anime",
        "positive_suffix": (
            "anime style, detailed anime art, vibrant colors, clean linework, "
            "studio quality, manga illustration"
        ),
        "negative_prefix": (
            "photorealistic, 3d render, photograph, blurry, low quality"
        ),
    },
    "fantasy_art": {
        "label": "Fantasy Art",
        "positive_suffix": (
            "epic fantasy art, highly detailed, dramatic lighting, intricate, "
            "concept art, artstation, by Greg Rutkowski"
        ),
        "negative_prefix": "low quality, blurry, deformed, modern, photograph",
    },
    "oil_painting": {
        "label": "Oil Painting",
        "positive_suffix": (
            "oil painting, classical art, impasto, rich colors, painterly, "
            "museum quality, detailed brushwork"
        ),
        "negative_prefix": "photograph, digital art, 3d render, anime, blurry",
    },
    "watercolor": {
        "label": "Watercolor",
        "positive_suffix": (
            "watercolor painting, soft edges, translucent colors, "
            "traditional media, artistic, detailed"
        ),
        "negative_prefix": "photograph, digital art, harsh lines, blurry",
    },
    "comic_book": {
        "label": "Comic Book",
        "positive_suffix": (
            "comic book art, bold lines, flat colors, halftone, "
            "Marvel/DC style, dynamic pose"
        ),
        "negative_prefix": "photograph, realistic, blurry, low quality",
    },
    "dark_fantasy": {
        "label": "Dark Fantasy",
        "positive_suffix": (
            "dark fantasy art, brooding atmosphere, dramatic shadows, "
            "gothic, eerie, highly detailed, concept art"
        ),
        "negative_prefix": "bright, cheerful, low quality, blurry, deformed",
    },
    "cinematic": {
        "label": "Cinematic",
        "positive_suffix": (
            "cinematic shot, movie still, dramatic lighting, shallow depth of field, "
            "film grain, anamorphic lens, 4k"
        ),
        "negative_prefix": "cartoon, anime, low quality, blurry, deformed",
    },
    "pixel_art": {
        "label": "Pixel Art",
        "positive_suffix": (
            "pixel art, 16-bit, retro game style, crisp pixels, "
            "limited palette, sprite art"
        ),
        "negative_prefix": "photograph, realistic, blurry, high resolution",
    },
}

# Default negative prompt shared across all non-FLUX models
DEFAULT_NEGATIVE = (
    "(worst quality, low quality, normal quality:1.4), "
    "(jpeg artifacts, compression artifacts, blurry, grainy, noisy:1.2), "
    "(deformed, distorted, disfigured:1.3), "
    "bad anatomy, bad proportions, "
    "(extra limbs, missing limbs, floating limbs:1.2), "
    "(extra fingers, missing fingers, fused fingers, too many fingers:1.4), "
    "malformed hands, mutated hands, "
    "long neck, ugly face, bad face, "
    "watermark, signature, text, logo, username, "
    "out of frame, cropped, lowres, draft"
)


# ---------------------------------------------------------------------------
# Workflow builders
# ---------------------------------------------------------------------------

def _build_sd15_workflow(
    positive: str,
    negative: str,
    checkpoint: str,
    width: int,
    height: int,
    steps: int,
    cfg: float,
    sampler: str,
    scheduler: str,
    seed: int,
) -> dict:
    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": checkpoint},
        },
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": positive, "clip": ["1", 1]},
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative, "clip": ["1", 1]},
        },
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed, "steps": steps, "cfg": cfg,
                "sampler_name": sampler, "scheduler": scheduler,
                "denoise": 1.0,
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
            },
        },
        "6": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
        },
        "7": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "story_maker", "images": ["6", 0]},
        },
    }


def _build_sdxl_workflow(
    positive: str,
    negative: str,
    checkpoint: str,
    width: int,
    height: int,
    steps: int,
    cfg: float,
    sampler: str,
    scheduler: str,
    seed: int,
) -> dict:
    pos_node = {
        "class_type": "CLIPTextEncodeSDXL",
        "inputs": {
            "width": width, "height": height,
            "crop_w": 0, "crop_h": 0,
            "target_width": width, "target_height": height,
            "text_g": positive, "text_l": positive,
            "clip": ["1", 1],
        },
    }
    neg_node = {
        "class_type": "CLIPTextEncodeSDXL",
        "inputs": {
            "width": width, "height": height,
            "crop_w": 0, "crop_h": 0,
            "target_width": width, "target_height": height,
            "text_g": negative, "text_l": negative,
            "clip": ["1", 1],
        },
    }
    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": checkpoint},
        },
        "2": pos_node,
        "3": neg_node,
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed, "steps": steps, "cfg": cfg,
                "sampler_name": sampler, "scheduler": scheduler,
                "denoise": 1.0,
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
            },
        },
        "6": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
        },
        "7": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "story_maker", "images": ["6", 0]},
        },
    }


def _build_flux_workflow(
    positive: str,
    checkpoint: str,
    width: int,
    height: int,
    steps: int,
    guidance: float,
    sampler: str,
    seed: int,
) -> dict:
    """FLUX.1 workflow — uses CLIPTextEncodeFlux + ModelSamplingFlux + EmptySD3LatentImage."""
    loaders, model, clip, vae = _flux_loaders(checkpoint)
    return {
        **loaders,
        # FLUX positive conditioning — guidance baked in here
        "2": {
            "class_type": "CLIPTextEncodeFlux",
            "inputs": {
                "clip": clip,
                "clip_l": positive,
                "t5xxl": positive,
                "guidance": guidance,
            },
        },
        # Empty negative — required by KSampler, FLUX doesn't use it
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "", "clip": clip},
        },
        # 16-channel latent (SD3/FLUX format)
        "4": {
            "class_type": "EmptySD3LatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        # Patch model for shift-based FLUX scheduling
        "5": {
            "class_type": "ModelSamplingFlux",
            "inputs": {
                "model": model,
                "max_shift": 1.15,
                "base_shift": 0.5,
                "width": width,
                "height": height,
            },
        },
        "6": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": 1.0,        # always 1.0 for FLUX; guidance lives in conditioning
                "sampler_name": sampler,
                "scheduler": "simple",
                "denoise": 1.0,
                "model": ["5", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
            },
        },
        "7": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["6", 0], "vae": vae},
        },
        "8": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "story_maker", "images": ["7", 0]},
        },
    }


def _build_workflow(
    positive: str,
    negative: str,
    checkpoint: str,
    width: int,
    height: int,
    steps: int,
    cfg: float,
    sampler: str,
    scheduler: str,
    seed: Optional[int] = None,
) -> dict:
    if seed is None or seed < 0:
        seed = random.randint(0, 2**32 - 1)

    if is_flux(checkpoint):
        defs = get_checkpoint_defaults(checkpoint)
        effective_steps = steps if steps > 0 else defs["steps"]
        guidance = cfg if cfg > 0 else defs["cfg"]
        return _build_flux_workflow(
            positive=positive,
            checkpoint=checkpoint,
            width=width, height=height,
            steps=effective_steps,
            guidance=guidance,
            sampler=sampler,
            seed=seed,
        )
    elif is_sdxl(checkpoint):
        return _build_sdxl_workflow(
            positive=positive, negative=negative,
            checkpoint=checkpoint,
            width=width, height=height,
            steps=steps, cfg=cfg,
            sampler=sampler, scheduler=scheduler,
            seed=seed,
        )
    else:
        return _build_sd15_workflow(
            positive=positive, negative=negative,
            checkpoint=checkpoint,
            width=width, height=height,
            steps=steps, cfg=cfg,
            sampler=sampler, scheduler=scheduler,
            seed=seed,
        )


class ComfyUIClient:
    def __init__(self, base_url: str = "http://comfyui:8188"):
        self.base_url = base_url.rstrip("/")
        self.client_id = str(uuid.uuid4())
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))
        return self._client

    async def check_status(self) -> dict:
        try:
            r = await self.client.get(f"{self.base_url}/system_stats", timeout=5.0)
            data = r.json()
            return {
                "online": True,
                "version": data.get("system", {}).get("comfyui_version", "unknown"),
            }
        except Exception as e:
            return {"online": False, "error": str(e)}

    async def list_checkpoints(self) -> list[str]:
        """Checkpoints, plus FLUX GGUFs from models/unet (loaded split — see
        _flux_loaders). Kontext is an edit model, not text-to-image, so it's left out.
        Without the ComfyUI-GGUF node there are simply no GGUF entries."""
        r = await self.client.get(
            f"{self.base_url}/object_info/CheckpointLoaderSimple", timeout=10.0
        )
        r.raise_for_status()
        info = r.json()
        names = list(info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0])
        try:
            r = await self.client.get(f"{self.base_url}/object_info/UnetLoaderGGUF", timeout=10.0)
            r.raise_for_status()
            ggufs = r.json()["UnetLoaderGGUF"]["input"]["required"]["unet_name"][0]
            names += [n for n in ggufs if is_flux(n) and "kontext" not in n.lower()]
        except Exception:  # noqa: BLE001 - no GGUF node is a normal state
            pass
        return names

    async def has_kontext(self) -> bool:
        """Is the Kontext GGUF installed (and the GGUF node present)?"""
        try:
            r = await self.client.get(f"{self.base_url}/object_info/UnetLoaderGGUF", timeout=10.0)
            r.raise_for_status()
            return KONTEXT_UNET in r.json()["UnetLoaderGGUF"]["input"]["required"]["unet_name"][0]
        except Exception:  # noqa: BLE001 - ComfyUI down / no GGUF node
            return False

    async def generate_kontext(self, image_b64: str, instruction: str, seed: Optional[int] = None,
                               steps: int = KONTEXT_STEPS, guidance: float = KONTEXT_GUIDANCE) -> str:
        """Edit an image by instruction with Flux Kontext; returns a base64 PNG.

        Mirrors ComfyUI's flux_kontext_dev_basic template with the GGUF loader (the fp8
        build needs ~20 GB). FluxKontextImageScale is mandatory — it snaps the source to
        a trained resolution; without it the output goes mushy. CFG is pinned to 1.0 and
        the negative is a zeroed copy of the positive: Kontext is guidance-distilled.
        The output size is Kontext's, not the source's — callers resize if they care.
        """
        if seed is None:
            seed = random.randint(0, 2**32 - 1)
        img_name = await self.upload_image(base64.b64decode(image_b64))
        workflow = {
            "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": KONTEXT_UNET}},
            "2": {"class_type": "DualCLIPLoader", "inputs": {
                "clip_name1": FLUX_CLIP_L, "clip_name2": FLUX_T5, "type": "flux"}},
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": FLUX_VAE}},
            "4": {"class_type": "LoadImage", "inputs": {"image": img_name}},
            "5": {"class_type": "FluxKontextImageScale", "inputs": {"image": ["4", 0]}},
            "6": {"class_type": "VAEEncode", "inputs": {"pixels": ["5", 0], "vae": ["3", 0]}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"text": instruction, "clip": ["2", 0]}},
            "8": {"class_type": "ReferenceLatent", "inputs": {"conditioning": ["7", 0], "latent": ["6", 0]}},
            "9": {"class_type": "FluxGuidance", "inputs": {"conditioning": ["8", 0], "guidance": guidance}},
            "10": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["7", 0]}},
            "11": {"class_type": "KSampler", "inputs": {
                "model": ["1", 0], "seed": seed, "steps": steps, "cfg": 1.0,
                "sampler_name": "euler", "scheduler": "simple",
                "positive": ["9", 0], "negative": ["10", 0], "latent_image": ["6", 0], "denoise": 1.0}},
            "12": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["3", 0]}},
            "13": {"class_type": "SaveImage", "inputs": {"images": ["12", 0], "filename_prefix": "companion"}},
        }
        r = await self.client.post(f"{self.base_url}/prompt",
                                   json={"prompt": workflow, "client_id": self.client_id})
        r.raise_for_status()
        info = await self._wait_for_output(r.json()["prompt_id"], timeout=600)
        r = await self.client.get(f"{self.base_url}/view", params={
            "filename": info["filename"], "subfolder": info.get("subfolder", ""), "type": "output"})
        r.raise_for_status()
        return base64.b64encode(r.content).decode()

    async def list_samplers(self) -> dict:
        r = await self.client.get(f"{self.base_url}/object_info/KSampler", timeout=10.0)
        r.raise_for_status()
        info = r.json()["KSampler"]["input"]["required"]
        return {
            "samplers": info["sampler_name"][0],
            "schedulers": info["scheduler"][0],
        }

    async def generate(
        self,
        positive: str,
        negative: str,
        checkpoint: str,
        style: str = "none",
        width: int = 512,
        height: int = 512,
        steps: int = 20,
        cfg: float = 7.0,
        sampler: str = "euler",
        scheduler: str = "normal",
        seed: Optional[int] = None,
    ) -> str:
        """Generate an image and return it as a base64-encoded PNG string."""
        # Apply style preset to prompts (FLUX ignores negative in the workflow,
        # but we still apply the suffix to the positive prompt)
        preset = STYLE_PRESETS.get(style, STYLE_PRESETS["none"])
        if preset["positive_suffix"]:
            positive = f"{positive}, {preset['positive_suffix']}"
        if preset["negative_prefix"] and not is_flux(checkpoint):
            negative = f"{preset['negative_prefix']}, {negative}" if negative else preset["negative_prefix"]

        workflow = _build_workflow(
            positive=positive,
            negative=negative,
            checkpoint=checkpoint,
            width=width,
            height=height,
            steps=steps,
            cfg=cfg,
            sampler=sampler,
            scheduler=scheduler,
            seed=seed,
        )

        # Queue the prompt
        payload = {"prompt": workflow, "client_id": self.client_id}
        r = await self.client.post(f"{self.base_url}/prompt", json=payload)
        r.raise_for_status()
        prompt_id = r.json()["prompt_id"]

        # Poll for completion (max 5 minutes)
        image_info = await self._wait_for_output(prompt_id, timeout=300)

        # Fetch the image bytes
        r = await self.client.get(
            f"{self.base_url}/view",
            params={
                "filename": image_info["filename"],
                "subfolder": image_info.get("subfolder", ""),
                "type": "output",
            },
        )
        r.raise_for_status()
        return base64.b64encode(r.content).decode()

    async def _wait_for_output(self, prompt_id: str, timeout: int = 300) -> dict:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(1.5)
            r = await self.client.get(f"{self.base_url}/history/{prompt_id}", timeout=10.0)
            if r.status_code == 200:
                history = r.json()
                if prompt_id in history:
                    entry = history[prompt_id]
                    status = entry.get("status", {})
                    if status.get("status_str") == "error":
                        msgs = status.get("messages", [])
                        raise RuntimeError(f"ComfyUI generation error: {msgs}")
                    outputs = entry.get("outputs", {})
                    for node_output in outputs.values():
                        images = node_output.get("images", [])
                        if images:
                            return images[0]
        raise TimeoutError(f"ComfyUI generation timed out after {timeout}s")

    async def upload_image(self, png_bytes: bytes, filename: str = "companion_base.png") -> str:
        """Upload a PNG to ComfyUI's input folder; return the reference name."""
        files = {"image": (filename, png_bytes, "image/png")}
        r = await self.client.post(f"{self.base_url}/upload/image",
                                   files=files, data={"overwrite": "true"})
        r.raise_for_status()
        j = r.json()
        sub = j.get("subfolder", "")
        return f"{sub}/{j['name']}" if sub else j["name"]

    async def generate_img2img(
        self, image_b64: str, positive: str, negative: str, checkpoint: str,
        style: str = "none", steps: int = 20, cfg: float = 7.0,
        sampler: str = "euler", scheduler: str = "normal",
        denoise: float = 0.55, seed: Optional[int] = None,
    ) -> str:
        """img2img from a base image — keeps composition/identity, changes the rest.

        Standard SD1.5/SDXL node graph (LoadImage -> VAEEncode -> KSampler -> VAEDecode);
        FLUX gets its own (CLIPTextEncodeFlux with `cfg` as guidance, ModelSamplingFlux,
        sampler cfg pinned to 1.0, no negative) — the SD graph would run, but FLUX would
        ignore half of it and sample at the wrong cfg.
        """
        if seed is None:
            seed = random.randint(0, 2**32 - 1)
        preset = STYLE_PRESETS.get(style, STYLE_PRESETS["none"])
        if preset["positive_suffix"]:
            positive = f"{positive}, {preset['positive_suffix']}"
        if preset["negative_prefix"] and not is_flux(checkpoint):
            negative = f"{preset['negative_prefix']}, {negative}" if negative else preset["negative_prefix"]

        img_name = await self.upload_image(base64.b64decode(image_b64))
        if is_flux(checkpoint):
            workflow = self._flux_img2img(img_name, positive, checkpoint, steps, cfg,
                                          sampler, denoise, seed)
        else:
            workflow = self._sd_img2img(img_name, positive, negative, checkpoint, steps, cfg,
                                        sampler, scheduler, denoise, seed)
        r = await self.client.post(f"{self.base_url}/prompt",
                                   json={"prompt": workflow, "client_id": self.client_id})
        r.raise_for_status()
        prompt_id = r.json()["prompt_id"]
        info = await self._wait_for_output(prompt_id, timeout=300)
        r = await self.client.get(f"{self.base_url}/view", params={
            "filename": info["filename"], "subfolder": info.get("subfolder", ""), "type": "output"})
        r.raise_for_status()
        return base64.b64encode(r.content).decode()

    @staticmethod
    def _flux_img2img(img_name: str, positive: str, checkpoint: str, steps: int,
                      guidance: float, sampler: str, denoise: float, seed: int) -> dict:
        loaders, model, clip, vae = _flux_loaders(checkpoint)
        return {
            **loaders,
            "6": {"class_type": "CLIPTextEncodeFlux", "inputs": {
                "clip": clip, "clip_l": positive, "t5xxl": positive, "guidance": guidance}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": clip}},
            "10": {"class_type": "LoadImage", "inputs": {"image": img_name}},
            "11": {"class_type": "VAEEncode", "inputs": {"pixels": ["10", 0], "vae": vae}},
            "12": {"class_type": "ModelSamplingFlux", "inputs": {
                "model": model, "max_shift": 1.15, "base_shift": 0.5,
                "width": 1024, "height": 1024}},
            "3": {"class_type": "KSampler", "inputs": {
                "seed": seed, "steps": steps, "cfg": 1.0,
                "sampler_name": sampler, "scheduler": "simple", "denoise": denoise,
                "model": ["12", 0], "positive": ["6", 0], "negative": ["7", 0],
                "latent_image": ["11", 0]}},
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": vae}},
            "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "companion", "images": ["8", 0]}},
        }

    @staticmethod
    def _sd_img2img(img_name: str, positive: str, negative: str, checkpoint: str, steps: int,
                    cfg: float, sampler: str, scheduler: str, denoise: float, seed: int) -> dict:
        return {
            "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": positive, "clip": ["4", 1]}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["4", 1]}},
            "10": {"class_type": "LoadImage", "inputs": {"image": img_name}},
            "11": {"class_type": "VAEEncode", "inputs": {"pixels": ["10", 0], "vae": ["4", 2]}},
            "3": {"class_type": "KSampler", "inputs": {
                "seed": seed, "steps": steps, "cfg": cfg,
                "sampler_name": sampler, "scheduler": scheduler, "denoise": denoise,
                "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
                "latent_image": ["11", 0]}},
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
            "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "companion", "images": ["8", 0]}},
        }

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
