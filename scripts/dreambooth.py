import gc
import logging
import math
import os
import traceback

import gradio
import torch
import torch.utils.checkpoint
from diffusers.utils import logging as dl

from extensions.sd_dreambooth_extension.dreambooth import dream_state
from extensions.sd_dreambooth_extension.dreambooth.db_concept import Concept
from extensions.sd_dreambooth_extension.dreambooth.db_config import from_file
from extensions.sd_dreambooth_extension.dreambooth.finetune_utils import ImageBuilder, PromptData
from extensions.sd_dreambooth_extension.dreambooth.utils import reload_system_models, unload_system_models, printm, \
    isset, get_images, get_lora_models
from modules import shared, devices

try:
    cmd_dreambooth_models_path = shared.cmd_opts.dreambooth_models_path
except:
    cmd_dreambooth_models_path = None

logger = logging.getLogger(__name__)
console = logging.StreamHandler()
console.setLevel(logging.DEBUG)
logger.addHandler(console)
logger.setLevel(logging.DEBUG)
dl.set_verbosity_error()

mem_record = {}


def training_wizard_person(model_dir):
    return training_wizard(
        model_dir,
        is_person=True)


def training_wizard(model_dir, is_person=False):
    """
    Calculate the number of steps based on our learning rate, return the following:
    db_max_train_steps,
    db_num_train_epochs,
    c1_max_steps,
    c1_num_class_images,
    c2_max_steps,
    c2_num_class_images,
    c3_max_steps,
    c3_num_class_images,
    db_status
    """
    if model_dir == "" or model_dir is None:
        return "Please select a model.", 1000, -1, 0, -1, 0, -1, 0
    # Load config, get total steps
    config = from_file(model_dir)

    if config is None:
        status = "Unable to load config."
        return status, 0, 100, -1, 0, -1, 0, -1
    else:
        # Build concepts list using current settings
        concepts = config.concepts_list

        # Count the total number of images in all datasets
        total_images = 0
        counts_list = []
        max_images = 0

        # Set "base" value, which is 100 steps/image at LR of .000002
        if is_person:
            lr_scale = .000002 / config.learning_rate
            class_mult = 10
        else:
            class_mult = 0
            lr_scale = .0000025 / config.learning_rate
        step_mult = 100 * lr_scale

        for concept in concepts:
            if not os.path.exists(concept.instance_data_dir):
                print("Nonexistent instance directory.")
            else:
                concept_images = get_images(concept.instance_data_dir)
                total_images += len(concept_images)
                image_count = len(concept_images)
                print(f"Image count in {concept.instance_data_dir} is {image_count}")
                if image_count > max_images:
                    max_images = image_count
                c_dict = {
                    "concept": concept,
                    "images": image_count,
                    "classifiers": round(image_count * class_mult)
                }
                counts_list.append(c_dict)

        c_list = []
        status = f"Wizard results:"
        status += f"<br>Num Epochs: {step_mult}"
        status += f"<br>Max Steps: {0}"

        for x in range(3):
            if x < len(counts_list):
                c_dict = counts_list[x]
                c_list.append(int(c_dict["classifiers"]))
                status += f"<br>Concept {x} Class Images: {c_dict['classifiers']}"

            else:
                c_list.append(0)

        print(status)

    return 0, int(step_mult), -1, c_list[0], -1, c_list[1], -1, c_list[2], status


