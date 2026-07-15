#logger_config.py
import os
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime

ROOT_DIR = os.path.dirname(__file__)
LOG_DIR = os.path.join(ROOT_DIR, "Logs")
os.makedirs(LOG_DIR, exist_ok=True)
timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
LOG_FILE = os.path.join(LOG_DIR, f"alex_{timestamp}.log")

logger = logging.getLogger("ALEX")
logger.setLevel(logging.DEBUG)

file_handler = RotatingFileHandler(
    LOG_FILE,
    maxBytes=5*1024*1024,
    backupCount=3,
    encoding="utf-8"
)
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)