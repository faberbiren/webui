import os
import sys
import time
import importlib
import signal
import re
import warnings
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from packaging import version

import logging
logging.getLogger("xformers").addFilter(lambda record: 'A matching Triton is not available' not in record.getMessage())

from modules import paths, timer, import_hook, errors

startup_timer = timer.Timer()

import torch
import pytorch_lightning # pytorch_lightning should be imported after torch, but it re-enables warnings on import so import once to disable them
warnings.filterwarnings(action="ignore", category=DeprecationWarning, module="pytorch_lightning")
startup_timer.record("import torch")

import gradio
startup_timer.record("import gradio")

import ldm.modules.encoders.modules
startup_timer.record("import ldm")

from modules import extra_networks, ui_extra_networks_checkpoints
from modules import extra_networks_hypernet, ui_extra_networks_hypernets, ui_extra_networks_textual_inversion
from modules.call_queue import wrap_queued_call, queue_lock, wrap_gradio_gpu_call

# Truncate version number of nightly/local build of PyTorch to not cause exceptions with CodeFormer or Safetensors
if ".dev" in torch.__version__ or "+git" in torch.__version__:
    torch.__long_version__ = torch.__version__
    torch.__version__ = re.search(r'[\d.]+[\d]', torch.__version__).group(0)

from modules import shared, devices, sd_samplers, upscaler, extensions, localization, ui_tempdir, ui_extra_networks
import modules.codeformer_model as codeformer
import modules.face_restoration
import modules.gfpgan_model as gfpgan
import modules.img2img

import modules.lowvram
import modules.scripts
import modules.sd_hijack
import modules.sd_models
import modules.sd_vae
import modules.txt2img
import modules.script_callbacks
import modules.textual_inversion.textual_inversion
import modules.progress

import modules.ui
from modules import modelloader
from modules.shared import cmd_opts
import modules.hypernetworks.hypernetwork

startup_timer.record("other imports")


if cmd_opts.server_name:
    server_name = cmd_opts.server_name
else:
    server_name = "0.0.0.0" if cmd_opts.listen else None


def check_versions():
    if shared.cmd_opts.skip_version_check:
        return

    expected_torch_version = "3.11.12"

    if version.parse(torch.__version__) < version.parse(expected_torch_version):
        errors.print_error_explanation(f"""
You are running torch {torch.__version__}.
The program is tested to work with torch {expected_torch_version}.
To reinstall the desired version, run with commandline flag --reinstall-torch.
Beware that this will cause a lot of large files to be downloaded, as well as
there are reports of issues with training tab on the latest version.

Use --skip-version-check commandline argument to disable this check.
        """.strip())

    expected_xformers_version = "0.0.16rc425"
    if shared.xformers_available:
        import xformers

        if version.parse(xformers.__version__) < version.parse(expected_xformers_version):
            errors.print_error_explanation(f"""
You are running xformers {xformers.__version__}.
The program is tested to work with xformers {expected_xformers_version}.
To reinstall the desired version, run with commandline flag --reinstall-xformers.

Use --skip-version-check commandline argument to disable this check.
            """.strip())


def initialize():
    check_versions()

    extensions.list_extensions()
    localization.list_localizations(cmd_opts.localizations_dir)
    startup_timer.record("list extensions")

    if cmd_opts.ui_debug_mode:
        shared.sd_upscalers = upscaler.UpscalerLanczos().scalers
        modules.scripts.load_scripts()
        return

    modelloader.cleanup_models()
    modules.sd_models.setup_model()
    startup_timer.record("list SD models")

    codeformer.setup_model(cmd_opts.codeformer_models_path)
    startup_timer.record("setup codeformer")

    gfpgan.setup_model(cmd_opts.gfpgan_models_path)
    startup_timer.record("setup gfpgan")

    modelloader.list_builtin_upscalers()
    startup_timer.record("list builtin upscalers")

    modules.scripts.load_scripts()
    startup_timer.record("load scripts")

    modelloader.load_upscalers()
    startup_timer.record("load upscalers")

    modules.sd_vae.refresh_vae_list()
    startup_timer.record("refresh VAE")

    modules.textual_inversion.textual_inversion.list_textual_inversion_templates()
    startup_timer.record("refresh textual inversion templates")

    try:
        modules.sd_models.load_model()
    except Exception as e:
        errors.display(e, "loading stable diffusion model")
        print("", file=sys.stderr)
        print("Stable diffusion model failed to load, exiting", file=sys.stderr)
        exit(1)
    startup_timer.record("load SD checkpoint")

    shared.opts.data["sd_model_checkpoint"] = shared.sd_model.sd_checkpoint_info.title

    shared.opts.onchange("sd_model_checkpoint", wrap_queued_call(lambda: modules.sd_models.reload_model_weights()))
    shared.opts.onchange("sd_vae", wrap_queued_call(lambda: modules.sd_vae.reload_vae_weights()), call=False)
    shared.opts.onchange("sd_vae_as_default", wrap_queued_call(lambda: modules.sd_vae.reload_vae_weights()), call=False)
    shared.opts.onchange("temp_dir", ui_tempdir.on_tmpdir_changed)
    startup_timer.record("opts onchange")

    shared.reload_hypernetworks()
    startup_timer.record("reload hypernets")

    ui_extra_networks.intialize()
    ui_extra_networks.register_page(ui_extra_networks_textual_inversion.ExtraNetworksPageTextualInversion())
    ui_extra_networks.register_page(ui_extra_networks_hypernets.ExtraNetworksPageHypernetworks())
    ui_extra_networks.register_page(ui_extra_networks_checkpoints.ExtraNetworksPageCheckpoints())

    extra_networks.initialize()
    extra_networks.register_extra_network(extra_networks_hypernet.ExtraNetworkHypernet())
    startup_timer.record("extra networks")

    if cmd_opts.tls_keyfile is not None and cmd_opts.tls_keyfile is not None:

        try:
            if not os.path.exists(cmd_opts.tls_keyfile):
                print("Invalid path to TLS keyfile given")
            if not os.path.exists(cmd_opts.tls_certfile):
                print(f"Invalid path to TLS certfile: '{cmd_opts.tls_certfile}'")
        except TypeError:
            cmd_opts.tls_keyfile = cmd_opts.tls_certfile = None
            print("TLS setup invalid, running webui without TLS")
        else:
            print("Running with TLS")
        startup_timer.record("TLS")

    # make the program just exit at ctrl+c without waiting for anything
    def sigint_handler(sig, frame):
        print(f'Interrupted with signal {sig} in {frame}')
        os._exit(0)

    signal.signal(signal.SIGINT, sigint_handler)