def ui_samples(model_dir: str,
               save_sample_prompt: str,
               num_samples: int = 1,
               batch_size: int = 1,
               lora_model_path: str = "",
               lora_weight: float = 1,
               lora_txt_weight: float = 1,
               negative_prompt: str = "",
               seed: int = -1,
               steps: int = 60,
               scale: float = 7.5
               ):
    dream_state.status.job_count = num_samples + 1
    if model_dir is None or model_dir == "":
        return "Please select a model."
    config = from_file(model_dir)
    msg = f"Generated {num_samples} sample(s)."
    images = []
    try:
        print(f"Loading model from {config.model_dir}.")
        dream_state.status.job_no = 1
        dream_state.status.textinfo = "Loading diffusion model..."
        img_builder = ImageBuilder(
            config,
            False,
            lora_model_path,
            lora_weight,
            lora_txt_weight,
            batch_size)
        if save_sample_prompt is None:
            msg = "Please provide a sample prompt."
            print(msg)
            return None, msg
        dream_state.status.textinfo = f"Generating sample image for model {config.model_name}..."
        dream_state.status.sampling_steps = 0
        dream_state.status.current_image_sampling_step = 0
        pd = PromptData()
        pd.steps = steps
        pd.prompt = save_sample_prompt
        pd.negative_prompt = negative_prompt
        pd.scale = scale
        pd.seed = seed
        prompts = [pd] * batch_size
        while len(images) > num_samples:
            out_images = img_builder.generate_images(prompts)
            for img in out_images:
                if len(images) > num_samples:
                    images.append(img)
        img_builder.unload()
    except Exception as e:
        msg = f"Exception generating sample(s): {e}"
        print(msg)
        traceback.print_exc()
    reload_system_models()
    return images, msg


def performance_wizard():
    attention = "xformers"
    gradient_checkpointing = True
    mixed_precision = 'fp16'
    target_precision = 'fp16'
    if torch.cuda.is_bf16_supported():
        mixed_precision = 'bf16'
        target_precision = 'bf16'
    not_cache_latents = True
    sample_batch_size = 1
    train_batch_size = 1
    train_text_encoder = False
    use_8bit_adam = True
    use_cpu = False
    use_ema = False
    try:
        t = torch.cuda.get_device_properties(0).total_memory
        gb = math.ceil(t / 1073741824)
        print(f"Total VRAM: {gb}")
        if gb >= 24:
            attention = "default"
            not_cache_latents = False
            sample_batch_size = 4
            train_batch_size = 2
            train_text_encoder = True
            use_ema = True
            use_8bit_adam = False
        if 24 > gb >= 16:
            attention = "xformers"
            not_cache_latents = False
            train_text_encoder = True
            use_ema = True
        if 16 > gb >= 10:
            train_text_encoder = False
            use_ema = False
        if gb < 10:
            use_cpu = True
            use_8bit_adam = False
            mixed_precision = 'no'

        msg = f"Calculated training params based on {gb}GB of VRAM:"
    except Exception as e:
        msg = f"An exception occurred calculating performance values: {e}"
        pass

    has_xformers = False
    try:
        import xformers
        import xformers.ops
        has_xformers = True
    except:
        pass
    if has_xformers:
        use_8bit_adam = True
        mixed_precision = target_precision

    if use_cpu:
        msg += "<br>Detected less than 10GB of VRAM, setting CPU training to true."
    log_dict = {"Attention": attention, "Gradient Checkpointing": gradient_checkpointing, "Precision": mixed_precision,
                "Cache Latents": not not_cache_latents, "Training Batch Size": train_batch_size,
                "Class Generation Batch Size": sample_batch_size,
                "Train Text Encoder": train_text_encoder, "8Bit Adam": use_8bit_adam, "EMA": use_ema, "CPU": use_cpu}
    for key in log_dict:
        msg += f"<br>{key}: {log_dict[key]}"
    return attention, gradient_checkpointing, mixed_precision, not_cache_latents, sample_batch_size, \
           train_batch_size, train_text_encoder, use_8bit_adam, use_cpu, use_ema, msg


