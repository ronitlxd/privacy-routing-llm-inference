"""Shared runner options; generation budgets can differ without changing residency.

The options of each model come from its role in jlr.models (models.toml, or the .env settings):
GPU layers, threads and context are settings, no longer decided from the model's name.
"""
import os


def runner_options(model, output_tokens=512):
    from .models import config
    for role in config().roles.values():
        if role.model == model and role.endpoint.provider == 'ollama':
            return role.options(output_tokens)
    return {'temperature': 0, 'seed': 42, 'num_ctx': int(os.environ.get('JLR_V2_CONTEXT', '4096')),
            'num_thread': int(os.environ.get('JLR_THREADS', '4')), 'num_gpu': int(os.environ.get('JLR_NUM_GPU', '-1')),
            'num_predict': output_tokens}
