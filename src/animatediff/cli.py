import glob
import logging
import os.path
from datetime import datetime
from pathlib import Path
from typing import Annotated, Optional

import torch
import typer
from diffusers.utils.logging import set_verbosity_error as set_diffusers_verbosity_error
from rich.logging import RichHandler

import re
from PIL import Image
from shutil import copyfile, rmtree
import numpy as np

from animatediff import __version__, console, get_dir
from animatediff.generate import controlnet_preprocess, create_pipeline, create_us_pipeline, ip_adapter_preprocess, load_controlnet_models, run_inference, run_upscale, unload_controlnet_models
from animatediff.pipelines import AnimationPipeline, load_text_embeddings
from animatediff.settings import CKPT_EXTENSIONS, InferenceConfig, ModelConfig, get_infer_config, get_model_config
from animatediff.utils.civitai2config import generate_config_from_civitai_info
from animatediff.utils.model import checkpoint_to_pipeline, get_base_model
from animatediff.utils.pipeline import get_context_params, send_to_device
from animatediff.utils.util import extract_frames, is_v2_motion_module, path_from_cwd, save_frames, save_imgs, save_video
from animatediff.utils.wild_card import replace_wild_card

cli: typer.Typer = typer.Typer(
    context_settings=dict(help_option_names=["-h", "--help"]),
    rich_markup_mode="rich",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
data_dir = get_dir("data")
checkpoint_dir = data_dir.joinpath("models/sd")
pipeline_dir = data_dir.joinpath("models/huggingface")


try:
    import google.colab

    IN_COLAB = True
except:
    IN_COLAB = False

if IN_COLAB:
    import sys

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
else:
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[
            RichHandler(console=console, rich_tracebacks=True),
        ],
        datefmt="%H:%M:%S",
        force=True,
    )

logger = logging.getLogger(__name__)


try:
    from animatediff.rife import app as rife_app

    cli.add_typer(rife_app, name="rife")
except ImportError:
    logger.debug("RIFE not available, skipping...", exc_info=True)
    rife_app = None


from animatediff.stylize import stylize

cli.add_typer(stylize, name="stylize")


# mildly cursed globals to allow for reuse of the pipeline if we're being called as a module
g_pipeline: Optional[AnimationPipeline] = None
last_model_path: Optional[Path] = None


def version_callback(value: bool):
    if value:
        console.print(f"AnimateDiff v{__version__}")
        raise typer.Exit()


