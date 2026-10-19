"""Единственная точка времени службы: тесты подменяют NOW и SLEEP."""
import asyncio
import time
from typing import Callable

NOW: Callable[[], float] = time.time
SLEEP = asyncio.sleep
