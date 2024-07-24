"""
Jovimetrix - http://www.github.com/amorano/jovimetrix
Creation
"""

import os
from pathlib import Path
from typing import Any, Tuple

import torch
from loguru import logger

try:
    from server import PromptServer
    from aiohttp import web
except:
    pass
from comfy.utils import ProgressBar

from Jovimetrix import comfy_message, parse_reset, ROOT
from Jovimetrix.sup.lexicon import JOVImageNode, Lexicon
from Jovimetrix.sup.util import load_file, parse_param, EnumConvertType
from Jovimetrix.sup.image import cv2tensor_full, image_convert, tensor2cv, MIN_IMAGE_SIZE
from Jovimetrix.sup.shader import shader_meta, CompileException, GLSLShader

# =============================================================================

JOV_ROOT_GLSL = ROOT / 'res' / 'glsl'
GLSL_PROGRAMS = {
    "vertex": { "NONE": None },
    "fragment": { "NONE": None }
}

GLSL_PROGRAMS["vertex"].update({str(f.relative_to(JOV_ROOT_GLSL)): str(f) for f in Path(JOV_ROOT_GLSL).rglob('*.vert')})
if (USER_GLSL := os.getenv("JOV_GLSL", None)) is not None:
    GLSL_PROGRAMS["vertex"].update({str(f.relative_to(USER_GLSL)): str(f) for f in Path(USER_GLSL).rglob('*.vert')})

GLSL_PROGRAMS["fragment"].update({str(f.relative_to(JOV_ROOT_GLSL)): str(f) for f in Path(JOV_ROOT_GLSL).rglob('*.glsl')})
if USER_GLSL is not None:
    GLSL_PROGRAMS["fragment"].update({str(f.relative_to(USER_GLSL)): str(f) for f in Path(USER_GLSL).rglob('*.glsl')})

logger.info(f"  vertex programs: {len(GLSL_PROGRAMS['vertex'])}")
logger.info(f"fragment programs: {len(GLSL_PROGRAMS['fragment'])}")

JOV_CATEGORY = "CREATE"

# =============================================================================

try:
    @PromptServer.instance.routes.get("/jovimetrix/glsl")
    async def jovimetrix_glsl_list(request) -> Any:
        ret = {k:[kk for kk, vv in v.items() \
                  if kk not in ['NONE'] and vv not in [None] and Path(vv).exists()]
               for k, v in GLSL_PROGRAMS.items()}
        return web.json_response(ret)

    @PromptServer.instance.routes.get("/jovimetrix/glsl/{shader}")
    async def jovimetrix_glsl_raw(request, shader:str) -> Any:
        if (program := GLSL_PROGRAMS.get(shader, None)) is None:
            return web.json_response(f"no program {shader}")
        response = load_file(program)
        return web.json_response(response)

    @PromptServer.instance.routes.post("/jovimetrix/glsl")
    async def jovimetrix_glsl(request) -> Any:
        json_data = await request.json()
        response = {k:None for k in json_data.keys()}
        for who in response.keys():
            if (programs := GLSL_PROGRAMS.get(who, None)) is None:
                logger.warning(f"no program type {who}")
                continue
            fname = json_data[who]
            if (data := programs.get(fname, None)) is not None:
                response[who] = load_file(data)
            else:
                logger.warning(f"no glsl shader entry {fname}")

        return web.json_response(response)
except Exception as e:
    logger.error(e)