@cli.command()
def generate(
    model_name_or_path: Annotated[
        Path,
        typer.Option(
            ...,
            "--model-path",
            "-m",
            path_type=Path,
            help="Base model to use (path or HF repo ID). You probably don't need to change this.",
        ),
    ] = Path("runwayml/stable-diffusion-v1-5"),
    config_path: Annotated[
        Path,
        typer.Option(
            "--config-path",
            "-c",
            path_type=Path,
            exists=True,
            readable=True,
            dir_okay=False,
            help="Path to a prompt configuration JSON file",
        ),
    ] = Path("config/prompts/01-ToonYou.json"),
    width: Annotated[
        int,
        typer.Option(
            "--width",
            "-W",
            min=64,
            max=3840,
            help="Width of generated frames",
            rich_help_panel="Generation",
        ),
    ] = 512,
    height: Annotated[
        int,
        typer.Option(
            "--height",
            "-H",
            min=64,
            max=2160,
            help="Height of generated frames",
            rich_help_panel="Generation",
        ),
    ] = 512,
    length: Annotated[
        int,
        typer.Option(
            "--length",
            "-L",
            min=1,
            max=9999,
            help="Number of frames to generate",
            rich_help_panel="Generation",
        ),
    ] = 16,
    context: Annotated[
        Optional[int],
        typer.Option(
            "--context",
            "-C",
            min=1,
            max=32,
            help="Number of frames to condition on (default: max of <length> or 32). max for motion module v1 is 24",
            show_default=False,
            rich_help_panel="Generation",
        ),
    ] = None,
    overlap: Annotated[
        Optional[int],
        typer.Option(
            "--overlap",
            "-O",
            min=1,
            max=12,
            help="Number of frames to overlap in context (default: context//4)",
            show_default=False,
            rich_help_panel="Generation",
        ),
    ] = None,
    stride: Annotated[
        Optional[int],
        typer.Option(
            "--stride",
            "-S",
            min=0,
            max=8,
            help="Max motion stride as a power of 2 (default: 0)",
            show_default=False,
            rich_help_panel="Generation",
        ),
    ] = None,
    repeats: Annotated[
        int,
        typer.Option(
            "--repeats",
            "-r",
            min=1,
            max=99,
            help="Number of times to repeat the prompt (default: 1)",
            show_default=False,
            rich_help_panel="Generation",
        ),
    ] = 1,
    device: Annotated[
        str,
        typer.Option("--device", "-d", help="Device to run on (cpu, cuda, cuda:id)", rich_help_panel="Advanced"),
    ] = "cuda",
    use_xformers: Annotated[
        bool,
        typer.Option(
            "--xformers",
            "-x",
            is_flag=True,
            help="Use XFormers instead of SDP Attention",
            rich_help_panel="Advanced",
        ),
    ] = False,
    force_half_vae: Annotated[
        bool,
        typer.Option(
            "--half-vae",
            is_flag=True,
            help="Force VAE to use fp16 (not recommended)",
            rich_help_panel="Advanced",
        ),
    ] = False,
    out_dir: Annotated[
        Path,
        typer.Option(
            "--out-dir",
            "-o",
            path_type=Path,
            file_okay=False,
            help="Directory for output folders (frames, gifs, etc)",
            rich_help_panel="Output",
        ),
    ] = Path("output/"),
    no_frames: Annotated[
        bool,
        typer.Option(
            "--no-frames",
            "-N",
            is_flag=True,
            help="Don't save frames, only the animation",
            rich_help_panel="Output",
        ),
    ] = False,
    save_merged: Annotated[
        bool,
        typer.Option(
            "--save-merged",
            "-m",
            is_flag=True,
            help="Save a merged animation of all prompts",
            rich_help_panel="Output",
        ),
    ] = False,
    input_image: Annotated[
        str,
        typer.Option(
            "--input-image",
            "-I",
            is_flag=True,
            help="input png image",
            rich_help_panel="Output",
        ),
    ] = None,
    input_image_model: Annotated[
        bool,
        typer.Option(
            "--input-image-model",
            "-IM",
            is_flag=False,
            help="input image use model",
            rich_help_panel="Output",
        ),
    ] = True,
    input_image_fix: Annotated[
        int,
        typer.Option(
            "--input-image-fix",
            "-IF",
            is_flag=False,
            help="input png image fix",
            rich_help_panel="Output",
        ),
    ] = 1,
    input_image_flip: Annotated[
        bool,
        typer.Option(
            "--input-image-flip",
            "-IFL",
            is_flag=True,
            help="input png image flip",
            rich_help_panel="Output",
        ),
    ] = False,
    version: Annotated[
        Optional[bool],
        typer.Option(
            "--version",
            "-v",
            callback=version_callback,
            is_eager=True,
            is_flag=True,
            help="Show version",
        ),
    ] = None,
):
    """
    Do the thing. Make the animation happen. Waow.
    """

    # be quiet, diffusers. we care not for your safety checker
    set_diffusers_verbosity_error()

    config_path = config_path.absolute()
    logger.info(f"Using generation config: {path_from_cwd(config_path)}")
    model_config: ModelConfig = get_model_config(config_path)
    is_v2 = is_v2_motion_module(model_config.motion_module)
    infer_config: InferenceConfig = get_infer_config(is_v2)

    # set sane defaults for context, overlap, and stride if not supplied
    context, overlap, stride = get_context_params(length, context, overlap, stride)

    if (not is_v2) and (context > 24):
        logger.warning("For motion module v1, the maximum value of context is 24. Set to 24")
        context = 24

    # turn the device string into a torch.device
    device: torch.device = torch.device(device)

    # Get the base model if we don't have it already
    logger.info(f"Using base model: {model_name_or_path}")
    base_model_path: Path = get_base_model(model_name_or_path, local_dir=get_dir("data/models/huggingface"))

    # old prompt
    for idx, prompt in enumerate(model_config.prompt):
        model_config.prompt_map[f"{idx}"] = prompt

    # input image
    if input_image != None:
        im = Image.open(input_image)
        im.load()
        if "parameters" in im.info:
            prompt = ""
            neg_prompt = ""
            extra = ""
            start_neg_prompt = False
            parameters = im.info["parameters"]
            for line in parameters.split("\n"):
                if start_neg_prompt == True:
                    if line.startswith("Steps:"):
                        extra = line
                        break
                    else:
                        neg_prompt = neg_prompt + line
                else:
                    if line.startswith("Negative prompt:"):
                        start_neg_prompt = True
                        neg_prompt = line[len("Negative prompt:") :]
                    else:
                        prompt = prompt + line

            if prompt != "":
                print(f"[prompt] {prompt}")
                for index, p in model_config.prompt_map.items():
                    model_config.prompt_map[index] = prompt + " " + p
            if neg_prompt != "":
                print(f"[neg_prompt] {neg_prompt}")
                for index, p in enumerate(model_config.n_prompt):
                    model_config.n_prompt[index] = neg_prompt + " " + p
            if extra != "":
                print(f"[extra] {extra}")
                setting = dict()
                x = extra.split(",")
                for i in x:
                    try:
                        key = i[: i.index(":")]
                        val = i[i.index(":") + 1 :]
                        setting[str(key).strip()] = str(val).strip()
                    except Exception as e:
                        print("error", e)

                if "Seed" in setting:
                    for index, seed in enumerate(model_config.seed):
                        model_config.seed[index] = int(float(setting["Seed"]))
                        if model_config.seed[index] == -1:
                            try:
                                filename = os.path.splitext(os.path.basename(input_image))[0]
                                fileseed = filename.split("-")[1]
                                model_config.seed[index] = int(fileseed)
                            except Exception as e:
                                print("error", e)
                if "Model" in setting and input_image_model:
                    model_path = os.path.join(os.path.dirname(model_config.path), setting["Model"] + ".safetensors")
                    print(f"{model_path=}")
                    if os.path.exists(model_path):
                        model_config.path = Path(model_path)

        if os.path.basename(config_path).startswith("prompt_runtime"):
            for key in model_config.controlnet_map:
                if key.startswith("controlnet_"):
                    if data_dir.joinpath(f"controlnet_image/runtime/{key}").is_dir():
                        rmtree(data_dir.joinpath(f"controlnet_image/runtime/{key}"))
                    if model_config.controlnet_map[key]["enable"]:
                        if not data_dir.joinpath(f"controlnet_image/runtime/{key}").is_dir():
                            data_dir.joinpath(f"controlnet_image/runtime/{key}").mkdir()

                        if input_image_fix > 0:
                            if input_image_flip:
                                im_temp = im.transpose(Image.FLIP_LEFT_RIGHT)
                                im_temp.save(data_dir.joinpath(f"controlnet_image/runtime/{key}/0000.png"))
                            else:
                                copyfile(input_image, data_dir.joinpath(f"controlnet_image/runtime/{key}/0000.png"))

                            if key == "controlnet_openpose":
                                if input_image_fix > 2:
                                    if input_image_flip:
                                        im_temp = im.transpose(Image.FLIP_LEFT_RIGHT)
                                        im_temp.save(data_dir.joinpath(f"controlnet_image/runtime/{key}/0016.png"))
                                    else:
                                        copyfile(input_image, data_dir.joinpath(f"controlnet_image/runtime/{key}/0016.png"))
                                elif input_image_fix > 1:
                                    if input_image_flip:
                                        im_temp = im.transpose(Image.FLIP_LEFT_RIGHT)
                                        im_temp.save(data_dir.joinpath(f"controlnet_image/runtime/{key}/0008.png"))
                                    else:
                                        copyfile(input_image, data_dir.joinpath(f"controlnet_image/runtime/{key}/0008.png"))

    # lora parsing
    new_prompts = {}
    lora_map = {}
    for key, prompt in model_config.prompt_map.items():
        items = re.findall(r"<lora:([^:]+):([0-9.]+)>", prompt)
        for lora, weight in items:
            lora_map[lora] = weight
        prompt = re.sub(r"<lora:([^:]+):([0-9.]+)>", "", prompt)
        new_prompts[key] = prompt
    model_config.prompt_map = new_prompts

    if model_config.lora_path != None:
        lora_files = sorted(glob.glob(os.path.join(model_config.lora_path, "**", "*.safetensors"), recursive=True))
        for lora_file in lora_files:
            lora_filename = os.path.basename(lora_file)
            lora_name = os.path.splitext(lora_filename)[0]
            if lora_name in lora_map:
                model_config.lora_map[lora_file] = float(lora_map[lora_name])

    print(f"{model_config.seed=}")
    print(f"{model_config.prompt_map=}")
    print(f"{model_config.lora_map=}")

    # get a timestamp for the output directory
    time_str = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    # make the output directory
    save_dir = out_dir.joinpath(f"{time_str}-{model_config.save_name}")
    save_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Will save outputs to ./{path_from_cwd(save_dir)}")

    controlnet_image_map, controlnet_type_map, controlnet_ref_map = controlnet_preprocess(model_config.controlnet_map, width, height, length, save_dir, device)
    ip_adapter_map = ip_adapter_preprocess(model_config.ip_adapter_map, width, height, length, save_dir)

    # beware the pipeline
    global g_pipeline
    global last_model_path
    if g_pipeline is None or last_model_path != model_config.path.resolve():
        g_pipeline = create_pipeline(
            base_model=base_model_path,
            model_config=model_config,
            infer_config=infer_config,
            use_xformers=use_xformers,
        )
        last_model_path = model_config.path.resolve()
    else:
        logger.info("Pipeline already loaded, skipping initialization")
        # reload TIs; create_pipeline does this for us, but they may have changed
        # since load time if we're being called from another package
        load_text_embeddings(g_pipeline)

    load_controlnet_models(pipe=g_pipeline, model_config=model_config)

    if g_pipeline.device == device:
        logger.info("Pipeline already on the correct device, skipping device transfer")
    else:
        g_pipeline = send_to_device(g_pipeline, device, freeze=True, force_half=force_half_vae, compile=model_config.compile)

    # save raw config to output directory
    save_config_path = save_dir.joinpath("raw_prompt.json")
    save_config_path.write_text(model_config.json(indent=4), encoding="utf-8")

    # fix seed
    for i, s in enumerate(model_config.seed):
        if s == -1:
            model_config.seed[i] = torch.seed()

    # wildcard conversion
    wild_card_dir = get_dir("wildcards")
    for k in model_config.prompt_map.keys():
        model_config.prompt_map[k] = replace_wild_card(model_config.prompt_map[k], wild_card_dir)

    if model_config.head_prompt:
        model_config.head_prompt = replace_wild_card(model_config.head_prompt, wild_card_dir)
    if model_config.tail_prompt:
        model_config.tail_prompt = replace_wild_card(model_config.tail_prompt, wild_card_dir)

    # save config to output directory
    logger.info("Saving prompt config to output directory")
    save_config_path = save_dir.joinpath("prompt.json")
    save_config_path.write_text(model_config.json(indent=4), encoding="utf-8")

    num_negatives = len(model_config.n_prompt)
    num_seeds = len(model_config.seed)
    gen_total = repeats  # total number of generations

    logger.info("Initialization complete!")
    logger.info(f"Generating {gen_total} animations")
    outputs = []

    gen_num = 0  # global generation index

    # repeat the prompts if we're doing multiple runs
    for _ in range(repeats):
        if model_config.prompt_map:
            # get the index of the prompt, negative, and seed
            idx = gen_num
            logger.info(f"Running generation {gen_num + 1} of {gen_total}")

            # allow for reusing the same negative prompt(s) and seed(s) for multiple prompts
            n_prompt = model_config.n_prompt[idx % num_negatives]
            seed = model_config.seed[idx % num_seeds]

            # duplicated in run_inference, but this lets us use it for frame save dirs
            # TODO: Move gif Output out of run_inference...
            if seed == -1:
                seed = torch.seed()
            logger.info(f"Generation seed: {seed}")

            prompt_map = {}
            for k in model_config.prompt_map.keys():
                if int(k) < length:
                    pr = model_config.prompt_map[k]
                    if model_config.head_prompt:
                        pr = model_config.head_prompt + "," + pr
                    if model_config.tail_prompt:
                        pr = pr + "," + model_config.tail_prompt

                    prompt_map[int(k)] = pr

            output = run_inference(
                pipeline=g_pipeline,
                prompt="this is dummy string",
                n_prompt=n_prompt,
                seed=seed,
                steps=model_config.steps,
                guidance_scale=model_config.guidance_scale,
                width=width,
                height=height,
                duration=length,
                idx=gen_num,
                out_dir=save_dir,
                context_frames=context,
                context_overlap=overlap,
                context_stride=stride,
                clip_skip=model_config.clip_skip,
                prompt_map=prompt_map,
                controlnet_map=model_config.controlnet_map,
                controlnet_image_map=controlnet_image_map,
                controlnet_type_map=controlnet_type_map,
                controlnet_ref_map=controlnet_ref_map,
                no_frames=no_frames,
                ip_adapter_map=ip_adapter_map,
                output_map=model_config.output,
            )
            outputs.append(output)
            torch.cuda.empty_cache()

            # increment the generation number
            gen_num += 1

    unload_controlnet_models(pipe=g_pipeline)

    logger.info("Generation complete!")
    if save_merged:
        logger.info("Output merged output video...")
        merged_output = torch.concat(outputs, dim=0)
        save_video(merged_output, save_dir.joinpath("final.gif"))

    logger.info("Done, exiting...")
    cli.info

    return save_dir


