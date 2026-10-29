"""Единственная точка времени службы: тесты подменяют NOW, SLEEP и TIMEOUT."""
import asyncio
import time
from typing import Callable

NOW: Callable[[], float] = time.time
SLEEP = asyncio.sleep
TIMEOUT = asyncio.timeout          # срок на целый шаг (скачивание ролика); тест истекает им в нужный момент
