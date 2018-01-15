import asyncio
import json
from functools import wraps

import aiohttp
from async_timeout import timeout

from .logger import get_logger


class Bot:
    logger = get_logger(__name__)

    def __init__(self, token, timeout=30):
        self.token = token
        self.timeout = timeout
        self.queue = asyncio.Queue()
        self.handlers = {}
        self.update_id = 0
        self._polling = False

    @property
    def base_url(self):
        return f'https://api.telegram.org/bot{self.token}'

    async def dispatch(self, update):
        handler = self.handlers.get(update['message']['text'])
        if handler is not None:
            self.logger.debug('Dispatching...')
            result = await handler(update)
            return result
        self.logger.error('Handler not found')

    def on(self, message_text):
        def decorator(handler):
            self.handlers[message_text] = handler

            @wraps(handler)
            def wrapper(*args, **kwargs):
                return handler(*args, **kwargs)

            return wrapper

        return decorator

    async def reply(self, update, text):
        # TODO: Recall why exception (KeyError for instance)
        # is suppressed here and execution just hangs
        response = await self.make_request(
            'sendMessage', {'chat_id': update['message']['chat']['id'], 'text': text})
        self.logger.debug(response)
        return response

    async def make_request(self, method, payload=None):
        url = f'{self.base_url}/{method}'
        headers = {'content-type': 'application/json'}
        data = payload and json.dumps(payload)
        async with aiohttp.ClientSession() as client:
            async with client.post(url, data=data, headers=headers) as resp:
                self.logger.debug(resp.status)
                json_response = await resp.json()
                self.logger.debug(json_response)
                return json_response

    async def consume(self):
        while True:
            if not self._polling:
                break
            try:
                with timeout(self.timeout / 10):
                    update = await self.queue.get()
                    self.logger.debug(update)
                    await self.dispatch(update)
            except asyncio.TimeoutError:
                continue

    async def start_polling(self):
        self._polling = True
        async for update in self.update_generator():
            await self.queue.put(update)

    def stop_polling(self):
        self._polling = False

    async def update_generator(self):
        while True:
            if not self._polling:
                break

            try:
                with timeout(self.timeout):
                    response = await self.get_updates(self.update_id + 1)
                    result = response['result']

                    if not result:
                        continue

                    self.update_id = max(r['update_id'] for r in result)

                    for update in result:
                        yield update

            except asyncio.TimeoutError:
                continue
        raise StopAsyncIteration

    async def get_updates(self, offset):
        self.logger.debug('Getting updates...')
        url = f'{self.base_url}/getUpdates?timeout={self.timeout}&offset={offset}'
        async with aiohttp.ClientSession() as client:
            async with client.get(url) as resp:
                # TODO: Check response status
                json_response = await resp.json()
                return json_response

    def run(self):
        loop = asyncio.get_event_loop()

        poller = loop.create_task(self.start_polling())
        consumer = loop.create_task(self.consume())

        try:
            loop.run_forever()
        # TODO: Handle termination in a more neat way (using signals)
        except KeyboardInterrupt:
            # TODO: Simplify shutdown
            self.logger.info('Shutting down...')
            self.stop_polling()
            self.logger.info('Waiting for poller to complete')
            # loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(poller)
            self.logger.info('Waiting for consumer to complete')
            loop.run_until_complete(consumer)
            loop.close()