@cli.command()
def tile_upscale(
    frames_dir: Annotated[
        Path,
        typer.Argument(path_type=Path, file_okay=False, exists=True, help="Path to source frames directory"),
    ] = ...,
    model_name_or_path: Annotated[
        Path,
        typer.Option(
            ...,
            "--model-path",
            "-m",
            path_type=Path,
            help="Base model to use (path or HF repo ID). You probably don't need to change this.",
        ),
    ] = Path("runwayml/stable-diffusion-v1-5"),
    config_path: Annotated[
        Path,
        typer.Option(
            "--config-path",
            "-c",
            path_type=Path,
            exists=True,
            readable=True,
            dir_okay=False,
            help="Path to a prompt configuration JSON file. default is frames_dir/../prompt.json",
        ),
    ] = None,
    width: Annotated[
        int,
        typer.Option(
            "--width",
            "-W",
            min=-1,
            max=3840,
            help="Width of generated frames",
            rich_help_panel="Generation",
        ),
    ] = -1,
    height: Annotated[
        int,
        typer.Option(
            "--height",
            "-H",
            min=-1,
            max=2160,
            help="Height of generated frames",
            rich_help_panel="Generation",
        ),
    ] = -1,
    device: Annotated[
        str,
        typer.Option("--device", "-d", help="Device to run on (cpu, cuda, cuda:id)", rich_help_panel="Advanced"),
    ] = "cuda",
    use_xformers: Annotated[
        bool,
        typer.Option(
            "--xformers",
            "-x",
            is_flag=True,
            help="Use XFormers instead of SDP Attention",
            rich_help_panel="Advanced",
        ),
    ] = False,
    force_half_vae: Annotated[
        bool,
        typer.Option(
            "--half-vae",
            is_flag=True,
            help="Force VAE to use fp16 (not recommended)",
            rich_help_panel="Advanced",
        ),
    ] = False,
    out_dir: Annotated[
        Path,
        typer.Option(
            "--out-dir",
            "-o",
            path_type=Path,
            file_okay=False,
            help="Directory for output folders (frames, gifs, etc)",
            rich_help_panel="Output",
        ),
    ] = Path("upscaled/"),
    no_frames: Annotated[
        bool,
        typer.Option(
            "--no-frames",
            "-N",
            is_flag=True,
            help="Don't save frames, only the animation",
            rich_help_panel="Output",
        ),
    ] = False,
):
    """Upscale frames using controlnet tile"""
    # be quiet, diffusers. we care not for your safety checker
    set_diffusers_verbosity_error()

    if width < 0 and height < 0:
        raise ValueError(f"invalid width,height: {width},{height} \n At least one of them must be specified.")

    if not config_path:
        tmp = frames_dir.parent.joinpath("prompt.json")
        if tmp.is_file():
            config_path = tmp

    config_path = config_path.absolute()
    logger.info(f"Using generation config: {path_from_cwd(config_path)}")
    model_config: ModelConfig = get_model_config(config_path)
    infer_config: InferenceConfig = get_infer_config(is_v2_motion_module(model_config.motion_module))
    frames_dir = frames_dir.absolute()

    # turn the device string into a torch.device
    device: torch.device = torch.device(device)

    # get a timestamp for the output directory
    time_str = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    # make the output directory
    save_dir = out_dir.joinpath(f"{time_str}-{model_config.save_name}")
    save_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Will save outputs to ./{path_from_cwd(save_dir)}")

    if "controlnet_tile" not in model_config.upscale_config:
        model_config.upscale_config["controlnet_tile"] = {
            "enable": True,
            "controlnet_conditioning_scale": 1.0,
            "guess_mode": False,
            "control_guidance_start": 0.0,
            "control_guidance_end": 1.0,
        }

    use_controlnet_ref = False
    use_controlnet_tile = False
    use_controlnet_line_anime = False
    use_controlnet_ip2p = False

    if model_config.upscale_config:
        use_controlnet_ref = model_config.upscale_config["controlnet_ref"]["enable"] if "controlnet_ref" in model_config.upscale_config else False
        use_controlnet_tile = model_config.upscale_config["controlnet_tile"]["enable"] if "controlnet_tile" in model_config.upscale_config else False
        use_controlnet_line_anime = model_config.upscale_config["controlnet_line_anime"]["enable"] if "controlnet_line_anime" in model_config.upscale_config else False
        use_controlnet_ip2p = model_config.upscale_config["controlnet_ip2p"]["enable"] if "controlnet_ip2p" in model_config.upscale_config else False

    if use_controlnet_tile == False:
        if use_controlnet_line_anime == False:
            if use_controlnet_ip2p == False:
                raise ValueError(f"At least one of them should be enabled. {use_controlnet_tile=}, {use_controlnet_line_anime=}, {use_controlnet_ip2p=}")

    # beware the pipeline
    us_pipeline = create_us_pipeline(
        model_config=model_config,
        infer_config=infer_config,
        use_xformers=use_xformers,
        use_controlnet_ref=use_controlnet_ref,
        use_controlnet_tile=use_controlnet_tile,
        use_controlnet_line_anime=use_controlnet_line_anime,
        use_controlnet_ip2p=use_controlnet_ip2p,
    )

    if us_pipeline.device == device:
        logger.info("Pipeline already on the correct device, skipping device transfer")
    else:
        us_pipeline = send_to_device(us_pipeline, device, freeze=True, force_half=force_half_vae, compile=model_config.compile)

    model_config.result = {"original_frames": str(frames_dir)}

    # save config to output directory
    logger.info("Saving prompt config to output directory")
    save_config_path = save_dir.joinpath("prompt.json")
    save_config_path.write_text(model_config.json(indent=4), encoding="utf-8")

    num_prompts = 1
    num_negatives = len(model_config.n_prompt)
    num_seeds = len(model_config.seed)

    logger.info("Initialization complete!")

    gen_num = 0  # global generation index

    org_images = sorted(glob.glob(os.path.join(frames_dir, "[0-9]*.png"), recursive=False))
    length = len(org_images)

    if model_config.prompt_map:
        # get the index of the prompt, negative, and seed
        idx = gen_num % num_prompts
        logger.info(f"Running generation {gen_num + 1} of {1} (prompt {idx + 1})")

        # allow for reusing the same negative prompt(s) and seed(s) for multiple prompts
        n_prompt = model_config.n_prompt[idx % num_negatives]
        seed = seed = model_config.seed[idx % num_seeds]

        if seed == -1:
            seed = torch.seed()
        logger.info(f"Generation seed: {seed}")

        prompt_map = {}
        for k in model_config.prompt_map.keys():
            if int(k) < length:
                pr = model_config.prompt_map[k]
                if model_config.head_prompt:
                    pr = model_config.head_prompt + "," + pr
                if model_config.tail_prompt:
                    pr = pr + "," + model_config.tail_prompt

                prompt_map[int(k)] = pr

        if model_config.upscale_config:
            upscaled_output = run_upscale(
                org_imgs=org_images,
                pipeline=us_pipeline,
                prompt_map=prompt_map,
                n_prompt=n_prompt,
                seed=seed,
                steps=model_config.steps,
                guidance_scale=model_config.guidance_scale,
                clip_skip=model_config.clip_skip,
                us_width=width,
                us_height=height,
                idx=gen_num,
                out_dir=save_dir,
                upscale_config=model_config.upscale_config,
                use_controlnet_ref=use_controlnet_ref,
                use_controlnet_tile=use_controlnet_tile,
                use_controlnet_line_anime=use_controlnet_line_anime,
                use_controlnet_ip2p=use_controlnet_ip2p,
            )
            torch.cuda.empty_cache()
            if upscaled_output is not None:
                if no_frames is not True:
                    save_imgs(upscaled_output, save_dir.joinpath(f"{gen_num:02d}-{seed}-upscaled"))

        # increment the generation number
        gen_num += 1

    logger.info("Generation complete!")

    logger.info("Done, exiting...")
    cli.info

    return save_dir