def load_params(model_dir):
    data = from_file(model_dir)
    concepts = []
    ui_dict = {}
    msg = ""
    if data is None:
        print("Can't load config!")
        msg = "Please specify a model to load."
    elif data.__dict__ is None:
        print("Can't load config!")
        msg = "Please check your model config."
    else:
        for key in data.__dict__:
            value = data.__dict__[key]
            if key == "concepts_list":
                concepts = value
            else:
                if key == "pretrained_model_name_or_path":
                    key = "model_path"
                ui_dict[f"db_{key}"] = value
                msg = "Loaded config."

    ui_concept_list = concepts if concepts is not None else []
    if len(ui_concept_list) < 3:
        while len(ui_concept_list) < 3:
            ui_concept_list.append(Concept())
    c_idx = 1
    for ui_concept in ui_concept_list:
        if c_idx > 3:
            break

        for key in sorted(ui_concept):
            ui_dict[f"c{c_idx}_{key}"] = ui_concept[key]
        c_idx += 1
    ui_dict["db_status"] = msg
    ui_keys = ["db_adam_beta1",
               "db_adam_beta2",
               "db_adam_epsilon",
               "db_adam_weight_decay",
               "db_attention",
               "db_center_crop",
               "db_concepts_path",
               "db_custom_model_name",
               "db_epoch_pause_frequency",
               "db_epoch_pause_time",
               "db_gradient_accumulation_steps",
               "db_gradient_checkpointing",
               "db_half_model",
               "db_hflip",
               "db_learning_rate",
               "db_lora_learning_rate",
               "db_lora_txt_learning_rate",
               "db_lora_txt_weight",
               "db_lora_weight",
               "db_lr_scheduler",
               "db_lr_warmup_steps",
               "db_max_token_length",
               "db_max_train_steps",
               "db_mixed_precision",
               "db_not_cache_latents",
               "db_num_train_epochs",
               "db_pad_tokens",
               "db_pretrained_vae_name_or_path",
               "db_prior_loss_weight",
               "db_resolution",
               "db_sample_batch_size",
               "db_save_ckpt_after",
               "db_save_ckpt_cancel",
               "db_save_ckpt_during",
               "db_save_class_txt",
               "db_save_embedding_every",
               "db_save_lora_after",
               "db_save_lora_cancel",
               "db_save_lora_during",
               "db_save_preview_every",
               "db_save_state_after",
               "db_save_state_cancel",
               "db_save_state_during",
               "db_save_use_global_counts",
               "db_save_use_epochs",
               "db_scale_lr",
               "db_shuffle_tags",
               "db_train_batch_size",
               "db_train_text_encoder",
               "db_use_8bit_adam",
               "db_use_concepts",
               "db_use_cpu",
               "db_use_ema",
               "db_use_lora",
               "c1_class_data_dir", "c1_class_guidance_scale", "c1_class_infer_steps",
               "c1_class_negative_prompt", "c1_class_prompt", "c1_class_token",
               "c1_instance_data_dir", "c1_instance_prompt", "c1_instance_token", "c1_max_steps", "c1_n_save_sample",
               "c1_num_class_images", "c1_sample_seed", "c1_save_guidance_scale", "c1_save_infer_steps",
               "c1_save_sample_negative_prompt", "c1_save_sample_prompt", "c1_save_sample_template",
               "c2_class_data_dir",
               "c2_class_guidance_scale", "c2_class_infer_steps", "c2_class_negative_prompt", "c2_class_prompt",
               "c2_class_token", "c2_instance_data_dir", "c2_instance_prompt",
               "c2_instance_token", "c2_max_steps", "c2_n_save_sample", "c2_num_class_images", "c2_sample_seed",
               "c2_save_guidance_scale", "c2_save_infer_steps", "c2_save_sample_negative_prompt",
               "c2_save_sample_prompt", "c2_save_sample_template", "c3_class_data_dir", "c3_class_guidance_scale",
               "c3_class_infer_steps", "c3_class_negative_prompt", "c3_class_prompt", "c3_class_token",
               "c3_instance_data_dir", "c3_instance_prompt", "c3_instance_token",
               "c3_max_steps", "c3_n_save_sample", "c3_num_class_images", "c3_sample_seed", "c3_save_guidance_scale",
               "c3_save_infer_steps", "c3_save_sample_negative_prompt", "c3_save_sample_prompt",
               "c3_save_sample_template", "db_status"]
    output = []
    for key in ui_keys:
        if key in ui_dict:
            if key == "db_v2" or key == "db_has_ema":
                output.append("True" if ui_dict[key] else "False")
            else:
                output.append(ui_dict[key])
        else:
            if 'epoch' in key:
                output.append(0)
            else:
                output.append(None)
    print(f"Returning {output}")
    return output


