"""Serialize VK token rotation and read credentials saved by other workers."""

from contextlib import contextmanager
import fcntl
import os
import time

from . import publish_secret_store
from .publication_config import project_env_name


@contextmanager
def locked_vk_credentials(project_slug: str):
    # Shared by all processes on this host; never contains credentials itself.
    env_path = publish_secret_store.ENV_PATH
    lock_path = env_path.with_name(env_path.name + ".vk-oauth.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + 45
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("VK credential refresh is busy; retry later.") from None
                time.sleep(0.1)
        # load_env_file intentionally does not overwrite os.environ. Rotated
        # credentials are the exception: another worker may have replaced them.
        names = {project_env_name(project_slug, key) for key in (
            "VK_ACCESS_TOKEN", "VK_REFRESH_TOKEN", "VK_DEVICE_ID",
        )}
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                key, sep, value = line.partition("=")
                if sep and key.strip() in names:
                    os.environ[key.strip()] = value.strip().strip('"').strip("'")
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
