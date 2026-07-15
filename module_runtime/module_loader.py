import os
import importlib.util

MODULES_PATH = "modules"

loaded_modules = {}


def load_module(name):
    module_path = os.path.join(MODULES_PATH, name, "module.py")

    if not os.path.exists(module_path):
        return None

    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)

    try:
        spec.loader.exec_module(module)
    except Exception as e:
        print(f"❌ Module load failed: {e}")
        return None

    loaded_modules[name] = module
    return module


def get_module(name):
    return loaded_modules.get(name)


def list_modules():
    return list(loaded_modules.keys())

def load_all_modules():
    if not os.path.exists(MODULES_PATH):
        return

    for name in os.listdir(MODULES_PATH):
        module_dir = os.path.join(MODULES_PATH, name)

        if os.path.isdir(module_dir):
            load_module(name)