def load_model_params(model_name):
    """
    @param model_name: The name of the model to load.
    @return:
    db_model_path: The full path to the model directory
    db_revision: The current revision of the model
    db_v2: If the model requires a v2 config/compilation
    db_has_ema: Was the model extracted with EMA weights
    db_src: The source checkpoint that weights were extracted from or hub URL
    db_scheduler: Scheduler used for this model
    db_outcome: The result of loading model params
    """
    data = from_file(model_name)
    if data is None:
        print("Can't load config!")
        msg = f"Error loading model params: '{model_name}'."
        return "", "", "", "", "", "", msg
    else:
        msg = f"Selected model: '{model_name}'."
        return data.model_dir, \
               data.revision, \
               data.epoch, \
               "True" if data.v2 else "False", \
               "True" if data.has_ema else "False", \
               data.src, \
               data.scheduler, \
               msg


def start_training(model_dir: str, lora_model_name: str, lora_alpha: float, lora_txt_alpha: float, imagic_only: bool,
                   use_subdir: bool, custom_model_name: str, use_txt2img: bool):
    """

    @param model_dir: The directory containing the dreambooth model/config
    @param lora_model_name: (Optional) - A lora model name to apply to diffusion model.
    @param lora_alpha: Lora unet strength if model name specified.
    @param lora_txt_alpha: Lora text encoder strength if model name specified.
    @param imagic_only: Train using imagic instead of dreambooth.
    @param use_subdir: Save generated checkpoints to a subdirectory in the models dir.
    @param custom_model_name: A custom filename to use when generating regular and lora checkpoints.
    @param use_txt2img: Whether to use txt2img or diffusion pipeline for image generation.
    @return:
    lora_model_name: If using lora, this will be the model name of the saved weights. (For resuming further training)
    revision: The model revision after training.
    status: Any relevant messages.
    """
    global mem_record
    if model_dir == "" or model_dir is None:
        print("Invalid model name.")
        msg = "Create or select a model first."
        dirs = get_lora_models()
        lora_model_name = gradio.Dropdown.update(choices=sorted(dirs), value=lora_model_name)
        return lora_model_name, 0, msg
    config = from_file(model_dir)

    # Clear pretrained VAE Name if applicable
    if config.pretrained_vae_name_or_path == "":
        config.pretrained_vae_name_or_path = None

    msg = None
    if config.attention == "xformers":
        if config.mixed_precision == "no":
            msg = "Using xformers, please set mixed precision to 'fp16' or 'bf16' to continue."
    if config.use_cpu:
        if config.use_8bit_adam or config.mixed_precision != "no":
            msg = "CPU Training detected, please disable 8Bit Adam and set mixed precision to 'no' to continue."
    if not len(config.concepts_list):
        msg = "Please configure some concepts."
    if not os.path.exists(config.pretrained_model_name_or_path):
        msg = "Invalid training data directory."
    if isset(config.pretrained_vae_name_or_path) and not os.path.exists(config.pretrained_vae_name_or_path):
        msg = "Invalid Pretrained VAE Path."
    if config.resolution <= 0:
        msg = "Invalid resolution."

    if msg:
        print(msg)
        dirs = get_lora_models()
        lora_model_name = gradio.Dropdown.update(choices=sorted(dirs), value=lora_model_name)
        return lora_model_name, 0, msg

    # Clear memory and do "stuff" only after we've ensured all the things are right
    print(f"Custom model name is {custom_model_name}")
    print("Starting Dreambooth training...")
    unload_system_models()
    total_steps = config.revision

    try:
        if imagic_only:
            dream_state.status.textinfo = "Initializing imagic training..."
            print(dream_state.status.textinfo)
            from extensions.sd_dreambooth_extension.dreambooth.train_imagic import train_imagic
            mem_record = train_imagic(config, mem_record)
        else:
            dream_state.status.textinfo = "Initializing dreambooth training..."
            print(dream_state.status.textinfo)
            from extensions.sd_dreambooth_extension.dreambooth.train_dreambooth import main
            config, mem_record, msg = main(config, mem_record, use_subdir=use_subdir, lora_model=lora_model_name,
                                           lora_alpha=lora_alpha, lora_txt_alpha=lora_txt_alpha,
                                           custom_model_name=custom_model_name, use_txt2img=use_txt2img)
            if config.revision != total_steps:
                config.save()
        total_steps = config.revision
        res = f"Training {'interrupted' if dream_state.status.interrupted else 'finished'}. " \
              f"Total lifetime steps: {total_steps} \n"
    except Exception as e:
        res = f"Exception training model: {e}"
        traceback.print_exc()
        pass

    devices.torch_gc()
    gc.collect()
    printm("Training completed, reloading SD Model.")
    print(f'Memory output: {mem_record}')
    reload_system_models()
    if lora_model_name != "" and lora_model_name is not None:
        lora_model_name = f"{config.model_name}_{total_steps}.pt"
    print(f"Returning result: {res}")
    dirs = get_lora_models()
    lora_model_name = gradio.Dropdown.update(choices=sorted(dirs), value=lora_model_name)
    return lora_model_name, total_steps, res