def setup_middleware(app):
    app.middleware_stack = None # reset current middleware to allow modifying user provided list
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    if cmd_opts.cors_allow_origins and cmd_opts.cors_allow_origins_regex:
        app.add_middleware(CORSMiddleware, allow_origins=cmd_opts.cors_allow_origins.split(','), allow_origin_regex=cmd_opts.cors_allow_origins_regex, allow_methods=['*'], allow_credentials=True, allow_headers=['*'])
    elif cmd_opts.cors_allow_origins:
        app.add_middleware(CORSMiddleware, allow_origins=cmd_opts.cors_allow_origins.split(','), allow_methods=['*'], allow_credentials=True, allow_headers=['*'])
    elif cmd_opts.cors_allow_origins_regex:
        app.add_middleware(CORSMiddleware, allow_origin_regex=cmd_opts.cors_allow_origins_regex, allow_methods=['*'], allow_credentials=True, allow_headers=['*'])
    app.build_middleware_stack() # rebuild middleware stack on-the-fly


def create_api(app):
    from modules.api.api import Api
    api = Api(app, queue_lock)
    return api


def wait_on_server(demo=None):
    while 1:
        time.sleep(0.5)
        if shared.state.need_restart:
            shared.state.need_restart = False
            time.sleep(0.5)
            demo.close()
            time.sleep(0.5)
            break


def api_only():
    initialize()

    app = FastAPI()
    setup_middleware(app)
    api = create_api(app)

    modules.script_callbacks.app_started_callback(None, app)

    print(f"Startup time: {startup_timer.summary()}.")
    api.launch(server_name="0.0.0.0" if cmd_opts.listen else "127.0.0.1", port=cmd_opts.port if cmd_opts.port else 7861)


def webui():
    initialize()

    while 1:
        if shared.opts.clean_temp_dir_at_start:
            ui_tempdir.cleanup_tmpdr()
            startup_timer.record("cleanup temp dir")

        modules.script_callbacks.before_ui_callback()
        startup_timer.record("scripts before_ui_callback")

        shared.demo = modules.ui.create_ui()
        startup_timer.record("create ui")

        app, local_url, share_url = shared.demo.launch(
            height=3000,
            prevent_thread_lock=True
        )
        # after initial launch, disable --autolaunch for subsequent restarts
        cmd_opts.autolaunch = False

        startup_timer.record("gradio launch")

        app.user_middleware = [x for x in app.user_middleware if x.cls.__name__ != 'CORSMiddleware']
        initialize_util.setup_middleware(app)

        progress.setup_progress_api(app)
        ui.setup_ui_api(app)

        ui_extra_networks.add_pages_to_demo(app)

        modules.script_callbacks.app_started_callback(shared.demo, app)
        startup_timer.record("scripts app_started_callback")

        print(f"Startup time: {startup_timer.summary()}.")

        wait_on_server(shared.demo)
        print('Restarting UI...')

        startup_timer.reset()

        sd_samplers.set_samplers()

        modules.script_callbacks.script_unloaded_callback()
        extensions.list_extensions()
        startup_timer.record("list extensions")

        localization.list_localizations(cmd_opts.localizations_dir)

        modelloader.forbid_loaded_nonbuiltin_upscalers()
        modules.scripts.reload_scripts()
        startup_timer.record("load scripts")

        modules.script_callbacks.model_loaded_callback(shared.sd_model)
        startup_timer.record("model loaded callback")

        modelloader.load_upscalers()
        startup_timer.record("load upscalers")

        for module in [module for name, module in sys.modules.items() if name.startswith("modules.ui")]:
            importlib.reload(module)
        startup_timer.record("reload script modules")

        modules.sd_models.list_models()
        startup_timer.record("list SD models")

        shared.reload_hypernetworks()
        startup_timer.record("reload hypernetworks")

        ui_extra_networks.intialize()
        ui_extra_networks.register_page(ui_extra_networks_textual_inversion.ExtraNetworksPageTextualInversion())
        ui_extra_networks.register_page(ui_extra_networks_hypernets.ExtraNetworksPageHypernetworks())
        ui_extra_networks.register_page(ui_extra_networks_checkpoints.ExtraNetworksPageCheckpoints())

        extra_networks.initialize()
        extra_networks.register_extra_network(extra_networks_hypernet.ExtraNetworkHypernet())
        startup_timer.record("initialize extra networks")


if __name__ == "__main__":
    if cmd_opts.nowebui:
        api_only()
    else:
        webui()