@cli.command()
def civitai2config(
    lora_dir: Annotated[
        Path,
        typer.Argument(path_type=Path, file_okay=False, exists=True, help="Path to loras directory"),
    ] = ...,
    config_org: Annotated[
        Path,
        typer.Option(
            "--config-org",
            "-c",
            path_type=Path,
            dir_okay=False,
            exists=True,
            help="Path to original config file",
        ),
    ] = Path("config/prompts/prompt_travel.json"),
    out_dir: Annotated[
        Optional[Path],
        typer.Option(
            "--out-dir",
            "-o",
            path_type=Path,
            file_okay=False,
            help="Target directory for generated configs",
        ),
    ] = Path("config/prompts/converted/"),
    lora_weight: Annotated[
        float,
        typer.Option(
            "--lora_weight",
            "-l",
            min=0.0,
            max=3.0,
            help="Lora weight",
        ),
    ] = 0.75,
):
    """Generate config file from *.civitai.info"""

    out_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Generate config files from: {lora_dir}")
    generate_config_from_civitai_info(lora_dir, config_org, out_dir, lora_weight)
    logger.info(f"saved at: {out_dir.absolute()}")


@cli.command()
def convert(
    checkpoint: Annotated[
        Path,
        typer.Option(
            "--checkpoint",
            "-i",
            path_type=Path,
            dir_okay=False,
            exists=True,
            help="Path to a model checkpoint file",
        ),
    ] = ...,
    out_dir: Annotated[
        Optional[Path],
        typer.Option(
            "--out-dir",
            "-o",
            path_type=Path,
            file_okay=False,
            help="Target directory for converted model",
        ),
    ] = None,
):
    """Convert a StableDiffusion checkpoint into a Diffusers pipeline"""
    logger.info(f"Converting checkpoint: {checkpoint}")
    _, pipeline_dir = checkpoint_to_pipeline(checkpoint, target_dir=out_dir)
    logger.info(f"Converted to HuggingFace pipeline at {pipeline_dir}")