def ui_classifiers(model_name: str, lora_model: str, lora_weight: float, lora_txt_weight: float, use_txt2img: bool):
    """
    UI method for generating class images.
    @param model_name: The model to generate classes for.
    @param lora_model: An optional lora model to use when generating classes.
    @param lora_weight: The weight of the lora unet.
    @param lora_txt_weight: The weight of the lora text encoder.
    @param use_txt2img: Use txt2image when generating concepts.
    @return:
    """
    if model_name == "" or model_name is None:
        print("Invalid model name.")
        msg = "Create or select a model first."
        return msg
    config = from_file(model_name)

    # Clear pretrained VAE Name if applicable
    if config.pretrained_vae_name_or_path == "":
        config.pretrained_vae_name_or_path = None

    msg = None
    if config.attention == "xformers":
        if config.mixed_precision == "no":
            msg = "Using xformers, please set mixed precision to 'fp16' or 'bf16' to continue."
    if config.use_cpu:
        if config.use_8bit_adam or config.mixed_precision != "no":
            msg = "CPU Training detected, please disable 8Bit Adam and set mixed precision to 'no' to continue."
    if not len(config.concepts_list):
        msg = "Please configure some concepts."
    if not os.path.exists(config.pretrained_model_name_or_path):
        msg = "Invalid training data directory."
    if isset(config.pretrained_vae_name_or_path) and not os.path.exists(config.pretrained_vae_name_or_path):
        msg = "Invalid Pretrained VAE Path."
    if config.resolution <= 0:
        msg = "Invalid resolution."

    if msg:
        dream_state.status.textinfo = msg
        print(msg)
        return msg

    images = []
    try:
        from extensions.sd_dreambooth_extension.dreambooth.train_dreambooth import generate_classifiers
        print("Generating concepts...")
        unload_system_models()
        count, _, images = generate_classifiers(config, lora_model=lora_model, lora_weight=lora_weight,
                                                lora_text_weight=lora_txt_weight, use_txt2img=use_txt2img)
        reload_system_models()
        msg = f"Generated {count} class images."
    except Exception as e:
        msg = f"Exception generating concepts: {str(e)}"
        traceback.print_exc()
    return msg, images