class GLSLNodeBase(JOVImageNode):
    CATEGORY = f"JOVIMETRIX 🔺🟩🔵/GLSL"
    FRAGMENT = None
    VERTEX = None

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        d = super().INPUT_TYPES()
        d.update({
            "optional": {
                Lexicon.WH: ("VEC2", {"default": (512, 512), "min": MIN_IMAGE_SIZE, "step": 1,}),
                Lexicon.MATTE: ("VEC4", {"default": (0, 0, 0, 255), "step": 1,
                                         "label": [Lexicon.R, Lexicon.G, Lexicon.B, Lexicon.A], "rgb": True}),
                Lexicon.BATCH: ("INT", {"default": 0, "step": 1, "min": 0, "max": 1048576}),
                Lexicon.FPS: ("INT", {"default": 24, "step": 1, "min": 1, "max": 120}),
                Lexicon.TIME: ("FLOAT", {"default": 0, "step": 0.001, "min": 0, "precision": 4}),
                Lexicon.WAIT: ("BOOLEAN", {"default": False}),
                Lexicon.RESET: ("BOOLEAN", {"default": False})
            }
        })
        return Lexicon._parse(d, cls)

    @classmethod
    def IS_CHANGED(cls, **kw) -> float:
        return float("nan")

    def __init__(self, *arg, **kw) -> None:
        super().__init__(*arg, **kw)
        self.__glsl = GLSLShader()
        self.__delta = 0

    def run(self, ident, **kw) -> tuple[torch.Tensor]:
        wihi = parse_param(kw, Lexicon.WH, EnumConvertType.VEC2INT, [(512, 512)], MIN_IMAGE_SIZE)[0]
        matte = parse_param(kw, Lexicon.MATTE, EnumConvertType.VEC4INT, [(0, 0, 0, 255)], 0, 255)[0]
        delta = parse_param(kw, Lexicon.TIME, EnumConvertType.FLOAT, 0)[0]
        batch = parse_param(kw, Lexicon.BATCH, EnumConvertType.INT, 1, 0, 1048576)[0]
        fps = parse_param(kw, Lexicon.FPS, EnumConvertType.INT, 24, 1, 120)[0]
        wait = parse_param(kw, Lexicon.WAIT, EnumConvertType.BOOLEAN, False)[0]
        reset = parse_param(kw, Lexicon.RESET, EnumConvertType.BOOLEAN, False)[0]

        variables = kw.copy()
        for p in [Lexicon.TIME, Lexicon.BATCH, Lexicon.FPS, Lexicon.WAIT, Lexicon.RESET, Lexicon.WH, Lexicon.MATTE, Lexicon.PROG_VERT, Lexicon.PROG_FRAG]:
            variables.pop(p, None)

        self.__glsl.size = wihi
        self.__glsl.fps = fps
        try:
            self.__glsl.program(self.VERTEX, self.FRAGMENT)
        except CompileException as e:
            comfy_message(ident, "jovi-glsl-error", {"id": ident, "e": str(e)})
            logger.error(e)
            return

        if batch > 0:
            self.__delta = delta

        if parse_reset(ident) > 0 or reset:
            self.__delta = 0
        step = 1. / fps

        images = []
        pbar = ProgressBar(batch)
        count = batch if batch > 0 else 1
        for idx in range(count):
            vars = {}
            for k, v in variables.items():
                var = v if not isinstance(v, (list, tuple,)) else v[idx] if idx < len(v) else v[-1]
                if isinstance(var, (torch.Tensor)):
                    var = tensor2cv(var)
                    var = image_convert(var, 4)
                vars[k] = var

            image = self.__glsl.render(self.__delta, **vars)
            image = cv2tensor_full(image, matte)
            images.append(image)
            if not wait:
                self.__delta += step
                # if batch == 0:
                comfy_message(ident, "jovi-glsl-time", {"id": ident, "t": self.__delta})
            pbar.update_absolute(idx)
        return [torch.cat(i, dim=0) for i in zip(*images)]

class GLSLNode(GLSLNodeBase):
    NAME = "GLSL (JOV) 🍩"
    CATEGORY = f"JOVIMETRIX 🔺🟩🔵/{JOV_CATEGORY}"
    DESCRIPTION = """
Execute custom GLSL (OpenGL Shading Language) fragment shaders to generate images or apply effects. GLSL is a high-level shading language used for graphics programming, particularly in the context of rendering images or animations. This node allows for real-time rendering of shader effects, providing flexibility and creative control over image processing pipelines. It takes advantage of GPU acceleration for efficient computation, enabling the rapid generation of complex visual effects.
"""

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        d = super().INPUT_TYPES()
        opts = d.get('optional', {})
        opts.update({
            Lexicon.PROG_VERT: ("STRING", {"default": GLSLShader.PROG_VERTEX, "multiline": True, "dynamicPrompts": False}),
            Lexicon.PROG_FRAG: ("STRING", {"default": GLSLShader.PROG_FRAGMENT, "multiline": True, "dynamicPrompts": False}),
        })
        d['optional'] = opts
        return Lexicon._parse(d, cls)

    def run(self, ident, **kw) -> tuple[torch.Tensor]:
        self.VERTEX = parse_param(kw, Lexicon.PROG_VERT, EnumConvertType.STRING, "")[0]
        self.FRAGMENT = parse_param(kw, Lexicon.PROG_FRAG, EnumConvertType.STRING, "")[0]
        return super().run(ident, **kw)

class GLSLNodeDynamic(GLSLNodeBase):
    @classmethod
    def INPUT_TYPES(cls) -> dict:
        d = super().INPUT_TYPES()
        opts = d.get('optional', {})
        opts.update({
            Lexicon.PROG_FRAG: ("JDATABUCKET", {"fragment": cls.FRAGMENT}),
        })
        d['optional'] = opts
        return Lexicon._parse(d, cls)

def import_dynamic() -> Tuple[str,...]:
    ret = []
    for name, fname in GLSL_PROGRAMS['fragment'].items():
        if name == 'NONE': continue
        if (shader := load_file(fname)) is None:
            logger.error(f"missing shader file {fname}")
            continue

        meta = shader_meta(shader)
        name = meta.get('name', name.split('.')[0])
        class_name = f'GLSLNode_{name.title()}'
        class_def = type(class_name, (GLSLNodeDynamic,), {
            "NAME": f'GLSL {name} (JOV) 🧙🏽'.upper(),
            "DESCRIPTION": meta.get('desc', name),
            "FRAGMENT": shader
        })

        #def init_method(self, *arg, **kw) -> None:
       #     super(class_def, self).__init__(*arg, **kw)
        #    self.FRAGMENT = shader

        #class_def.__init__ = init_method
        ret.append((class_name, class_def,))
    return ret