@cli.command()
def merge(
    checkpoint: Annotated[
        Path,
        typer.Option(
            "--checkpoint",
            "-i",
            path_type=Path,
            dir_okay=False,
            exists=True,
            help="Path to a model checkpoint file",
        ),
    ] = ...,
    out_dir: Annotated[
        Optional[Path],
        typer.Option(
            "--out-dir",
            "-o",
            path_type=Path,
            file_okay=False,
            help="Target directory for converted model",
        ),
    ] = None,
):
    """Convert a StableDiffusion checkpoint into an AnimationPipeline"""
    raise NotImplementedError("Sorry, haven't implemented this yet!")

    # if we have a checkpoint, convert it to HF automagically
    if checkpoint.is_file() and checkpoint.suffix in CKPT_EXTENSIONS:
        logger.info(f"Loading model from checkpoint: {checkpoint}")
        # check if we've already converted this model
        model_dir = pipeline_dir.joinpath(checkpoint.stem)
        if model_dir.joinpath("model_index.json").exists():
            # we have, so just use that
            logger.info("Found converted model in {model_dir}, will not convert")
            logger.info("Delete the output directory to re-run conversion.")
        else:
            # we haven't, so convert it
            logger.info("Converting checkpoint to HuggingFace pipeline...")
            g_pipeline, model_dir = checkpoint_to_pipeline(checkpoint)
    logger.info("Done!")
